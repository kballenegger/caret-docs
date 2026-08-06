"""Shared test scaffolding: a real server on a real socket, no network.

The suite talks HTTP to `127.0.0.1` on an ephemeral port with the echo
adapters wired in, so every test exercises the same routing, parsing and
serialisation a phone would — but nothing leaves the machine, nothing costs
a token, and the results are deterministic.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import adapters  # noqa: E402
from caret_backend.server import Backend, make_server  # noqa: E402
from caret_backend.store import Store  # noqa: E402

API_KEY = "test-key"


class BlockingAgent(adapters.EchoAgent):
    """An agent that waits on an event — used to observe a real 202."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def draft(self, *, instruction: str, visible_text=None, app_hint=None) -> str:
        self.released.wait(timeout=10)
        return f"[slow] {instruction}"


class TestServer:
    """Start a backend on an ephemeral port; tear the whole thing down after."""

    def __init__(self, *, inline_jobs: bool = True, **overrides) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        options = {
            "store": Store(Path(self._tmp.name)),
            "agent": adapters.EchoAgent(),
            "transcriber": adapters.EchoTranscriber(),
            "image_generator": adapters.FakeImageGenerator(),
            "api_keys": (API_KEY,),
            "run_jobs_inline": inline_jobs,
        }
        options.update(overrides)
        self.backend = Backend(**options)
        self.httpd = make_server(self.backend, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    # ------------------------------------------------------------ requests

    def request(self, method: str, path: str, *, body=None, headers=None, key=API_KEY):
        head = dict(headers or {})
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            head.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
            head.setdefault("Content-Type", "application/octet-stream")
        if key is not None:
            head["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return resp.status, _json(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read()), dict(exc.headers)

    def open_session(self, *, intent: str = "dictate", client_request_id: str = "crid-1"):
        status, body, _ = self.request(
            "POST",
            "/v1/dictation/sessions",
            body={
                "client_request_id": client_request_id,
                "codec": "pcm16",
                "sample_rate_hz": 16000,
                "channels": 1,
                "intent": intent,
            },
        )
        assert status == 200, body
        return body["session_id"]

    def upload(self, session_id: str, seq: int, pcm: bytes, *, sha: str | None = None):
        return self.request(
            "PUT",
            f"/v1/dictation/sessions/{session_id}/chunks/{seq}",
            body=pcm,
            headers={
                "X-Caret-Chunk-SHA256": sha or sha256_hex(pcm),
                "X-Caret-Chunk-Duration-Ms": str(duration_ms(pcm)),
            },
        )


def _json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw.decode("utf-8", "replace")}


def pcm(ms: int, sample_rate: int = 16000) -> bytes:
    return b"\x01\x02" * int(sample_rate * ms / 1000)


def duration_ms(data: bytes, sample_rate: int = 16000) -> int:
    return int(len(data) / 2 / sample_rate * 1000)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
