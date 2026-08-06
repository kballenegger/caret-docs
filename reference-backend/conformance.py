#!/usr/bin/env python3
"""Conformance checker for any `caret/v1` backend.

Point it at a running backend and it exercises the contract the way a
client does — including the parts that are easy to get subtly wrong: the
one input model, chunk idempotency, checksum rejection, the async polling
convention, and one terminal result per session.

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
        head.setdefault("X-Caret-Client", "caret-conformance/1.1")
        req = urllib.request.Request(url, data=data, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                return resp.status, _decode(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read()), dict(exc.headers)

    def poll(self, method: str, path: str, body: dict, *, budget: float = 90.0):
        """Re-POST the identical body until the result is terminal.

        This is the whole async convention: a 202 is not an error and the
        retry is not a new request — it is the same request again.
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
    status, body, _ = client.request("GET", "/v1/health", auth=False)
    report.check("health is anonymous", status == 200, f"got {status}")
    report.check(
        "contract is caret/v1", body.get("contract") == "caret/v1", repr(body.get("contract"))
    )
    report.check("time is present", bool(body.get("time")))
    caps = body.get("capabilities") or {}
    report.check("capabilities present", bool(caps), "no capabilities object")

    status, authed, _ = client.request("GET", "/v1/health")
    report.check(
        "health reflects a valid key",
        (authed.get("auth") or {}).get("valid") is True,
        f"auth={authed.get('auth')}",
    )
    return caps


def check_auth(client: Client, report: Report) -> None:
    print("\nauth")
    status, body, _ = client.request("POST", "/v1/draft", body={}, auth=False)
    report.check("unauthenticated draft is 401", status == 401, f"got {status}")
    report.check(
        "401 uses the error envelope",
        (body.get("error") or {}).get("code") == "unauthorized",
        json.dumps(body)[:200],
    )
    report.check("401 carries a request_id", bool(body.get("request_id")))

    bad = Client(client.base, client.key + "-wrong")
    status, _, _ = bad.request("POST", "/v1/draft", body={})
    report.check("a wrong key is 401", status == 401, f"got {status}")


