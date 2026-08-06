"""caret/v1 over `http.server`. Stdlib only.

The whole contract lives here: routing, auth, validation, the one input
model, the async job semantics, and the error envelope. Roughly the order
you would implement it in yourself.

Put a real reverse proxy in front of this in production — it terminates TLS
and it is what the contract means by "https:// is required". This process
speaks plain HTTP and never tries to be an edge server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import adapters
from .errors import (
    CaretError,
    bad_request,
    input_invalid,
    internal_error,
    new_request_id,
    session_conflict,
    session_expired,
    unauthorized,
    unknown_session,
)
from .store import SESSION_TTL_SECONDS, Store, start_janitor

log = logging.getLogger("caret")

CONTRACT = "caret/v1"
SERVICE = "caret-reference-backend"
VERSION = "1.1.0"

CHUNK_MAX_BYTES = 524_288
CHUNK_TARGET_DURATION_MS = 3_000
MAX_JSON_BYTES = 1_048_576
# Past this, an oversized body is hung up on rather than read to the end.
DRAIN_LIMIT_BYTES = 4_194_304
MAX_TEXT_CHARS = 4_000
MAX_APP_HINT_CHARS = 200
RETRY_AFTER_SECONDS = 3

ASPECT_RATIOS = ("square", "landscape", "portrait")
QUALITIES = ("fast", "standard", "best")

# Sample rate is fixed by the contract; every duration here derives from it.
SAMPLE_RATE_HZ = 16_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ limiter


class RateLimiter:
    """Fixed-window counter per API key. Small, but not nothing.

    A BYOB backend is usually one user and one phone, so the ceiling exists
    to stop a looping client from burning the operator's model budget, not
    to police tenants.
    """

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        bucket = int(time.time() // self.window)
        with self._lock:
            for existing in [k for k in self._counts if k[1] != bucket]:
                del self._counts[existing]
            count = self._counts.get((key, bucket), 0) + 1
            self._counts[(key, bucket)] = count
        if count > self.limit:
            raise CaretError(
                429,
                "rate_limited",
                f"more than {self.limit} requests in {self.window}s",
                retryable=True,
            )


# ------------------------------------------------------------------ backend


class Backend:
    """Contract logic, independent of the HTTP plumbing (so it is testable)."""

    def __init__(
        self,
        *,
        store: Store,
        agent,
        transcriber,
        image_generator=None,
        api_keys: tuple[str, ...] = (),
        polish_enabled: bool = True,
        rate_limit_per_minute: int = 120,
        run_jobs_inline: bool = False,
    ) -> None:
        self.store = store
        self.agent = agent
        self.transcriber = transcriber
        self.image_generator = image_generator
        self.api_keys = tuple(api_keys)
        self.polish_enabled = polish_enabled
        self.limiter = RateLimiter(rate_limit_per_minute)
        # Tests run jobs synchronously so a poll loop is not needed to
        # observe a terminal result; production always uses a thread.
        self.run_jobs_inline = run_jobs_inline

    # ------------------------------------------------------------ capability

    @property
    def dictation_available(self) -> bool:
        return not isinstance(self.transcriber, adapters.NullTranscriber)

    @property
    def imagine_available(self) -> bool:
        return self.image_generator is not None

    def capabilities(self) -> dict:
        text_modes = ["text"] + (["audio"] if self.dictation_available else [])
        modes = {"draft": text_modes}
        if self.imagine_available:
            modes["imagine"] = text_modes
        if self.dictation_available:
            modes["dictation"] = ["audio"]
        return {
            "draft": True,
            "dictation": self.dictation_available,
            "imagine": self.imagine_available,
            "input_modes": modes,
        }

    # ----------------------------------------------------------------- auth

    def authenticate(self, header: str | None) -> bool:
        """Constant-time bearer check. No keys configured means fail closed."""
        if not self.api_keys or not header:
            return False
        prefix = "bearer "
        if not header.lower().startswith(prefix):
            return False
        presented = header[len(prefix):].strip()
        return any(hmac.compare_digest(presented, key) for key in self.api_keys)

    def require_auth(self, header: str | None) -> str:
        if not self.authenticate(header):
            raise unauthorized()
        return header.split(" ", 1)[1].strip()

    # --------------------------------------------------------------- health

    def health(self, auth_header: str | None, request_id: str) -> dict:
        presented = bool(auth_header)
        valid = self.authenticate(auth_header) if presented else None
        return {
            "status": "ok" if self.api_keys else "degraded",
            "service": SERVICE,
            "contract": CONTRACT,
            "version": VERSION,
            "time": _now_iso(),
            "auth": {"presented": presented, "valid": valid},
            "capabilities": self.capabilities(),
            "request_id": request_id,
        }

    # ---------------------------------------------------------- input model

    def parse_input(self, payload: dict, *, alias: str, alias_code: str) -> tuple[str, object]:
        """Resolve the shared `input` object, or its 1.0 alias.

        Exactly one of them. Both, or neither, is `422 input_invalid` — the
        alternative is guessing which one the client meant, and guessing
        wrong is worse than a clear error.
        """
        raw = payload.get("input")
        alias_value = payload.get(alias)
        if raw is not None and alias_value is not None:
            raise input_invalid(f"provide exactly one of input or {alias}")
        if raw is None and alias_value is None:
            raise input_invalid(f"provide input or {alias}")

        if raw is None:
            return "text", self._text(alias_value, field=alias, code=alias_code)
        if not isinstance(raw, dict):
            raise input_invalid("input must be an object")
        kind = raw.get("type")
        if kind == "text":
            # The alias keeps its 1.0 error code so existing clients keep
            # matching on it; the new object reports the new code.
            return "text", self._text(raw.get("text"), field="input.text", code="input_invalid")
        if kind == "audio":
            return "audio", self._audio(raw)
        raise input_invalid('input.type must be "text" or "audio"')

    @staticmethod
    def _text(value, *, field: str, code: str) -> str:
        if not isinstance(value, str):
            raise input_invalid(f"{field} must be a string")
        if not 1 <= len(value) <= MAX_TEXT_CHARS:
            raise CaretError(422, code, f"{field} must be 1-{MAX_TEXT_CHARS} characters")
        return value

    def _audio(self, raw: dict) -> dict:
        if not self.dictation_available:
            raise CaretError(
                422,
                "unsupported_input_type",
                "this backend has no transcriber configured; send text input",
            )
        session_id = raw.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise input_invalid("input.session_id must be a non-empty string")
        polish = raw.get("polish", True)
        if not isinstance(polish, bool):
            raise bad_request("input.polish must be a boolean")
        return {
            "session_id": session_id,
            "client_chunk_count": _positive_int(raw, "client_chunk_count"),
            "client_total_duration_ms": _positive_int(raw, "client_total_duration_ms"),
            "polish": polish,
        }

    # -------------------------------------------------------------- sessions

    def create_session(self, payload: dict, request_id: str) -> dict:
        client_request_id = _client_request_id(payload)
        codec = payload.get("codec")
        if not isinstance(codec, str) or not codec:
            raise bad_request("codec is required")
        if codec != "pcm16":
            raise CaretError(415, "unsupported_audio_codec", "v1 supports pcm16 only")
        if payload.get("sample_rate_hz") != SAMPLE_RATE_HZ:
            raise bad_request(f"sample_rate_hz must be {SAMPLE_RATE_HZ}")
        if payload.get("channels") != 1:
            raise bad_request("channels must be 1")
        intent = payload.get("intent", "dictate")
        if intent is not None and intent not in ("dictate", "ask", "imagine"):
            raise bad_request("intent must be dictate, ask or imagine")
        # `intent` is advisory by contract: reject early only for something
        # this backend genuinely cannot do, never merely because the eventual
        # consumer might differ.
        if intent == "imagine" and not self.imagine_available:
            raise CaretError(
                422, "unsupported_input_type", "this backend does not implement imagine"
            )
        if not self.dictation_available:
            raise CaretError(
                422, "unsupported_input_type", "this backend has no transcriber configured"
            )

        record = self.store.create_session(
            client_request_id=client_request_id,
            meta={
                "codec": codec,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "channels": 1,
                "intent": intent,
                "app_hint": _optional_string(payload, "app_hint", MAX_APP_HINT_CHARS),
                "language_hint": _optional_string(payload, "language_hint", 16),
            },
        )
        return {
            "session_id": record["session_id"],
            "chunk_max_bytes": CHUNK_MAX_BYTES,
            "chunk_target_duration_ms": CHUNK_TARGET_DURATION_MS,
            "expires_at": datetime.fromtimestamp(
                record["expires_at"], tz=timezone.utc
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "request_id": request_id,
        }

    def _live_session(self, session_id: str) -> dict:
        meta = self.store.read_session(session_id)
        if meta is None:
            raise unknown_session(session_id)
        if time.time() > meta["expires_at"]:
            raise session_expired()
        return meta

    def put_chunk(
        self,
        session_id: str,
        seq: int,
        body: bytes,
        *,
        declared_sha256: str | None,
        duration_ms: int | None,
        request_id: str,
    ) -> dict:
        meta = self._live_session(session_id)
        if meta.get("completed"):
            # Claiming a session is not closing it: a `missing_chunks` reply
            # binds the session to a surface and then explicitly asks for
            # more audio. Only a terminal result seals it.
            raise session_conflict(meta.get("consumed_by") or "another request")
        if seq < 0:
            raise bad_request("seq must be >= 0")
        if len(body) > CHUNK_MAX_BYTES:
            raise CaretError(
                413, "chunk_too_large", f"chunk exceeds {CHUNK_MAX_BYTES} bytes"
            )
        if not body:
            raise bad_request("chunk body is empty")
        if not declared_sha256:
            raise bad_request("X-Caret-Chunk-SHA256 is required")

        actual = _sha256_hex(body)
        if actual != declared_sha256.lower():
            # The body does not match its own header: a transport problem,
            # so it is retryable.
            raise CaretError(
                409,
                "chunk_checksum_mismatch",
                "body does not match X-Caret-Chunk-SHA256",
                retryable=True,
            )
        stored = self.store.chunk_sha256(session_id, seq)
        if stored is not None and stored != actual:
            # Two *different* chunks for one sequence number. Retrying will
            # not help; the client has a bug.
            raise CaretError(
                409, "chunk_seq_conflict", f"seq {seq} already holds different audio"
            )

        duplicate = self.store.put_chunk(
            session_id,
            seq,
            body,
            sha256=actual,
            duration_ms=duration_ms or 0,
        )
        return {
            "session_id": session_id,
            "seq": seq,
            "accepted": True,
            "duplicate": duplicate,
            "request_id": request_id,
        }

    def claim_session(self, session_id: str, surface: str) -> dict:
        """Bind a session to the one surface that will consume it."""
        meta = self._live_session(session_id)
        consumed_by = meta.get("consumed_by")
        if consumed_by and consumed_by != surface:
            raise session_conflict(consumed_by)
        if not consumed_by:
            meta = self.store.update_session(session_id, consumed_by=surface) or meta
        return meta

    # ------------------------------------------------------------- job model

    def _poll_or_start(self, key: str, surface: str, work, request_id: str) -> dict:
        """The async contract in one place.

        First call registers the job and starts it; every identical re-post
        finds the same record, so `request_id` is stable from the first 202
        through the terminal response. Terminal results — success *and*
        failure — stay cached against the key, so polling never re-runs work
        and a failed generation is not silently retried at the operator's
        expense.
        """
        job = self.store.claim_job(key, request_id=request_id, surface=surface)
        if job["state"] == "running" and job["request_id"] == request_id:
            # We just created it (a poll would have found an older id).
            if self.run_jobs_inline:
                self._execute(key, work)
                job = self.store.read_job(key) or job
            else:
                threading.Thread(
                    target=self._execute,
                    args=(key, work),
                    name=f"caret-job-{surface}",
                    daemon=True,
                ).start()
        if job["state"] == "running":
            return {
                "_status": 202,
                "status": "in_progress",
                "started_at": datetime.fromtimestamp(
                    job["started_at"], tz=timezone.utc
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "elapsed_seconds": round(time.time() - job["started_at"], 1),
                "retry_after_seconds": RETRY_AFTER_SECONDS,
                "request_id": job["request_id"],
            }
        if job["state"] == "failed":
            stored = job["error"]
            raise CaretError(
                stored["status"],
                stored["code"],
                stored["message"],
                retryable=stored["retryable"],
                request_id=job["request_id"],
            )
        result = dict(job["result"])
        result["request_id"] = job["request_id"]
        return result

    def _execute(self, key: str, work) -> None:
        try:
            result = work()
        except CaretError as exc:
            self.store.finish_job(
                key,
                result=None,
                error={
                    "status": exc.status,
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            log.exception("job %s failed", key)
            self.store.finish_job(
                key,
                result=None,
                error={
                    "status": 500,
                    "code": "internal_error",
                    "message": f"unexpected failure: {exc.__class__.__name__}",
                    "retryable": True,
                },
            )
        else:
            self.store.finish_job(key, result=result, error=None)

    # --------------------------------------------------------------- audio

    def _transcribe_session(self, audio: dict, surface: str) -> dict:
        """Shared by Dictate, spoken Ask and spoken Imagine. One code path."""
        session_id = audio["session_id"]
        meta = self.claim_session(session_id, surface)
        pcm = self.store.read_audio(session_id, audio["client_chunk_count"])
        text = self.transcriber.transcribe(pcm, sample_rate=meta["sample_rate_hz"])
        if not text.strip():
            raise CaretError(422, "no_speech_detected", "no speech in the recording")
        if audio["polish"] and self.polish_enabled:
            try:
                text = self.agent.polish(text)
            except CaretError as exc:
                # A polish failure must not lose a good transcript.
                log.warning("polish failed (%s); returning raw transcript", exc.code)
        duration_ms = int(len(pcm) / 2 / meta["sample_rate_hz"] * 1000)
        return {"transcript": text, "duration_ms": duration_ms}

    def _seal_session(self, session_id: str) -> None:
        """A terminal result closes the session and drops its audio."""
        self.store.update_session(session_id, completed=True)
        self.store.delete_session_audio(session_id)

    def _audio_precheck(self, audio: dict, surface: str, request_id: str) -> dict | None:
        """Completeness first: gaps are a 200, not an error, so the client
        can re-upload exactly what is missing and call again."""
        session_id = audio["session_id"]
        self.claim_session(session_id, surface)
        missing = self.store.missing_chunks(session_id, audio["client_chunk_count"])
        if missing:
            return {
                "status": "missing_chunks",
                "session_id": session_id,
                "missing_chunks": missing,
                "request_id": request_id,
            }
        return None

    # ---------------------------------------------------------------- draft

    def draft(self, payload: dict, request_id: str) -> dict:
        client_request_id = _client_request_id(payload)
        input_type, value = self.parse_input(
            payload, alias="instruction", alias_code="instruction_invalid"
        )
        visible_text = _optional_string(payload, "visible_text", MAX_TEXT_CHARS)
        app_hint = _optional_string(payload, "app_hint", MAX_APP_HINT_CHARS)

        if input_type == "text":
            # Ask is synchronous by contract. Cache successes only: a failed
            # attempt must stay retryable under the same id.
            cache_key = f"draft:{client_request_id}"
            cached = self.store.read_idempotent(cache_key)
            if cached:
                return cached
            text = self.agent.draft(
                instruction=value, visible_text=visible_text, app_hint=app_hint
            )
            body = {
                "text": text,
                "status": "complete",
                "input_type": "text",
                "request_id": request_id,
            }
            self.store.write_idempotent(cache_key, body)
            return body

        precheck = self._audio_precheck(value, "draft", request_id)
        if precheck:
            return {"text": None, "input_type": "audio", **precheck}

        def work() -> dict:
            heard = self._transcribe_session(value, "draft")
            text = self.agent.draft(
                instruction=heard["transcript"],
                visible_text=visible_text,
                app_hint=app_hint,
            )
            self._seal_session(value["session_id"])
            return {
                "text": text,
                "status": "complete",
                "input_type": "audio",
                "session_id": value["session_id"],
                "transcript": heard["transcript"],
                "missing_chunks": [],
                "duration_ms": heard["duration_ms"],
            }

        return self._poll_or_start(f"draft:{value['session_id']}", "draft", work, request_id)

    # -------------------------------------------------------------- imagine

    def imagine(self, payload: dict, request_id: str) -> dict:
        if not self.imagine_available:
            raise CaretError(
                404,
                "not_found",
                "this backend does not implement imagine",
            )
        client_request_id = _client_request_id(payload)
        input_type, value = self.parse_input(
            payload, alias="prompt", alias_code="prompt_invalid"
        )
        aspect_ratio = _enum(payload, "aspect_ratio", ASPECT_RATIOS, "unsupported_aspect_ratio")
        quality = _enum(payload, "quality", QUALITIES, "unsupported_quality")

        if input_type == "text":
            def work() -> dict:
                return self._generate(value, aspect_ratio, quality, input_type="text")

            return self._poll_or_start(
                f"imagine:{client_request_id}", "imagine", work, request_id
            )

        precheck = self._audio_precheck(value, "imagine", request_id)
        if precheck:
            return {"input_type": "audio", **precheck}

        def work() -> dict:
            heard = self._transcribe_session(value, "imagine")
            body = self._generate(
                heard["transcript"], aspect_ratio, quality, input_type="audio"
            )
            body["session_id"] = value["session_id"]
            body["transcript"] = heard["transcript"]
            body["missing_chunks"] = []
            self._seal_session(value["session_id"])
            return body

        return self._poll_or_start(
            f"imagine:{value['session_id']}", "imagine", work, request_id
        )

    def _generate(self, prompt: str, aspect_ratio: str, quality: str, *, input_type: str) -> dict:
        png = self.image_generator.generate(
            prompt, aspect_ratio=aspect_ratio, quality=quality
        )
        return {
            "status": "complete",
            "input_type": input_type,
            "media": {
                "kind": "image",
                "mime_type": "image/png",
                "filename": "caret-imagine.png",
                "byte_length": len(png),
                "sha256": _sha256_hex(png),
                "inline_base64": base64.b64encode(png).decode("ascii"),
            },
            "provider": getattr(self.image_generator, "name", "custom"),
            "aspect_ratio": aspect_ratio,
            "quality": quality,
        }

    # ------------------------------------------------------------ transcript

    def transcript(self, session_id: str, payload: dict, request_id: str) -> dict:
        polish = payload.get("polish", True)
        if not isinstance(polish, bool):
            raise bad_request("polish must be a boolean")
        audio = {
            "session_id": session_id,
            "client_chunk_count": _positive_int(payload, "client_chunk_count"),
            "client_total_duration_ms": _positive_int(payload, "client_total_duration_ms"),
            "polish": polish,
        }
        precheck = self._audio_precheck(audio, "dictation", request_id)
        if precheck:
            return {"text": None, **precheck}

        def work() -> dict:
            heard = self._transcribe_session(audio, "dictation")
            self._seal_session(session_id)
            return {
                "session_id": session_id,
                "status": "complete",
                "text": heard["transcript"],
                "missing_chunks": [],
                "duration_ms": heard["duration_ms"],
            }

        return self._poll_or_start(
            f"dictation:{session_id}", "dictation", work, request_id
        )


# ------------------------------------------------------------ small helpers


def _client_request_id(payload: dict) -> str:
    value = payload.get("client_request_id")
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise bad_request("client_request_id must be a string of 1-128 characters")
    return value


def _optional_string(payload: dict, field: str, max_len: int) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request(f"{field} must be a string")
    return value[:max_len]


def _positive_int(payload: dict, field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise bad_request(f"{field} must be an integer")
    if value < 1:
        raise bad_request(f"{field} must be >= 1")
    return value


def _enum(payload: dict, field: str, allowed: tuple[str, ...], code: str) -> str:
    value = payload.get(field)
    if value is None:
        return allowed[0]
    if not isinstance(value, str) or value not in allowed:
        raise CaretError(422, code, f"{field} must be one of {', '.join(allowed)}")
    return value


# --------------------------------------------------------------- http layer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "caret-reference/1.1"
    sys_version = ""

    backend: Backend  # injected by make_server

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)

    def _body(self, limit: int, *, code: str) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise bad_request("invalid Content-Length") from None
        if length < 0:
            raise bad_request("invalid Content-Length")
        if length > limit:
            # Drain a bounded amount so the client's in-flight write does not
            # deadlock against an unread socket, then hang up: past the cap
            # there is no reason to keep reading.
            remaining = min(length, DRAIN_LIMIT_BYTES)
            while remaining > 0:
                remaining -= len(self.rfile.read(min(remaining, 65536)) or b"")
            self.close_connection = True
            raise CaretError(413, code, f"body exceeds {limit} bytes")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict:
        raw = self._body(MAX_JSON_BYTES, code="bad_request")
        if not raw:
            raise bad_request("request body is required")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise bad_request("request body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise bad_request("request body must be a JSON object")
        return payload

    def _respond(self, status: int, body: dict, *, extra_headers: dict | None = None) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method: str) -> None:
        request_id = new_request_id()
        path = urlparse(self.path).path.rstrip("/") or "/"
        client = self.headers.get("X-Caret-Client", "-")
        try:
            status, body, headers = self._route(method, path, request_id)
        except CaretError as exc:
            log.warning(
                "%s %s -> %s %s (%s) client=%s",
                method, path, exc.status, exc.code, request_id, client,
            )
            self._respond(exc.status, exc.body(request_id))
            return
        except Exception:  # noqa: BLE001 - an escaped bug is still an envelope
            log.exception("%s %s failed (%s)", method, path, request_id)
            self._respond(500, internal_error().body(request_id))
            return
        log.info("%s %s -> %s (%s) client=%s", method, path, status, request_id, client)
        self._respond(status, body, extra_headers=headers)

    do_GET = lambda self: self._dispatch("GET")  # noqa: E731
    do_POST = lambda self: self._dispatch("POST")  # noqa: E731
    do_PUT = lambda self: self._dispatch("PUT")  # noqa: E731

    # -- routing ----------------------------------------------------------

    def _route(self, method: str, path: str, request_id: str) -> tuple[int, dict, dict]:
        backend = self.backend
        auth_header = self.headers.get("Authorization")

        if method == "GET" and path == "/v1/health":
            return 200, backend.health(auth_header, request_id), {}

        key = backend.require_auth(auth_header)
        backend.limiter.check(key)

        if method == "POST" and path == "/v1/draft":
            return self._async_aware(backend.draft(self._json_body(), request_id))
        if method == "POST" and path == "/v1/imagine":
            return self._async_aware(backend.imagine(self._json_body(), request_id))
        if method == "POST" and path == "/v1/dictation/sessions":
            return 200, backend.create_session(self._json_body(), request_id), {}

        parts = path.strip("/").split("/")
        # v1 / dictation / sessions / {id} / …
        if len(parts) >= 4 and parts[:3] == ["v1", "dictation", "sessions"]:
            session_id = parts[3]
            if method == "PUT" and len(parts) == 6 and parts[4] == "chunks":
                try:
                    seq = int(parts[5])
                except ValueError:
                    raise bad_request("seq must be an integer") from None
                body = self._body(CHUNK_MAX_BYTES, code="chunk_too_large")
                duration = self.headers.get("X-Caret-Chunk-Duration-Ms")
                return 200, backend.put_chunk(
                    session_id,
                    seq,
                    body,
                    declared_sha256=self.headers.get("X-Caret-Chunk-SHA256"),
                    duration_ms=int(duration) if (duration or "").isdigit() else None,
                    request_id=request_id,
                ), {}
            if method == "POST" and len(parts) == 5 and parts[4] == "transcript":
                return self._async_aware(
                    backend.transcript(session_id, self._json_body(), request_id)
                )

        raise CaretError(404, "not_found", f"no route for {method} {path}")

    @staticmethod
    def _async_aware(body: dict) -> tuple[int, dict, dict]:
        if body.pop("_status", 200) == 202:
            return 202, body, {"Retry-After": str(body["retry_after_seconds"])}
        return 200, body, {}


def make_server(backend: Backend, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    return ThreadingHTTPServer((host, port), handler)


def backend_from_env(env: dict[str, str] | None = None) -> Backend:
    env = os.environ if env is None else env
    data_dir = Path(env.get("CARET_DATA_DIR", Path.home() / ".caret-reference" / "data"))
    keys = tuple(k.strip() for k in env.get("CARET_API_KEYS", "").split(",") if k.strip())
    if not keys:
        log.warning(
            "CARET_API_KEYS is empty: every authenticated request will return 401 "
            "and /v1/health will report degraded"
        )
    store = Store(data_dir)
    start_janitor(store)
    return Backend(
        store=store,
        agent=adapters.agent_from_env(env),
        transcriber=adapters.transcriber_from_env(env),
        image_generator=adapters.image_generator_from_env(env),
        api_keys=keys,
        polish_enabled=env.get("CARET_POLISH", "on") != "off",
        rate_limit_per_minute=int(env.get("CARET_RATE_LIMIT_PER_MINUTE", "120")),
    )


__all__ = [
    "Backend",
    "CHUNK_MAX_BYTES",
    "SESSION_TTL_SECONDS",
    "backend_from_env",
    "make_server",
]
