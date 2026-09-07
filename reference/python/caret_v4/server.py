"""The caret/v4 backend: `GET /health` and the three WebSocket routes.

Everything the protocol calls a policy decision is a constructor
argument, and everything it calls a fact is computed. Capabilities are
the second kind: a route is advertised when a provider for it is
configured and not otherwise, because §2 forbids advertising what the
backend cannot do.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import lanes
from .cleanup import SPEC_NAME, SpecError, load_spec
from .protocol import (
    PROTOCOL_NAME,
    ROUTE_ASK,
    ROUTE_DICTATE,
    ROUTE_IMAGINE,
    ROUTES,
)
from .session import Session
from .ws import WSError, upgrade

VERSION = "1.0.0"

DEFAULT_MAX_AUDIO_SECONDS = 600
DEFAULT_MAX_FRAME_BYTES = 524288
DEFAULT_MAX_TEXT_CHARS = 4000
DEFAULT_MAX_VOCABULARY_ENTRIES = 200
#: §8 asks for at least five minutes of successful-result caching.
DEFAULT_RESULT_CACHE_TTL_SECONDS = 600.0


class ResultCache:
    """Successful terminal results, keyed by route and client_request_id.

    Successes only: §8 is explicit that retrying a failure is the point
    of retrying, so a cached error would defeat the mechanism it is
    meant to support.
    """

    def __init__(self, ttl: float = DEFAULT_RESULT_CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._entries: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def get(self, route: str, client_request_id: str):
        with self._lock:
            entry = self._entries.get((route, client_request_id))
            if entry is None:
                return None
            if time.monotonic() - entry["at"] > self.ttl:
                del self._entries[(route, client_request_id)]
                return None
            return entry

    def put(self, route: str, client_request_id: str, request_id: str, result, audio) -> None:
        now = time.monotonic()
        with self._lock:
            for key, entry in list(self._entries.items()):
                if now - entry["at"] > self.ttl:
                    del self._entries[key]
            self._entries[(route, client_request_id)] = {
                "at": now, "request_id": request_id, "result": result, "audio": audio,
            }


class Server:
    def __init__(
        self,
        *,
        service: str = "caret-v4-reference-python",
        version: str = VERSION,
        api_keys: list[str] | None = None,
        stt: str = "loopback",
        agent: str = "",
        image: str = "",
        cleanup: str = "",
        max_audio_seconds: int = DEFAULT_MAX_AUDIO_SECONDS,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_vocabulary_entries: int = DEFAULT_MAX_VOCABULARY_ENTRIES,
        lane_timeout: float = 120.0,
        result_cache_ttl: float = DEFAULT_RESULT_CACHE_TTL_SECONDS,
        spec_dir: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.version = version
        self.max_audio_seconds = max_audio_seconds
        self.max_frame_bytes = max_frame_bytes
        self.max_text_chars = max_text_chars
        self.max_vocabulary_entries = max_vocabulary_entries
        self.lane_timeout = lane_timeout
        self.log = logger or logging.getLogger("caret_v4")
        self.cache = ResultCache(result_cache_ttl)
        self.blockers: list[dict] = []

        # A malformed lane spec is a startup failure: better a backend
        # that will not start than one that fails every dictation.
        self.stt = lanes.resolve_stt(stt, lane_timeout)
        self.agent = lanes.resolve_agent(agent, lane_timeout)
        self.image = lanes.resolve_image(image, lane_timeout)
        self.cleanup = lanes.resolve_cleanup(cleanup, lane_timeout)

        self.spec = None
        if self.cleanup is not None:
            try:
                self.spec = load_spec(spec_dir or None)
            except SpecError as exc:
                # Cleanup is best-effort, so an unverifiable spec disables
                # polish rather than the backend, and says so on /health.
                self.log.warning("cleanup spec unavailable: %s", exc)
                self.cleanup = None
                self.blockers.append({"code": "cleanup_spec_unavailable", "message": str(exc)})

        # §2: credentials are hashed at startup so the plain key is never
        # held for longer than the constructor.
        self._key_digests = [hashlib.sha256(k.encode("utf-8")).digest()
                             for k in (api_keys or []) if k]
        if self.stt is None:
            self.blockers.append({
                "code": "no_stt",
                "message": "no speech-to-text lane is configured; /dictate is unavailable",
            })
        if not self._key_digests:
            self.blockers.append({
                "code": "no_credentials",
                "message": "no API key is configured; every operation is refused",
            })

    # ------------------------------------------------------------ facts

    def route_enabled(self, route: str) -> bool:
        return {
            ROUTE_DICTATE: self.stt is not None,
            ROUTE_ASK: self.agent is not None,
            ROUTE_IMAGINE: self.image is not None,
        }.get(route, False)

    def routes(self) -> list[str]:
        served = [f"/{route}" for route in ROUTES if self.route_enabled(route)]
        return served or ["(none — /health only)"]

    def status(self) -> str:
        if self.stt is None or not self._key_digests:
            return "not_ready"
        return "degraded" if self.blockers else "ok"

    def vocabulary_capable(self) -> bool:
        """§7: advertise true only where the list actually goes
        somewhere. Computed from the providers, never configured."""
        if self.stt is None:
            return False
        return bool(getattr(self.stt, "uses_vocabulary", False)) or self.cleanup is not None

    def credential_valid(self, authorization: str) -> bool:
        """Constant-time in both content and length: the comparison runs
        against every configured key and the digests are fixed width, so
        a wrong key and a wrong-length key take the same path."""
        prefix = "bearer "
        presented = ""
        if authorization and authorization[:len(prefix)].lower() == prefix:
            presented = authorization[len(prefix):].strip()
        if not presented or not self._key_digests:
            return False
        digest = hashlib.sha256(presented.encode("utf-8")).digest()
        valid = False
        for known in self._key_digests:
            valid |= hmac.compare_digest(digest, known)
        return bool(valid)

    def cache_get(self, route: str, client_request_id: str):
        return self.cache.get(route, client_request_id)

    def cache_put(self, route: str, client_request_id: str, request_id: str, result, audio) -> None:
        self.cache.put(route, client_request_id, request_id, result, audio)

    # ----------------------------------------------------------- health

    def health_document(self, authorization: str | None) -> dict:
        presented = bool(authorization)
        document = {
            "protocol": PROTOCOL_NAME,
            "status": self.status(),
            "service": self.service,
            "version": self.version,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "auth": {
                "presented": presented,
                # null when nothing was presented: "no credential" is not
                # the same answer as "that credential is wrong", and an
                # operator debugging a 4401 needs to tell them apart.
                "valid": self.credential_valid(authorization or "") if presented else None,
            },
            "capabilities": {
                ROUTE_DICTATE: self.route_enabled(ROUTE_DICTATE),
                ROUTE_ASK: self.route_enabled(ROUTE_ASK),
                ROUTE_IMAGINE: self.route_enabled(ROUTE_IMAGINE),
                "text_input": {route: self.route_enabled(route) for route in ROUTES},
                "partials": {
                    route: self.route_enabled(route) and bool(getattr(self.stt, "streaming", False))
                    for route in ROUTES
                },
                "vocabulary": self.vocabulary_capable(),
            },
            "limits": {
                "max_audio_seconds": self.max_audio_seconds,
                "max_frame_bytes": self.max_frame_bytes,
                "max_text_chars": self.max_text_chars,
                "max_vocabulary_entries": self.max_vocabulary_entries,
            },
            "blockers": self.blockers,
        }
        if self.spec is not None:
            # §10 blesses naming the cleanup spec: the digest identifies
            # the prompt without disclosing a word of it.
            document["cleanup"] = {"spec": SPEC_NAME, "digest": self.spec.digest}
        return document


# ------------------------------------------------------------------ HTTP


def make_handler(server: Server):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"caret-v4-reference/{server.version}"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 — http.server's spelling
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                self._send_json(200, server.health_document(self.headers.get("Authorization")))
                return
            route = path.lstrip("/")
            if route in ROUTES:
                self._handle_route(route)
                return
            self._send_json(404, {"error": "not_found", "message": "no such route"})

        def _handle_route(self, route: str) -> None:
            try:
                conn = upgrade(self)
            except WSError as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
                return
            except OSError:
                return
            Session(server, conn, route, self.headers.get("Authorization") or "").run()

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, fmt: str, *args) -> None:
            # Request paths only, at debug level: §9 keeps transcripts,
            # audio, and vocabulary out of the log entirely, and a
            # WebSocket route's per-operation logging is the session's.
            server.log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def make_http_server(server: Server, host: str, port: int) -> ThreadingHTTPServer:
    http_server = ThreadingHTTPServer((host, port), make_handler(server))
    http_server.daemon_threads = True
    return http_server
