"""The conformance checker.

It treats the backend under test as a black box at a base URL: any
language, any host, a production backend or a first attempt written this
afternoon. It exercises what is easy to get subtly wrong — the finalize
totals check, exactly-one-terminal-event ordering after queued partials,
cumulative partial text, replay under a repeated `client_request_id`,
honest capabilities against served routes, and §10's error table with
its close codes.

The Go implementation ships the same checks. Running each language's
checker against the other language's server is how this repository knows
the two agree about the protocol rather than merely about themselves.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .protocol import (
    AUDIO_SAMPLE_RATE_HZ,
    BYTES_PER_SECOND,
    ERR_AUDIO_INCOMPLETE,
    ERR_AUDIO_TOO_SHORT,
    ERR_BAD_REQUEST,
    ERR_NO_SPEECH_DETECTED,
    ERR_NOT_SUPPORTED,
    ERR_PROTOCOL_ERROR,
    ERR_UNAUTHORIZED,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ROUTE_ASK,
    ROUTE_DICTATE,
    ROUTE_IMAGINE,
    close_code_for,
    retryable_for,
)
from .ws import CloseError, WSError, dial

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
WARN = "warn"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""

    def as_json(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Probe:
    """One operation seen from the client side."""

    events: list = field(default_factory=list)
    partials: list = field(default_factory=list)
    terminal: dict | None = None
    close_code: int = 0
    http_status: int = 0
    error: str = ""

    @property
    def terminal_kind(self) -> str:
        return (self.terminal or {}).get("event", "")

    @property
    def error_code(self) -> str:
        return (self.terminal or {}).get("code", "") if self.terminal_kind == "error" else ""


def tone(ms: int) -> bytes:
    """PCM16 mono at 16 kHz: a quiet 220 Hz sine, non-silent in every
    window, so a recognizer that finds nothing in it is not lying."""
    samples = AUDIO_SAMPLE_RATE_HZ * ms // 1000
    out = bytearray()
    for i in range(samples):
        value = int(6000 * math.sin(2 * math.pi * 220 * i / AUDIO_SAMPLE_RATE_HZ)) or 1
        out.extend(struct.pack("<h", value))
    return bytes(out)


def silence(ms: int) -> bytes:
    return b"\x00" * (AUDIO_SAMPLE_RATE_HZ * ms // 1000 * 2)


def chunk_audio(pcm: bytes, chunk_ms: int) -> list[bytes]:
    size = AUDIO_SAMPLE_RATE_HZ * chunk_ms // 1000 * 2
    return [pcm[i:i + size] for i in range(0, len(pcm), size)] or [b""]


def totals(chunks: list[bytes]) -> dict:
    total_bytes = sum(len(c) for c in chunks)
    return {
        "frames": len(chunks),
        "bytes": total_bytes,
        "duration_ms": total_bytes * 1000 // BYTES_PER_SECOND,
    }


def start_frame(client_request_id: str, **extra) -> dict:
    frame = {
        "type": "start",
        "protocol": PROTOCOL_VERSION,
        "client_request_id": client_request_id,
        "input": {"type": "audio", "codec": "pcm16", "sample_rate_hz": 16000, "channels": 1},
    }
    frame.update(extra)
    return frame


def request_id(label: str) -> str:
    return f"conform-{label}-{time.time_ns()}"


class Checker:
    def __init__(self, base_url: str, api_key: str = "", allow_insecure: bool = False,
                 verify_tls: bool = True, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.allow_insecure = allow_insecure
        self.verify_tls = verify_tls
        self.timeout = timeout

    # ------------------------------------------------------------- run

    def run(self) -> list[CheckResult]:
        if self.base_url.startswith("http://") and not self.allow_insecure:
            raise ValueError(
                "the base URL is http://; a conforming client refuses one. "
                "Allow insecure to check a loopback backend anyway"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("the base URL must start with http:// or https://")

        results: list[CheckResult] = []

        try:
            health = self._fetch_health(self.api_key)
        except Exception as exc:
            return [CheckResult("health.reachable", FAIL, str(exc))]
        results.append(CheckResult("health.reachable", PASS))

        results.append(
            CheckResult("health.protocol", PASS)
            if health.get("protocol") == PROTOCOL_NAME
            else CheckResult("health.protocol", FAIL,
                             f"protocol is {health.get('protocol')!r}, want {PROTOCOL_NAME!r}")
        )
        status = health.get("status")
        results.append(
            CheckResult("health.status", PASS, str(status))
            if status in ("ok", "degraded", "not_ready")
            else CheckResult("health.status", FAIL, f"status is {status!r}")
        )

        caps = health.get("capabilities")
        if not isinstance(caps, dict):
            results.append(CheckResult("health.capabilities", FAIL, "no capabilities object"))
            return results
        dictate = bool(caps.get(ROUTE_DICTATE))
        ask = bool(caps.get(ROUTE_ASK))
        imagine = bool(caps.get(ROUTE_IMAGINE))
        results.append(CheckResult("health.capabilities", PASS,
                                   f"dictate={dictate} ask={ask} imagine={imagine}"))

        # §2: /dictate is mandatory. A backend that cannot take speech
        # says not_ready and gives a machine-readable reason.
        if not dictate and (status != "not_ready" or not health.get("blockers")):
            results.append(CheckResult(
                "health.dictate_mandatory", FAIL,
                f"dictate is false but status is {status!r} with "
                f"{len(health.get('blockers') or [])} blockers",
            ))
        else:
            results.append(CheckResult("health.dictate_mandatory", PASS))

        results.append(
            CheckResult("health.auth_report", PASS)
            if isinstance(health.get("auth"), dict)
            else CheckResult("health.auth_report", FAIL, "no auth object")
        )

        results.append(self._check_bad_credential())
        if not dictate:
            results.append(CheckResult("dictate.*", SKIP, "backend reports dictate off"))
            return results

        results.extend(self._check_dictate_happy_path(caps))
        results.append(self._check_audio_incomplete())
        results.append(self._check_audio_too_short())
        results.append(self._check_protocol_error())
        results.append(self._check_bad_vocabulary())
        results.append(self._check_unknown_fields())
        results.append(self._check_replay())
        results.append(self._check_silence())
        results.append(self._check_text_input(caps))
        results.extend(self._check_unserved_routes(caps))

        results.append(self._check_ask() if ask
                       else CheckResult("ask.happy_path", SKIP, "backend does not serve /ask"))
        results.append(self._check_imagine() if imagine
                       else CheckResult("imagine.happy_path", SKIP,
                                        "backend does not serve /imagine"))
        return results

    # ------------------------------------------------------------ HTTP

    def _fetch_health(self, key: str) -> dict:
        request = urllib.request.Request(self.base_url + "/health")
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GET /health answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"GET /health: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GET /health is not JSON: {exc}") from exc

    def _ws_url(self, route: str) -> str:
        base = self.base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/" + route
        return "ws://" + base[len("http://"):] + "/" + route

    # ------------------------------------------------------- operations

    def _operate(self, route: str, start: dict | None = None, audio: list | None = None,
                 finalize: dict | None = None, key: str | None = None,
                 raw_first: str | None = None) -> Probe:
        probe = Probe()
        headers = {}
        credential = self.api_key if key is None else key
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        try:
            conn, status, _ = dial(self._ws_url(route), headers=headers,
                                   timeout=self.timeout, verify_tls=self.verify_tls)
        except (OSError, WSError) as exc:
            probe.error = str(exc)
            return probe
        if conn is None:
            probe.http_status = status
            return probe

        stop_sending = threading.Event()
        send_error: list[str] = []

        def send_json(payload: dict) -> None:
            conn.send_text(json.dumps(payload))

        try:
            conn.set_timeout(self.timeout)
            if raw_first is not None:
                conn.send_text(raw_first)
            elif start is not None:
                send_json(start)

            def upload() -> None:
                # §8 lets the server deliver a cached result the moment it
                # says ready and requires the client to accept it and stop
                # sending, so uploading happens alongside reading. A client
                # that uploads before it listens deadlocks against a
                # conforming backend.
                try:
                    for chunk in (audio or []):
                        if stop_sending.is_set():
                            return
                        conn.send_binary(chunk)
                    if finalize is not None and not stop_sending.is_set():
                        send_json(finalize)
                except (OSError, WSError) as exc:
                    send_error.append(str(exc))

            uploader = None
            while True:
                try:
                    binary, data = conn.read_message()
                except CloseError as exc:
                    probe.close_code = exc.code
                    break
                except (OSError, WSError) as exc:
                    probe.error = str(exc)
                    break
                if binary:
                    probe.error = "the server sent a binary frame"
                    break
                try:
                    event = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    probe.error = f"server frame is not JSON: {exc}"
                    break
                probe.events.append(event)
                kind = event.get("event")
                if kind == "ready" and uploader is None:
                    uploader = threading.Thread(target=upload, daemon=True)
                    uploader.start()
                elif kind == "partial":
                    probe.partials.append(event.get("text", ""))
                elif kind in ("result", "error"):
                    if probe.terminal is not None:
                        probe.error = "more than one terminal event"
                        break
                    probe.terminal = event
                    stop_sending.set()
                # Unknown event types are ignored, per §11.
            stop_sending.set()
            if uploader is not None:
                uploader.join(timeout=2.0)
            if probe.terminal is None and send_error and not probe.error:
                probe.error = f"send failed: {send_error[0]}"
        finally:
            try:
                conn.close(1000, "")
            except OSError:
                pass
        return probe

    # ---------------------------------------------------------- checks

    def _check_bad_credential(self) -> CheckResult:
        chunks = chunk_audio(tone(1000), 200)
        probe = self._operate(ROUTE_DICTATE, key="definitely-not-a-valid-credential",
                              start=start_frame(request_id("auth")), audio=chunks,
                              finalize={"type": "finalize", "audio": totals(chunks)})
        if probe.http_status == 401:
            return CheckResult("auth.rejects_bad_credential", PASS,
                               "refused the upgrade with HTTP 401")
        if probe.error_code == ERR_UNAUTHORIZED:
            if probe.close_code == close_code_for(ERR_UNAUTHORIZED):
                return CheckResult("auth.rejects_bad_credential", PASS, "unauthorized / 4401")
            return CheckResult("auth.rejects_bad_credential", FAIL,
                               f"unauthorized but close code {probe.close_code}, want 4401")
        return CheckResult("auth.rejects_bad_credential", FAIL,
                           f"a bogus credential was not refused "
                           f"({probe.terminal_kind!r} {probe.error_code!r})")

    def _check_dictate_happy_path(self, caps: dict) -> list[CheckResult]:
        chunks = chunk_audio(tone(3000), 200)
        probe = self._operate(
            ROUTE_DICTATE,
            start=start_frame(request_id("dictate"), vocabulary=["Sagrada Familia"]),
            audio=chunks,
            finalize={"type": "finalize", "audio": totals(chunks), "polish": True},
        )
        if probe.error:
            return [CheckResult("dictate.happy_path", FAIL, probe.error)]
        if probe.terminal_kind != "result":
            return [CheckResult("dictate.happy_path", FAIL,
                                f"terminal event was {probe.terminal_kind!r} {probe.error_code!r}")]

        out = []
        result = probe.terminal.get("result") or {}
        out.append(CheckResult("dictate.result_type", PASS) if result.get("type") == "dictation"
                   else CheckResult("dictate.result_type", FAIL,
                                    f"result.type is {result.get('type')!r}"))
        text = result.get("text") or ""
        out.append(CheckResult("dictate.result_text", PASS, f"{len(text)} characters")
                   if text.strip() else CheckResult("dictate.result_text", FAIL, "empty"))
        out.append(CheckResult("dictate.raw_transcript", PASS)
                   if isinstance(result.get("raw_transcript"), str)
                   else CheckResult("dictate.raw_transcript", FAIL, "missing"))
        route = result.get("stt_route")
        out.append(CheckResult("dictate.stt_route", PASS, str(route))
                   if route in ("stream", "fallback")
                   else CheckResult("dictate.stt_route", FAIL,
                                    f"stt_route is {route!r}, want stream or fallback"))
        out.append(CheckResult("dictate.close_code", PASS) if probe.close_code == 1000
                   else CheckResult("dictate.close_code", FAIL,
                                    f"close code {probe.close_code}, want 1000"))
        out.append(CheckResult("dictate.request_id", PASS) if probe.terminal.get("request_id")
                   else CheckResult("dictate.request_id", FAIL, "no request_id"))

        terminal_index = next(
            (i for i, e in enumerate(probe.events) if e.get("event") in ("result", "error")), -1
        )
        out.append(CheckResult("dictate.terminal_is_last", PASS)
                   if terminal_index == len(probe.events) - 1
                   else CheckResult("dictate.terminal_is_last", FAIL,
                                    f"{len(probe.events) - 1 - terminal_index} events followed it"))

        advertised = (caps.get("partials") or {}).get(ROUTE_DICTATE)
        if advertised and not probe.partials:
            out.append(CheckResult("dictate.partials", FAIL,
                                   "partials are advertised on /dictate but none arrived"))
        elif not probe.partials:
            out.append(CheckResult("dictate.partials", SKIP, "no partials advertised"))
        else:
            regressed = next(
                (i for i in range(1, len(probe.partials))
                 if not probe.partials[i].startswith(probe.partials[i - 1])
                 and len(probe.partials[i]) < len(probe.partials[i - 1])),
                None,
            )
            out.append(CheckResult("dictate.partials_cumulative", FAIL,
                                   f"partial {regressed} is shorter than the one before it")
                       if regressed is not None
                       else CheckResult("dictate.partials_cumulative", PASS,
                                        f"{len(probe.partials)} partials"))
        return out

    def _check_audio_incomplete(self) -> CheckResult:
        chunks = chunk_audio(tone(2000), 200)
        lie = totals(chunks)
        lie["frames"] += 3
        probe = self._operate(ROUTE_DICTATE, start=start_frame(request_id("incomplete")),
                              audio=chunks, finalize={"type": "finalize", "audio": lie})
        if probe.error_code != ERR_AUDIO_INCOMPLETE:
            return CheckResult("reliability.audio_incomplete", FAIL,
                               f"disagreeing finalize totals produced {probe.error_code!r}")
        if not probe.terminal.get("retryable"):
            return CheckResult("reliability.audio_incomplete", FAIL,
                               "audio_incomplete must be retryable")
        if probe.close_code != close_code_for(ERR_AUDIO_INCOMPLETE):
            return CheckResult("reliability.audio_incomplete", FAIL,
                               f"close code {probe.close_code}, want 4409")
        return CheckResult("reliability.audio_incomplete", PASS)

    def _check_audio_too_short(self) -> CheckResult:
        chunks = chunk_audio(tone(100), 50)
        probe = self._operate(ROUTE_DICTATE, start=start_frame(request_id("short")),
                              audio=chunks, finalize={"type": "finalize", "audio": totals(chunks)})
        if probe.error_code != ERR_AUDIO_TOO_SHORT:
            return CheckResult("bounds.audio_too_short", FAIL,
                               f"100 ms of audio produced {probe.error_code!r}")
        if probe.close_code != close_code_for(ERR_AUDIO_TOO_SHORT):
            return CheckResult("bounds.audio_too_short", FAIL,
                               f"close code {probe.close_code}, want 4422")
        return CheckResult("bounds.audio_too_short", PASS)

    def _check_protocol_error(self) -> CheckResult:
        probe = self._operate(ROUTE_DICTATE, raw_first='{"type":"finalize"}')
        if probe.error_code != ERR_PROTOCOL_ERROR:
            return CheckResult("errors.protocol_error", FAIL,
                               f"finalize as the first frame produced {probe.error_code!r}")
        if probe.close_code != close_code_for(ERR_PROTOCOL_ERROR):
            return CheckResult("errors.protocol_error", FAIL,
                               f"close code {probe.close_code}, want 4400")
        return CheckResult("errors.protocol_error", PASS)

    def _check_bad_vocabulary(self) -> CheckResult:
        chunks = chunk_audio(tone(1000), 200)
        probe = self._operate(ROUTE_DICTATE,
                              start=start_frame(request_id("vocab"), vocabulary=["x" * 200]),
                              audio=chunks,
                              finalize={"type": "finalize", "audio": totals(chunks)})
        if probe.error_code != ERR_BAD_REQUEST:
            return CheckResult("errors.bad_request", FAIL,
                               f"a 200-character vocabulary entry produced {probe.error_code!r}")
        return CheckResult("errors.bad_request", PASS)

    def _check_unknown_fields(self) -> CheckResult:
        """§11: unknown JSON fields are ignored everywhere. A backend that
        rejects them breaks every future additive change."""
        chunks = chunk_audio(tone(1500), 200)
        probe = self._operate(
            ROUTE_DICTATE,
            start=start_frame(request_id("unknown"), a_field_from_protocol_5="ignore me"),
            audio=chunks,
            finalize={"type": "finalize", "audio": totals(chunks), "some_future_finalizer": 12},
        )
        if probe.terminal_kind != "result":
            return CheckResult("forward_compat.unknown_fields", FAIL,
                               f"unknown fields produced {probe.terminal_kind!r} "
                               f"{probe.error_code!r}")
        return CheckResult("forward_compat.unknown_fields", PASS)

    def _check_replay(self) -> CheckResult:
        """§8's idempotency rule from the client's side: the same
        client_request_id, twice, must not produce two different
        answers."""
        identifier = request_id("replay")
        chunks = chunk_audio(tone(2000), 200)
        kwargs = dict(start=start_frame(identifier), audio=chunks,
                      finalize={"type": "finalize", "audio": totals(chunks)})
        first = self._operate(ROUTE_DICTATE, **kwargs)
        if first.terminal_kind != "result":
            return CheckResult("reliability.replay", FAIL,
                               f"the first attempt failed with {first.error_code!r}")
        second = self._operate(ROUTE_DICTATE, **kwargs)
        if second.terminal_kind != "result":
            return CheckResult("reliability.replay", FAIL,
                               f"the replay failed with {second.error_code!r} {second.error}")
        if (first.terminal["result"] or {}).get("text") != (second.terminal["result"] or {}).get("text"):
            return CheckResult("reliability.replay", FAIL,
                               "the same client_request_id and audio produced different text")
        return CheckResult("reliability.replay", PASS)

    def _check_silence(self) -> CheckResult:
        """Advisory: §8 says an empty transcript from a healthy recognizer
        is no_speech_detected, but a real recognizer handed digital
        silence may still hallucinate a word, and that is its business."""
        chunks = chunk_audio(silence(1500), 200)
        probe = self._operate(ROUTE_DICTATE, start=start_frame(request_id("silence")),
                              audio=chunks, finalize={"type": "finalize", "audio": totals(chunks)})
        if probe.error_code == ERR_NO_SPEECH_DETECTED:
            return CheckResult("errors.no_speech_detected", PASS)
        return CheckResult("errors.no_speech_detected", WARN,
                           f"1.5 s of digital silence produced {probe.terminal_kind!r} "
                           f"{probe.error_code!r}")

    def _check_text_input(self, caps: dict) -> CheckResult:
        supported = (caps.get("text_input") or {}).get(ROUTE_DICTATE)
        start = {
            "type": "start",
            "protocol": PROTOCOL_VERSION,
            "client_request_id": request_id("text"),
            "input": {"type": "text", "text": "lets push the review to thursday"},
        }
        probe = self._operate(ROUTE_DICTATE, start=start, finalize={"type": "finalize"})
        if supported is False:
            if probe.error_code != ERR_NOT_SUPPORTED:
                return CheckResult("dictate.text_input", FAIL,
                                   f"text_input is advertised false but text input produced "
                                   f"{probe.error_code!r}")
            return CheckResult("dictate.text_input", PASS, "honestly refused")
        if probe.terminal_kind != "result":
            return CheckResult("dictate.text_input", FAIL,
                               f"text input produced {probe.terminal_kind!r} {probe.error_code!r}")
        if "audio" in probe.terminal:
            return CheckResult("dictate.text_input", FAIL,
                               "the result carries an audio object for text input")
        return CheckResult("dictate.text_input", PASS)

    def _check_unserved_routes(self, caps: dict) -> list[CheckResult]:
        """Hold the backend to its own health document: a route it says it
        does not serve must answer not_supported rather than half-work."""
        out = []
        for route in (ROUTE_ASK, ROUTE_IMAGINE):
            if caps.get(route):
                continue
            probe = self._operate(route, start=start_frame(request_id("unserved")))
            name = f"capabilities.unserved_{route}"
            if probe.http_status == 404:
                out.append(CheckResult(name, PASS, "HTTP 404 on upgrade"))
            elif probe.error_code == ERR_NOT_SUPPORTED:
                out.append(CheckResult(name, PASS, "not_supported"))
            else:
                out.append(CheckResult(name, FAIL,
                                       f"/{route} is advertised off but answered "
                                       f"{probe.terminal_kind!r} {probe.error_code!r}"))
        return out

    def _check_ask(self) -> CheckResult:
        start = {
            "type": "start",
            "protocol": PROTOCOL_VERSION,
            "client_request_id": request_id("ask"),
            "input": {"type": "text", "text": "tell sam i'm running ten minutes late"},
        }
        probe = self._operate(ROUTE_ASK, start=start,
                              finalize={"type": "finalize", "visible_text": ""})
        if probe.terminal_kind != "result":
            return CheckResult("ask.happy_path", FAIL,
                               f"terminal event was {probe.terminal_kind!r} {probe.error_code!r}")
        result = probe.terminal.get("result") or {}
        if result.get("type") != "message":
            return CheckResult("ask.happy_path", FAIL, f"result.type is {result.get('type')!r}")
        if not (result.get("text") or "").strip():
            return CheckResult("ask.happy_path", FAIL, "result.text is empty")
        return CheckResult("ask.happy_path", PASS)

    def _check_imagine(self) -> CheckResult:
        start = {
            "type": "start",
            "protocol": PROTOCOL_VERSION,
            "client_request_id": request_id("imagine"),
            "input": {"type": "text", "text": "a lighthouse at dusk in watercolour"},
        }
        probe = self._operate(ROUTE_IMAGINE, start=start,
                              finalize={"type": "finalize", "aspect_ratio": "3:2",
                                        "quality": "low"})
        if probe.terminal_kind != "result":
            return CheckResult("imagine.happy_path", FAIL,
                               f"terminal event was {probe.terminal_kind!r} {probe.error_code!r}")
        result = probe.terminal.get("result") or {}
        if result.get("type") != "image":
            return CheckResult("imagine.happy_path", FAIL, f"result.type is {result.get('type')!r}")
        try:
            data = base64.b64decode(result.get("data_base64", ""), validate=True)
        except (ValueError, TypeError):
            return CheckResult("imagine.happy_path", FAIL, "data_base64 is not base64")
        # §6: byte_length and sha256 describe the delivered bytes, and
        # clients verify them. So does this checker.
        if result.get("byte_length") != len(data):
            return CheckResult("imagine.happy_path", FAIL,
                               f"byte_length is {result.get('byte_length')}, "
                               f"delivered {len(data)} bytes")
        if str(result.get("sha256", "")).lower() != hashlib.sha256(data).hexdigest():
            return CheckResult("imagine.happy_path", FAIL,
                               "sha256 does not match the delivered bytes")
        return CheckResult("imagine.happy_path", PASS)


def report(results: list[CheckResult], stream) -> bool:
    """Write the human-readable summary; return whether every required
    check passed. Warnings never fail the run."""
    counts = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        line = f"{result.status.upper():<6} {result.name}"
        if result.detail:
            line += f"  — {result.detail}"
        print(line, file=stream)
    print(f"\n{counts[PASS]} passed, {counts[FAIL]} failed, "
          f"{counts[WARN]} warnings, {counts[SKIP]} skipped", file=stream)
    return counts[FAIL] == 0


__all__ = ["Checker", "CheckResult", "Probe", "report", "retryable_for",
           "tone", "silence", "chunk_audio", "totals", "start_frame", "request_id"]
