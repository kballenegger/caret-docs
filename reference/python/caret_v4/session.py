"""The shared live-audio lifecycle, once.

§4 of the protocol says the three routes differ only in their finalizer
and their terminal result type. This file is that claim made structural:
one state machine reads `start`, streams audio, verifies the finalize
totals, and hands off to exactly one route-specific finish function. A
rule that lives here applies to /dictate, /ask, and /imagine identically,
because there is nowhere else for it to live.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
import uuid

from . import lanes
from .protocol import (
    ASPECT_RATIOS,
    AUDIO_SAMPLE_RATE_HZ,
    BYTES_PER_SECOND,
    ERR_AUDIO_INCOMPLETE,
    ERR_AUDIO_TOO_LONG,
    ERR_AUDIO_TOO_SHORT,
    ERR_BAD_REQUEST,
    ERR_GENERATION_FAILED,
    ERR_NO_SPEECH_DETECTED,
    ERR_NOT_SUPPORTED,
    ERR_PROTOCOL_ERROR,
    ERR_TIMEOUT,
    ERR_TRANSCRIPTION_FAILED,
    ERR_UNAUTHORIZED,
    PROTOCOL_VERSION,
    QUALITIES,
    ROUTE_ASK,
    ROUTE_DICTATE,
    ROUTE_IMAGINE,
    AudioSpec,
    OpError,
    close_code_for,
    normalize_vocabulary,
    retryable_for,
)
from .ws import CloseError, WSError

#: How long a client has to send its start frame, and how long a gap
#: between frames is tolerated before the operation times out.
START_DEADLINE_SECONDS = 10.0
FRAME_GAP_SECONDS = 60.0
#: §4 requires a progress event or a ping at least every 20 seconds.
PROGRESS_EVERY_SECONDS = 5.0
#: Shorter than this and there is nothing to transcribe.
MIN_AUDIO_MS = 300


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Session:
    def __init__(self, server, conn, route: str, authorization: str) -> None:
        self.server = server
        self.conn = conn
        self.route = route
        self.authorization = authorization
        self.op_id = new_id("op")
        self.request_id = ""
        self.started_at = time.monotonic()

        self.start_frame: dict = {}
        self.text_input = False
        self.input_text = ""
        self.vocabulary: list[str] = []

        self.audio = bytearray()
        self.frames = 0
        self.stream = None
        self.partial_seq = 0

        self.terminated = False
        self._send_lock = threading.Lock()

    # -------------------------------------------------------------- run

    def run(self) -> None:
        try:
            self._run()
        except (CloseError, WSError, OSError):
            # The peer vanished before the terminal event. §8: the server
            # discards the operation and the client replays it.
            self.terminated = True
        except Exception:  # pragma: no cover — a bug, not a protocol path
            self.server.log.exception("op=%s route=%s unhandled error", self.op_id, self.route)
            self._fail(OpError("internal_error", "the backend failed unexpectedly"))
        finally:
            self._abort_stream()
            try:
                self.conn.close(1000, "")
            except OSError:
                pass

    def _run(self) -> None:
        if not self.server.credential_valid(self.authorization):
            self._fail(OpError(ERR_UNAUTHORIZED, "missing or invalid credential"))
            return
        if not self.server.route_enabled(self.route):
            self._fail(OpError(ERR_NOT_SUPPORTED, f"this backend does not serve /{self.route}"))
            return
        try:
            self._read_start()
        except OpError as exc:
            self._fail(exc)
            return

        # §8 idempotency: a replayed client_request_id gets ready and then
        # the cached result immediately, so a retry after a dropped
        # connection is not a second bill.
        cached = self.server.cache_get(self.route, self.start_frame["client_request_id"])
        if cached is not None:
            self._send_ready()
            self.request_id = cached["request_id"]
            self._send_result(cached["result"], cached["audio"])
            return

        self._send_ready()
        if not self.text_input:
            self._open_stream()

        try:
            final = self._read_input()
        except OpError as exc:
            self._abort_stream()
            self._fail(exc)
            return
        if final is None:  # cancelled, or the socket died: no terminal event
            self._abort_stream()
            return
        try:
            self._check_finalize(final)
        except OpError as exc:
            self._abort_stream()
            self._fail(exc)
            return
        self._finish(final)

    # ------------------------------------------------------------ start

    def _read_start(self) -> None:
        self.conn.set_timeout(START_DEADLINE_SECONDS)
        try:
            binary, data = self.conn.read_message()
        except socket.timeout:
            raise OpError(ERR_TIMEOUT, "no start frame within 10 seconds") from None
        if binary:
            raise OpError(ERR_PROTOCOL_ERROR, "audio arrived before start")
        frame = _parse_control(data)
        if frame.get("type") != "start":
            raise OpError(ERR_PROTOCOL_ERROR, "the first frame must be start")
        if frame.get("protocol") != PROTOCOL_VERSION:
            raise OpError(ERR_PROTOCOL_ERROR, "this backend speaks protocol 4")

        client_request_id = frame.get("client_request_id")
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 128:
            raise OpError(ERR_BAD_REQUEST, "client_request_id is required and at most 128 characters")
        if len(str(frame.get("language_hint", ""))) > 16:
            raise OpError(ERR_BAD_REQUEST, "language_hint is at most 16 characters")
        if len(str(frame.get("app_hint", ""))) > 200:
            raise OpError(ERR_BAD_REQUEST, "app_hint is at most 200 characters")

        source = frame.get("input")
        if not isinstance(source, dict):
            raise OpError(ERR_BAD_REQUEST, "start needs an input object")
        kind = source.get("type")
        if kind == "audio":
            if source.get("codec") not in (None, "pcm16"):
                raise OpError(ERR_BAD_REQUEST, "the only codec is pcm16")
            if source.get("sample_rate_hz") not in (None, AUDIO_SAMPLE_RATE_HZ):
                raise OpError(ERR_BAD_REQUEST, "audio must be 16 kHz")
            if source.get("channels") not in (None, 1):
                raise OpError(ERR_BAD_REQUEST, "audio must be mono")
        elif kind == "text":
            text = source.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= self.server.max_text_chars:
                raise OpError(ERR_BAD_REQUEST, "text input is 1 to advertised max_text_chars characters")
            self.text_input = True
            self.input_text = text
        else:
            raise OpError(ERR_BAD_REQUEST, "input.type is audio or text")

        self.vocabulary = normalize_vocabulary(
            frame.get("vocabulary"), self.server.max_vocabulary_entries
        )
        self.start_frame = frame

    def _send_ready(self) -> None:
        stt = None
        if not self.text_input:
            stt = "streaming" if getattr(self.server.stt, "streaming", False) else "buffered"
        self._send({"event": "ready", "protocol": PROTOCOL_VERSION, "op_id": self.op_id, "stt": stt})

    def _stt_options(self) -> lanes.STTOptions:
        return lanes.STTOptions(
            vocabulary=self.vocabulary,
            language_hint=str(self.start_frame.get("language_hint", "")),
            sample_rate_hz=AUDIO_SAMPLE_RATE_HZ,
        )

    def _open_stream(self) -> None:
        """Ask the speech lane for a live recognizer. A provider without
        one is not a failure: §8 says the operation lives on, audio is
        buffered, and the transcript is produced at finalize."""
        if self.server.stt is None:
            return
        try:
            self.stream = self.server.stt.open(self._stt_options(), self._emit_partial)
        except lanes.NoStreaming:
            self.stream = None
        except Exception:
            self.server.log.warning("op=%s streaming recognizer unavailable", self.op_id)
            self.stream = None

    def _emit_partial(self, text: str) -> None:
        if self.terminated or not text:
            return
        self.partial_seq += 1
        self._send({
            "event": "partial",
            "op_id": self.op_id,
            "seq": self.partial_seq,
            "frames": self.frames,
            "bytes": len(self.audio),
            "text": text,
        })

    # ------------------------------------------------------------ input

    def _read_input(self) -> dict | None:
        """Run until finalize (returned), cancel or a dead socket (None),
        or a terminal error (raised)."""
        while True:
            self.conn.set_timeout(FRAME_GAP_SECONDS)
            try:
                binary, data = self.conn.read_message()
            except socket.timeout:
                raise OpError(ERR_TIMEOUT, "no audio for 60 seconds") from None
            except (CloseError, WSError, OSError):
                self.terminated = True
                return None
            if binary:
                self._consume_audio(data)
                continue
            frame = _parse_control(data)
            kind = frame.get("type")
            if kind == "finalize":
                return frame
            if kind == "cancel":
                self.terminated = True
                return None
            if kind == "start":
                raise OpError(ERR_PROTOCOL_ERROR, "start arrived twice")
            raise OpError(ERR_PROTOCOL_ERROR, "unknown control frame")

    def _consume_audio(self, chunk: bytes) -> None:
        if self.text_input:
            raise OpError(ERR_PROTOCOL_ERROR, "binary frames are not allowed with text input")
        if len(chunk) > self.server.max_frame_bytes:
            raise OpError(ERR_BAD_REQUEST, "audio frame exceeds the advertised max_frame_bytes")
        if not chunk:
            return  # empty frames are ignored, not errors
        if len(self.audio) + len(chunk) > self.server.max_audio_seconds * BYTES_PER_SECOND:
            raise OpError(ERR_AUDIO_TOO_LONG, "audio exceeded the advertised max_audio_seconds")
        self.frames += 1
        self.audio.extend(chunk)
        if self.stream is not None:
            try:
                self.stream.write(chunk)
            except Exception:
                # §8: streaming failure is not operation failure. Partials
                # stop; the buffered audio still gets transcribed.
                self.server.log.warning("op=%s streaming recognizer died mid-stream", self.op_id)
                self._abort_stream()

    def _check_finalize(self, frame: dict) -> None:
        """The V4 reliability contract: the client's own totals must match
        what the server received, or the operation is retried rather than
        transcribed from silently truncated audio."""
        totals = frame.get("audio")
        if self.text_input:
            if totals is not None:
                raise OpError(ERR_BAD_REQUEST, "finalize must not carry audio totals for text input")
            return
        if not isinstance(totals, dict):
            raise OpError(ERR_BAD_REQUEST, "finalize needs the audio totals")
        if totals.get("frames") != self.frames or totals.get("bytes") != len(self.audio):
            self.server.log.info(
                "op=%s route=%s audio mismatch: client %s/%s, server %d/%d",
                self.op_id, self.route, totals.get("frames"), totals.get("bytes"),
                self.frames, len(self.audio),
            )
            raise OpError(
                ERR_AUDIO_INCOMPLETE,
                "the audio the server received does not match what the client sent",
            )
        if self._duration_ms() < MIN_AUDIO_MS:
            raise OpError(ERR_AUDIO_TOO_SHORT, "that was too short to transcribe")

    def _duration_ms(self) -> int:
        return len(self.audio) * 1000 // BYTES_PER_SECOND

    def _consumed_audio(self) -> dict | None:
        if self.text_input:
            return None
        return AudioSpec(self.frames, len(self.audio), self._duration_ms()).as_json()

    # ------------------------------------------------------- finalizers

    def _finish(self, frame: dict) -> None:
        stop = self._start_progress()
        try:
            if self.route == ROUTE_DICTATE:
                result = self._finish_dictate(frame)
            elif self.route == ROUTE_ASK:
                result = self._finish_ask(frame)
            elif self.route == ROUTE_IMAGINE:
                result = self._finish_imagine(frame)
            else:  # pragma: no cover — the mux only routes the three
                raise OpError(ERR_NOT_SUPPORTED, "unknown route")
        except OpError as exc:
            stop()
            self._fail(exc)
            return
        finally:
            stop()
        self.request_id = new_id("req")
        self.server.cache_put(
            self.route, self.start_frame["client_request_id"],
            self.request_id, result, self._consumed_audio(),
        )
        self._send_result(result, self._consumed_audio())

    def _start_progress(self):
        """Keep the socket honest while a lane works."""
        done = threading.Event()
        began = time.monotonic()

        def tick():
            while not done.wait(PROGRESS_EVERY_SECONDS):
                self._send({
                    "event": "progress",
                    "op_id": self.op_id,
                    "stage": self._stage(),
                    "elapsed_ms": int((time.monotonic() - began) * 1000),
                })

        thread = threading.Thread(target=tick, name=f"progress-{self.op_id}", daemon=True)
        thread.start()
        return done.set

    def _stage(self) -> str:
        return {ROUTE_IMAGINE: "generating", ROUTE_ASK: "answering"}.get(self.route, "transcribing")

    def _finish_dictate(self, frame: dict) -> dict:
        raw, route = self._transcript()
        polish = frame.get("polish", True) is not False
        text, applied = raw, False
        if polish:
            polished = self._polish(raw)
            if polished is not None:
                text, applied = polished, True
        result = {
            "type": "dictation",
            "text": text,
            "raw_transcript": raw,
            "polish_applied": applied,
        }
        # Omitted rather than invented for text input: there was no
        # recognizer, so neither "stream" nor "fallback" would be true.
        if route:
            result["stt_route"] = route
        return result

    def _polish(self, raw: str) -> str | None:
        """`caret-cleanup/1` through the configured lane. Best-effort by
        contract: every failure returns None and the raw transcript
        travels, because a dictation that arrives unpolished is a small
        disappointment and one that arrives as an error message is a lost
        thought."""
        if self.server.cleanup is None or self.server.spec is None or not raw.strip():
            return None
        framing = self.server.spec.system_prompt(self.vocabulary)
        try:
            text = self.server.cleanup.polish(framing, raw)
        except Exception:
            self.server.log.warning("op=%s cleanup failed, returning the raw transcript", self.op_id)
            return None
        return text if text and text.strip() else None

    def _finish_ask(self, frame: dict) -> dict:
        visible_text = str(frame.get("visible_text") or "")
        if len(visible_text) > self.server.max_text_chars:
            raise OpError(ERR_BAD_REQUEST, "visible_text is too long")
        prompt, transcript = self._prompt_text()
        try:
            text = self.server.agent.respond(prompt, visible_text, self.vocabulary)
        except Exception:
            self.server.log.warning("op=%s route=ask agent lane failed", self.op_id)
            raise OpError(ERR_GENERATION_FAILED, "the agent could not answer") from None
        if not text or not text.strip():
            raise OpError(ERR_GENERATION_FAILED, "the agent could not answer")
        return {"type": "message", "text": text, "transcript": transcript}

    def _finish_imagine(self, frame: dict) -> dict:
        aspect = str(frame.get("aspect_ratio") or "1:1")
        if aspect not in ASPECT_RATIOS:
            raise OpError(ERR_BAD_REQUEST, "aspect_ratio is 1:1, 3:2, or 2:3")
        quality = str(frame.get("quality") or "high")
        if quality not in QUALITIES:
            raise OpError(ERR_BAD_REQUEST, "quality is low, medium, or high")
        prompt, transcript = self._prompt_text()
        try:
            mime, data = self.server.image.generate(prompt, aspect, quality)
        except Exception:
            self.server.log.warning("op=%s route=imagine image lane failed", self.op_id)
            raise OpError(ERR_GENERATION_FAILED, "the image could not be generated") from None
        if not data:
            raise OpError(ERR_GENERATION_FAILED, "the image could not be generated")
        return {
            "type": "image",
            "mime_type": mime,
            "byte_length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(data).decode("ascii"),
            "transcript": transcript,
            "provider": getattr(self.server.image, "name", "unknown"),
        }

    def _prompt_text(self) -> tuple[str, str]:
        """The instruction the route works from, plus the transcript to
        echo back when the input was speech."""
        if self.text_input:
            return self.input_text, ""
        text, _ = self._transcript()
        return text, text

    def _transcript(self) -> tuple[str, str]:
        """Resolve the audio to words: the live recognizer's flush when
        there was one, the batch route otherwise.

        An empty transcript from a healthy route is `no_speech_detected`.
        Silence is an answer, not an error, and never a reason to fall
        back to a second recognizer that will also hear nothing.
        """
        if self.text_input:
            return self.input_text, ""
        if self.server.stt is None:
            raise OpError(ERR_NOT_SUPPORTED, "this backend has no speech recognizer")
        route = "fallback"
        text = ""
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                text, route = stream.finish(), "stream"
            except Exception:
                self.server.log.warning(
                    "op=%s streaming flush failed, falling back to batch", self.op_id
                )
        if route == "fallback":
            try:
                text = self.server.stt.transcribe(bytes(self.audio), self._stt_options())
            except Exception:
                self.server.log.warning("op=%s every transcription route failed", self.op_id)
                raise OpError(ERR_TRANSCRIPTION_FAILED, "speech recognition is unavailable") from None
        if not text or not text.strip():
            raise OpError(ERR_NO_SPEECH_DETECTED, "no speech was detected")
        return text, route

    def _abort_stream(self) -> None:
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.abort()
            except Exception:
                pass

    # -------------------------------------------------------- terminals

    def _send(self, event: dict) -> None:
        with self._send_lock:
            try:
                self.conn.send_text(json.dumps(event, ensure_ascii=False))
            except (OSError, WSError):
                self.terminated = True

    def _send_result(self, result: dict, audio: dict | None) -> None:
        if self.terminated:
            return
        self.terminated = True
        self.request_id = self.request_id or new_id("req")
        event = {
            "event": "result",
            "op_id": self.op_id,
            "request_id": self.request_id,
            "result": result,
        }
        if audio is not None:
            event["audio"] = audio
        self._send(event)
        self.server.log.info(
            "op=%s route=%s ok in %dms",
            self.op_id, self.route, int((time.monotonic() - self.started_at) * 1000),
        )
        self.conn.close(1000, "")

    def _fail(self, error: OpError | None) -> None:
        """The one terminal error event, with §10's close code. Logging is
        ids, counts, and error codes: never a transcript, never audio,
        never a vocabulary."""
        if error is None or self.terminated:
            return
        self.terminated = True
        self.request_id = self.request_id or new_id("req")
        self._send({
            "event": "error",
            "op_id": self.op_id,
            "request_id": self.request_id,
            "code": error.code,
            "message": error.message,
            "retryable": retryable_for(error.code),
        })
        self.server.log.info(
            "op=%s route=%s error=%s in %dms",
            self.op_id, self.route, error.code, int((time.monotonic() - self.started_at) * 1000),
        )
        self.conn.close(close_code_for(error.code), error.code)


def _parse_control(data: bytes) -> dict:
    try:
        frame = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OpError(ERR_PROTOCOL_ERROR, "control frame is not JSON") from None
    if not isinstance(frame, dict):
        raise OpError(ERR_PROTOCOL_ERROR, "control frame is not a JSON object")
    return frame
