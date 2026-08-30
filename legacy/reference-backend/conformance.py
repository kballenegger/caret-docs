#!/usr/bin/env python3
"""Conformance checker for any `caret/v2` backend.

Point it at a running backend and it exercises the contract the way a
client does — including the parts that are easy to get subtly wrong: the
one input model (and the rejection of the removed V1 aliases), chunk
idempotency, checksum rejection, the async polling convention, and atomic
session consumption (one terminal result per session, no finalize
endpoint).

    python3 conformance.py --base-url http://127.0.0.1:8787 --api-key KEY

Exits 0 when every check passes, 1 otherwise. Standard library only, so it
runs anywhere Python does — including against a backend you wrote in some
other language.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 120


class Client:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key

    def request(self, method: str, path: str, *, body=None, headers=None, auth=True):
        url = self.base + path
        data = None
        head = dict(headers or {})
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            head.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
            head.setdefault("Content-Type", "application/octet-stream")
        if auth:
            head["Authorization"] = f"Bearer {self.key}"
        head.setdefault("X-Caret-Client", "caret-conformance/2.0")
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                return resp.status, _decode(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read()), dict(exc.headers)

    def poll(self, method: str, path: str, body: dict, *, budget: float = 90.0):
        """Re-POST the identical body until the result is terminal.

        This is the whole async convention: a 202 is not an error and the
        retry is not a new request — it is the same request again, and
        `request_id` must not change across polls.
        """
        deadline = time.time() + budget
        first_request_id = None
        while True:
            status, payload, headers = self.request(method, path, body=body)
            rid = (payload or {}).get("request_id")
            if first_request_id is None:
                first_request_id = rid
            elif rid != first_request_id:
                raise AssertionError(
                    f"request_id changed across polls: {first_request_id} -> {rid}"
                )
            if status != 202:
                return status, payload, headers
            if time.time() > deadline:
                raise AssertionError(f"still in_progress after {budget}s")
            time.sleep(float(headers.get("Retry-After") or payload.get("retry_after_seconds", 2)))


def _decode(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:400].decode("utf-8", "replace")}


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"  PASS  {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(f"{name}: {detail}")
        print(f"  FAIL  {name}\n        {detail}")

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  SKIP  {name} ({why})")

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(name)
        else:
            self.fail(name, detail or "condition not met")
        return condition


def _pcm(ms: int, sample_rate: int = 16000) -> bytes:
    return b"\x00\x01" * int(sample_rate * ms / 1000)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ sections


def check_health(client: Client, report: Report) -> dict:
    print("\nhealth")
    status, body, _ = client.request("GET", "/v2/health", auth=False)
    report.check("health is anonymous", status == 200, f"got {status}")
    report.check(
        "contract is caret/v2", body.get("contract") == "caret/v2", repr(body.get("contract"))
    )
    report.check("time is present", bool(body.get("time")))
    caps = body.get("capabilities") or {}
    report.check("capabilities present", bool(caps), "no capabilities object")
    modes = caps.get("input_modes") or {}
    report.check(
        "input_modes covers every operation",
        all(isinstance(modes.get(op), list) for op in ("dictate", "ask", "imagine")),
        json.dumps(modes)[:200],
    )
    # Capability honesty: an operation reported off must advertise no
    # input modes, and one reported on must accept at least text or session.
    honest = True
    for op in ("dictate", "ask", "imagine"):
        declared = bool(caps.get(op))
        advertised = [m for m in (modes.get(op) or []) if m in ("text", "session")]
        if declared != bool(advertised):
            honest = False
    report.check(
        "capabilities and input_modes agree",
        honest,
        json.dumps(caps)[:300],
    )

    status, authed, _ = client.request("GET", "/v2/health")
    report.check(
        "health reflects a valid key",
        (authed.get("auth") or {}).get("valid") is True,
        f"auth={authed.get('auth')}",
    )
    return caps


def check_auth(client: Client, report: Report) -> None:
    print("\nauth")
    status, body, _ = client.request("POST", "/v2/ask", body={}, auth=False)
    report.check("unauthenticated ask is 401", status == 401, f"got {status}")
    report.check(
        "401 uses the error envelope",
        (body.get("error") or {}).get("code") == "unauthorized",
        json.dumps(body)[:200],
    )
    report.check("401 carries a request_id", bool(body.get("request_id")))

    bad = Client(client.base, client.key + "-wrong")
    status, _, _ = bad.request("POST", "/v2/ask", body={})
    report.check("a wrong key is 401", status == 401, f"got {status}")


def check_text_ask(client: Client, report: Report) -> None:
    print("\nask — typed input")
    crid = f"conf-text-{int(time.time() * 1000)}"
    body = {
        "client_request_id": crid,
        "input": {"type": "text", "text": "say hello to the team"},
        "app_hint": "com.example.messages",
    }
    status, payload, _ = client.poll("POST", "/v2/ask", body)
    if not report.check("typed ask succeeds", status == 200, f"{status} {json.dumps(payload)[:200]}"):
        return
    report.check("ask returns text", isinstance(payload.get("text"), str) and payload["text"])
    report.check("ask returns a request_id", bool(payload.get("request_id")))
    report.check(
        "input_type is text", payload.get("input_type") == "text", repr(payload.get("input_type"))
    )

    status, replay, _ = client.request("POST", "/v2/ask", body=body)
    report.check(
        "same client_request_id replays the same text",
        status == 200 and replay.get("text") == payload.get("text"),
        "idempotent replay returned different text",
    )

    status, payload, _ = client.request(
        "POST",
        "/v2/ask",
        body={"client_request_id": crid + "-alias", "instruction": "say hello"},
    )
    report.check(
        "the removed V1 alias is rejected with input_invalid",
        status == 422 and (payload.get("error") or {}).get("code") == "input_invalid",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST", "/v2/ask", body={"client_request_id": crid + "-none"}
    )
    report.check(
        "missing input is rejected with input_invalid",
        status == 422 and (payload.get("error") or {}).get("code") == "input_invalid",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST",
        "/v2/ask",
        body={
            "client_request_id": crid + "-audio",
            "input": {
                "type": "audio",
                "session_id": "sess_x",
                "client_chunk_count": 1,
                "client_total_duration_ms": 1000,
            },
        },
    )
    report.check(
        'the removed input.type "audio" is rejected with input_invalid',
        status == 422 and (payload.get("error") or {}).get("code") == "input_invalid",
        f"{status} {json.dumps(payload)[:200]}",
    )


def open_session(client: Client, report: Report, intent: str, tag: str) -> str | None:
    status, payload, _ = client.request(
        "POST",
        "/v2/sessions",
        body={
            "client_request_id": f"conf-{tag}-{int(time.time() * 1000)}",
            "codec": "pcm16",
            "sample_rate_hz": 16000,
            "channels": 1,
            "intent": intent,
        },
    )
    if status != 200 or not payload.get("session_id"):
        report.fail(f"open {tag} session", f"{status} {json.dumps(payload)[:200]}")
        return None
    report.ok(f"open {tag} session")
    return payload["session_id"]


def upload(client: Client, session_id: str, seq: int, pcm: bytes, *, sha: str | None = None):
    ms = int(len(pcm) / 2 / 16000 * 1000)
    return client.request(
        "PUT",
        f"/v2/sessions/{session_id}/chunks/{seq}",
        body=pcm,
        headers={
            "X-Caret-Chunk-SHA256": sha or _sha(pcm),
            "X-Caret-Chunk-Duration-Ms": str(ms),
        },
    )


def session_input(session_id: str, *, count: int, duration_ms: int, polish: bool = False) -> dict:
    return {
        "type": "session",
        "session_id": session_id,
        "client_chunk_count": count,
        "client_total_duration_ms": duration_ms,
        "polish": polish,
    }


def check_dictate(client: Client, report: Report) -> None:
    print("\ndictate — chunked audio, atomic consumption")
    session_id = open_session(client, report, "dictate", "dict")
    if not session_id:
        return

    chunk = _pcm(3000)
    status, payload, _ = upload(client, session_id, 0, chunk)
    report.check("chunk 0 accepted", status == 200, f"{status} {json.dumps(payload)[:200]}")

    status, replay, _ = upload(client, session_id, 0, chunk)
    report.check(
        "re-uploading an identical chunk is idempotent",
        status == 200 and replay.get("duplicate") is True,
        f"{status} {json.dumps(replay)[:200]}",
    )

    status, payload, _ = upload(client, session_id, 1, chunk, sha=_sha(b"different"))
    report.check(
        "a wrong checksum is rejected",
        status == 409 and (payload.get("error") or {}).get("code") == "chunk_checksum_mismatch",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST",
        "/v2/dictate",
        body={
            "client_request_id": "conf-dict-gap",
            "input": session_input(session_id, count=2, duration_ms=6000),
        },
    )
    report.check(
        "an incomplete upload reports missing chunks, not an error",
        status == 200 and payload.get("status") == "missing_chunks" and payload.get("missing_chunks") == [1],
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, _, _ = upload(client, session_id, 1, _pcm(1500))
    report.check("the missing chunk uploads", status == 200)

    body = {
        "client_request_id": "conf-dict-final",
        "input": session_input(session_id, count=2, duration_ms=4500),
    }
    status, payload, _ = client.poll("POST", "/v2/dictate", body)
    report.check(
        "dictate completes the session",
        status == 200 and payload.get("status") == "complete" and isinstance(payload.get("text"), str),
        f"{status} {json.dumps(payload)[:200]}",
    )
    report.check(
        "dictate echoes the session id",
        payload.get("session_id") == session_id and payload.get("input_type") == "session",
        json.dumps(payload)[:200],
    )

    status, payload, _ = client.request(
        "POST",
        "/v2/ask",
        body={
            "client_request_id": "conf-conflict",
            "input": session_input(session_id, count=2, duration_ms=4500),
        },
    )
    report.check(
        "a consumed session cannot be reused by another operation",
        status == 409 and (payload.get("error") or {}).get("code") == "session_conflict",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = upload(client, session_id, 2, _pcm(500))
    report.check(
        "a chunk upload after consumption is a session_conflict",
        status == 409 and (payload.get("error") or {}).get("code") == "session_conflict",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST",
        "/v2/dictate",
        body={
            "client_request_id": "conf-unknown",
            "input": session_input("does-not-exist", count=1, duration_ms=1000),
        },
    )
    report.check(
        "an unknown session is 404 unknown_session",
        status == 404 and (payload.get("error") or {}).get("code") == "unknown_session",
        f"{status} {json.dumps(payload)[:200]}",
    )


def check_spoken(client: Client, report: Report, operation: str) -> None:
    print(f"\n{operation} — spoken input")
    session_id = open_session(client, report, operation, operation)
    if not session_id:
        return
    chunk = _pcm(2000)
    status, _, _ = upload(client, session_id, 0, chunk)
    if not report.check("chunk uploaded", status == 200):
        return

    body = {
        "client_request_id": f"conf-{operation}-{int(time.time() * 1000)}",
        "input": session_input(session_id, count=1, duration_ms=2000, polish=True),
    }
    status, payload, _ = client.poll("POST", f"/v2/{operation}", body)
    if not report.check(
        f"spoken {operation} completes", status == 200, f"{status} {json.dumps(payload)[:200]}"
    ):
        return
    report.check(
        "input_type is session",
        payload.get("input_type") == "session",
        repr(payload.get("input_type")),
    )
    report.check(
        "the transcript is echoed back",
        isinstance(payload.get("transcript"), str) and payload["transcript"],
        "no transcript in the response",
    )
    report.check(
        "the session id is echoed back", payload.get("session_id") == session_id
    )
    if operation == "ask":
        report.check("spoken ask returns text", isinstance(payload.get("text"), str))
    else:
        media = payload.get("media") or {}
        report.check(
            "spoken imagine returns media",
            media.get("mime_type") == "image/png" and media.get("byte_length", 0) > 0,
            json.dumps(media)[:200],
        )


def check_imagine_text(client: Client, report: Report) -> None:
    print("\nimagine — typed input")
    body = {
        "client_request_id": f"conf-img-{int(time.time() * 1000)}",
        "input": {"type": "text", "text": "a lighthouse at dusk"},
        "aspect_ratio": "1:1",
        "quality": "low",
    }
    status, payload, _ = client.poll("POST", "/v2/imagine", body)
    if not report.check("typed imagine completes", status == 200, f"{status} {json.dumps(payload)[:200]}"):
        return
    media = payload.get("media") or {}
    report.check("media is a PNG", media.get("mime_type") == "image/png", json.dumps(media)[:200])
    report.check("media declares byte_length", isinstance(media.get("byte_length"), int))
    report.check("media declares sha256", isinstance(media.get("sha256"), str))
    report.check(
        "media carries inline_base64 (the only payload transport)",
        isinstance(media.get("inline_base64"), str) and media["inline_base64"],
        json.dumps(media)[:200],
    )
    if isinstance(media.get("inline_base64"), str):
        import base64

        raw = base64.b64decode(media["inline_base64"])
        report.check(
            "inline_base64 matches sha256 and byte_length",
            _sha(raw) == media.get("sha256") and len(raw) == media.get("byte_length"),
            "declared digest or length does not match the bytes",
        )

    status, replay, _ = client.poll("POST", "/v2/imagine", body)
    report.check(
        "re-posting the identical body replays the cached result",
        status == 200 and (replay.get("media") or {}).get("sha256") == media.get("sha256"),
        f"{status} {json.dumps(replay)[:200]}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args(argv)

    client = Client(args.base_url, args.api_key)
    report = Report()

    print(f"caret/v2 conformance against {client.base}")
    caps = check_health(client, report)
    check_auth(client, report)

    modes = caps.get("input_modes") or {}
    if caps.get("ask"):
        check_text_ask(client, report)
    else:
        report.skip("typed ask", "capabilities.ask is false")

    if caps.get("dictate"):
        check_dictate(client, report)
        if caps.get("ask") and "session" in (modes.get("ask") or []):
            check_spoken(client, report, "ask")
        else:
            report.skip("spoken ask", "capabilities.input_modes.ask has no session")
    else:
        report.skip("dictate", "capabilities.dictate is false (this backend is not ready)")
        report.skip("spoken ask", "capabilities.dictate is false")

    if caps.get("imagine"):
        check_imagine_text(client, report)
        if caps.get("dictate") and "session" in (modes.get("imagine") or []):
            check_spoken(client, report, "imagine")
        else:
            report.skip("spoken imagine", "not advertised in capabilities")
    else:
        report.skip("imagine", "capabilities.imagine is false")

    print(
        f"\n{report.passed} passed, {len(report.failed)} failed, {len(report.skipped)} skipped"
    )
    for failure in report.failed:
        print(f"  - {failure}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
