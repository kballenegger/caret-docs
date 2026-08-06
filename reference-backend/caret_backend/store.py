"""Filesystem state: audio sessions, chunks, jobs, idempotency.

Deliberately boring — a directory per session, one JSON file per record,
atomic replace on every write. There is no database and no cache server to
run, which is the point: you can read the whole state of the backend with
`find` and delete it with `rm -rf`.

Concurrency model: one process, many threads (see `server.py`). A single
re-entrant lock guards every read-modify-write, so concurrent polls of the
same job cannot both start work. If you run more than one process against
the same data directory you must replace `_LOCK` with a real file lock —
that is called out in the reference-backend README rather than pretended
away here.

Retention: audio is the most sensitive thing this backend touches, so
chunks are deleted as soon as a session reaches a terminal result, and a
janitor thread removes whole sessions one TTL past expiry.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()

# The contract's session lifetime. A client that has not finished uploading
# within this window must open a new session.
SESSION_TTL_SECONDS = 3600
# How long a terminal job result stays replayable after its session expires.
JANITOR_GRACE_SECONDS = 3600


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.sessions = self.root / "sessions"
        self.jobs = self.root / "jobs"
        self.idem = self.root / "idempotency"
        for directory in (self.sessions, self.jobs, self.idem):
            directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- sessions

    def create_session(self, *, client_request_id: str, meta: dict) -> dict:
        """Open a session, or replay the one this client_request_id opened.

        Idempotency here is what makes a retried "open" safe on a flaky
        connection: the client cannot tell whether its first request landed,
        so replaying the same session id is the only answer that does not
        strand audio in an orphaned session.
        """
        with _LOCK:
            replay_path = self.idem / f"session-{_digest(client_request_id)}.json"
            replayed = _read_json(replay_path)
            if replayed and self.read_session(replayed["session_id"]) is not None:
                return self.read_session(replayed["session_id"])

            session_id = "sess_" + secrets.token_hex(8)
            now = time.time()
            record = dict(meta)
            record.update(
                {
                    "session_id": session_id,
                    "client_request_id": client_request_id,
                    "created_at": now,
                    "expires_at": now + SESSION_TTL_SECONDS,
                    "chunks": {},
                }
            )
            (self.sessions / session_id / "chunks").mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._meta_path(session_id), record)
            _atomic_write_json(replay_path, {"session_id": session_id})
            return record

    def _meta_path(self, session_id: str) -> Path:
        return self.sessions / session_id / "meta.json"

    def read_session(self, session_id: str) -> dict | None:
        # Session ids are server-generated, but they arrive from the network:
        # refuse anything that could escape the sessions directory.
        if not session_id or "/" in session_id or ".." in session_id:
            return None
        return _read_json(self._meta_path(session_id))

    def update_session(self, session_id: str, **fields) -> dict | None:
        with _LOCK:
            meta = self.read_session(session_id)
            if meta is None:
                return None
            meta.update(fields)
            _atomic_write_json(self._meta_path(session_id), meta)
            return meta

    def delete_session(self, session_id: str) -> None:
        shutil.rmtree(self.sessions / session_id, ignore_errors=True)

    def delete_session_audio(self, session_id: str) -> None:
        """Drop the raw audio but keep the session's terminal result."""
        shutil.rmtree(self.sessions / session_id / "chunks", ignore_errors=True)

    # ------------------------------------------------------------------ chunks

    def chunk_path(self, session_id: str, seq: int) -> Path:
        return self.sessions / session_id / "chunks" / f"{seq:06d}.pcm"

    def put_chunk(
        self, session_id: str, seq: int, data: bytes, *, sha256: str, duration_ms: int
    ) -> bool:
        """Store one chunk. Returns True when this was a duplicate replay.

        Idempotent per `(session_id, seq)`: identical bytes are a no-op. The
        caller has already verified `sha256` against `data`, so a stored
        digest that differs means the client sent two *different* chunks for
        one sequence number — a client bug, not a transport problem.
        """
        with _LOCK:
            meta = self.read_session(session_id)
            if meta is None:
                return False
            existing = meta["chunks"].get(str(seq))
            if existing is not None:
                return existing["sha256"] == sha256
            path = self.chunk_path(session_id, seq)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            meta["chunks"][str(seq)] = {
                "sha256": sha256,
                "bytes": len(data),
                "duration_ms": duration_ms,
            }
            _atomic_write_json(self._meta_path(session_id), meta)
            return False

    def chunk_sha256(self, session_id: str, seq: int) -> str | None:
        meta = self.read_session(session_id)
        if meta is None:
            return None
        stored = meta["chunks"].get(str(seq))
        return stored["sha256"] if stored else None

    def missing_chunks(self, session_id: str, expected_count: int) -> list[int]:
        meta = self.read_session(session_id) or {"chunks": {}}
        have = {int(seq) for seq in meta["chunks"]}
        return [seq for seq in range(expected_count) if seq not in have]

    def read_audio(self, session_id: str, expected_count: int) -> bytes:
        parts = []
        for seq in range(expected_count):
            path = self.chunk_path(session_id, seq)
            if path.exists():
                parts.append(path.read_bytes())
        return b"".join(parts)

    # -------------------------------------------------------------------- jobs

    def read_job(self, key: str) -> dict | None:
        return _read_json(self.jobs / f"{_digest(key)}.json")

    def write_job(self, key: str, record: dict) -> None:
        with _LOCK:
            _atomic_write_json(self.jobs / f"{_digest(key)}.json", record)

    def claim_job(self, key: str, *, request_id: str, surface: str) -> dict:
        """Return the existing job for `key`, or register a new running one.

        This is the whole of the async contract's correctness: the first
        POST creates the job, every re-post finds it, and `request_id`
        therefore stays stable from the first 202 through the terminal
        response. Claiming and checking must happen under one lock or two
        concurrent polls would start two jobs and bill the work twice.
        """
        with _LOCK:
            existing = self.read_job(key)
            if existing is not None:
                return existing
            record = {
                "key": key,
                "surface": surface,
                "request_id": request_id,
                "state": "running",
                "started_at": time.time(),
            }
            self.write_job(key, record)
            return record

    def finish_job(self, key: str, *, result: dict | None, error: dict | None) -> None:
        with _LOCK:
            record = self.read_job(key) or {"key": key}
            record["state"] = "failed" if error else "done"
            record["finished_at"] = time.time()
            record["result"] = result
            record["error"] = error
            self.write_job(key, record)

    # ------------------------------------------------------------ idempotency

    def read_idempotent(self, key: str) -> dict | None:
        return _read_json(self.idem / f"{_digest(key)}.json")

    def write_idempotent(self, key: str, payload: dict) -> None:
        with _LOCK:
            _atomic_write_json(self.idem / f"{_digest(key)}.json", payload)

    # ---------------------------------------------------------------- janitor

    def sweep(self, *, now: float | None = None) -> int:
        """Delete sessions one TTL past expiry. Returns how many went."""
        now = time.time() if now is None else now
        removed = 0
        if not self.sessions.exists():
            return 0
        for directory in list(self.sessions.iterdir()):
            meta = _read_json(directory / "meta.json")
            expires_at = (meta or {}).get("expires_at")
            if expires_at is None:
                # Unreadable or half-written: fall back to mtime so a
                # corrupt directory cannot pin storage forever.
                expires_at = directory.stat().st_mtime + SESSION_TTL_SECONDS
            if now > expires_at + JANITOR_GRACE_SECONDS:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed


def start_janitor(store: Store, *, interval_seconds: int = 300) -> threading.Thread:
    def loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                store.sweep()
            except Exception:  # noqa: BLE001 - a janitor must never die
                pass

    thread = threading.Thread(target=loop, name="caret-janitor", daemon=True)
    thread.start()
    return thread