def check_text_draft(client: Client, report: Report) -> None:
    print("\ndraft — typed input")
    crid = f"conf-text-{int(time.time() * 1000)}"
    body = {
        "client_request_id": crid,
        "input": {"type": "text", "text": "say hello to the team"},
        "app_hint": "com.example.messages",
    }
    status, payload, _ = client.poll("POST", "/v1/draft", body)
    if not report.check("typed draft succeeds", status == 200, f"{status} {json.dumps(payload)[:200]}"):
        return
    report.check("draft returns text", isinstance(payload.get("text"), str) and payload["text"])
    report.check("draft returns a request_id", bool(payload.get("request_id")))
    report.check(
        "input_type is text", payload.get("input_type") in (None, "text"), repr(payload.get("input_type"))
    )

    status, replay, _ = client.request("POST", "/v1/draft", body=body)
    report.check(
        "same client_request_id replays the same text",
        status == 200 and replay.get("text") == payload.get("text"),
        "idempotent replay returned different text",
    )

    status, payload, _ = client.request(
        "POST",
        "/v1/draft",
        body={"client_request_id": crid + "-both", "input": {"type": "text", "text": "hi"}, "instruction": "hi"},
    )
    report.check(
        "input plus the legacy alias is rejected",
        status == 422 and (payload.get("error") or {}).get("code") == "input_invalid",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST", "/v1/draft", body={"client_request_id": crid + "-none"}
    )
    report.check(
        "neither input nor alias is rejected",
        status == 422,
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.poll(
        "POST",
        "/v1/draft",
        {"client_request_id": crid + "-legacy", "instruction": "say hello"},
    )
    report.check(
        "the 1.0 alias still works",
        status == 200 and isinstance(payload.get("text"), str),
        f"{status} {json.dumps(payload)[:200]}",
    )


def open_session(client: Client, report: Report, intent: str, tag: str) -> str | None:
    status, payload, _ = client.request(
        "POST",
        "/v1/dictation/sessions",
        body={
            "client_request_id": f"conf-{tag}-{int(time.time() * 1000)}",
            "codec": "pcm16",
            "sample_rate_hz": 16000,
            "channels": 1,
            "intent": intent,
        },
    )
    if status != 200 or not payload.get("session_id"):
        report.fail(f"open {intent} session", f"{status} {json.dumps(payload)[:200]}")
        return None
    report.ok(f"open {intent} session")
    return payload["session_id"]


def upload(client: Client, session_id: str, seq: int, pcm: bytes, *, sha: str | None = None):
    ms = int(len(pcm) / 2 / 16000 * 1000)
    return client.request(
        "PUT",
        f"/v1/dictation/sessions/{session_id}/chunks/{seq}",
        body=pcm,
        headers={
            "X-Caret-Chunk-SHA256": sha or _sha(pcm),
            "X-Caret-Chunk-Duration-Ms": str(ms),
        },
    )


def check_dictation(client: Client, report: Report) -> None:
    print("\ndictation — chunked audio")
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
        f"/v1/dictation/sessions/{session_id}/transcript",
        body={"client_chunk_count": 2, "client_total_duration_ms": 6000, "polish": False},
    )
    report.check(
        "an incomplete upload reports missing chunks, not an error",
        status == 200 and payload.get("status") == "missing_chunks" and payload.get("missing_chunks") == [1],
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, _, _ = upload(client, session_id, 1, _pcm(1500))
    report.check("the missing chunk uploads", status == 200)

    status, payload, _ = client.poll(
        "POST",
        f"/v1/dictation/sessions/{session_id}/transcript",
        {"client_chunk_count": 2, "client_total_duration_ms": 4500, "polish": False},
    )
    report.check(
        "transcript completes",
        status == 200 and payload.get("status") == "complete" and isinstance(payload.get("text"), str),
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST",
        "/v1/draft",
        body={
            "client_request_id": "conf-conflict",
            "input": {
                "type": "audio",
                "session_id": session_id,
                "client_chunk_count": 2,
                "client_total_duration_ms": 4500,
            },
        },
    )
    report.check(
        "a consumed session cannot be reused by another surface",
        status == 409 and (payload.get("error") or {}).get("code") == "session_conflict",
        f"{status} {json.dumps(payload)[:200]}",
    )

    status, payload, _ = client.request(
        "POST",
        "/v1/dictation/sessions/does-not-exist/transcript",
        body={"client_chunk_count": 1, "client_total_duration_ms": 1000},
    )
    report.check(
        "an unknown session is 404 unknown_session",
        status == 404 and (payload.get("error") or {}).get("code") == "unknown_session",
        f"{status} {json.dumps(payload)[:200]}",
    )


def check_spoken(client: Client, report: Report, surface: str, path: str) -> None:
    print(f"\n{surface} — spoken input")
    # `intent` is the client's advisory hint about which surface will consume
    # the session; the draft surface is Ask.
    intent = "ask" if surface == "draft" else surface
    session_id = open_session(client, report, intent, surface)
    if not session_id:
        return
    chunk = _pcm(2000)
    status, _, _ = upload(client, session_id, 0, chunk)
    if not report.check("chunk uploaded", status == 200):
        return

    body = {
        "client_request_id": f"conf-{surface}-{int(time.time() * 1000)}",
        "input": {
            "type": "audio",
            "session_id": session_id,
            "client_chunk_count": 1,
            "client_total_duration_ms": 2000,
            "polish": True,
        },
    }
    status, payload, _ = client.poll("POST", path, body)
    if not report.check(
        f"spoken {surface} completes", status == 200, f"{status} {json.dumps(payload)[:200]}"
    ):
        return
    report.check(
        "input_type is audio", payload.get("input_type") == "audio", repr(payload.get("input_type"))
    )
    report.check(
        "the transcript is echoed back",
        isinstance(payload.get("transcript"), str) and payload["transcript"],
        "no transcript in the response",
    )
    report.check(
        "the session id is echoed back", payload.get("session_id") == session_id
    )
    if surface == "draft":
        report.check("spoken draft returns text", isinstance(payload.get("text"), str))
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
        "aspect_ratio": "square",
        "quality": "fast",
    }
    status, payload, _ = client.poll("POST", "/v1/imagine", body)
    if not report.check("typed imagine completes", status == 200, f"{status} {json.dumps(payload)[:200]}"):
        return
    media = payload.get("media") or {}
    report.check("media is a PNG", media.get("mime_type") == "image/png", json.dumps(media)[:200])
    report.check("media declares byte_length", isinstance(media.get("byte_length"), int))
    report.check("media declares sha256", isinstance(media.get("sha256"), str))
    if isinstance(media.get("inline_base64"), str):
        import base64

        raw = base64.b64decode(media["inline_base64"])
        report.check(
            "inline_base64 matches sha256 and byte_length",
            _sha(raw) == media.get("sha256") and len(raw) == media.get("byte_length"),
            "declared digest or length does not match the bytes",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args(argv)

    client = Client(args.base_url, args.api_key)
    report = Report()

    print(f"caret/v1 conformance against {client.base}")
    caps = check_health(client, report)
    check_auth(client, report)
    check_text_draft(client, report)

    modes = caps.get("input_modes") or {}
    if caps.get("dictation"):
        check_dictation(client, report)
        if "audio" in modes.get("draft", []):
            check_spoken(client, report, "draft", "/v1/draft")
        else:
            report.skip("spoken draft", "capabilities.input_modes.draft has no audio")
    else:
        report.skip("dictation", "capabilities.dictation is false")
        report.skip("spoken draft", "capabilities.dictation is false")

    if caps.get("imagine"):
        check_imagine_text(client, report)
        if caps.get("dictation") and "audio" in modes.get("imagine", []):
            check_spoken(client, report, "imagine", "/v1/imagine")
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
