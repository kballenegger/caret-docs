"""The `caret-connect:v1:` payload — a whole configuration in one scan.

Getting Caret talking to a backend means moving two strings from a
terminal to a phone: a base URL and an API key. Typing a 43-character key
on a phone keyboard is the worst part of the entire setup, and this module
removes it. After a backend has been configured *and validated*, the setup
flow offers a QR that carries the connection outright, and the phone is
configured by pointing a camera at it.

Wire format — fixed by the Caret client, which pins it byte for byte:

    {"k":"<api key>","t":"agent","u":"https://agent.example.com"}
        minified UTF-8 JSON, keys sorted, no spaces
    → base64url (RFC 4648 §5), padding stripped
    → prefixed with "caret-connect:v1:"
    → encoded as a QR

Field by field:

  `t`   payload type, case-sensitive: `agent` for a backend like this one,
        `hosted` for a Caret-operated backend.
  `u`   base URL. **Required for `agent`.** Omitted entirely for `hosted`,
        where the client pins its own canonical URL and rejects a payload
        that tries to point it somewhere else — an anti-redirect rule, so
        a `hosted` code can never be rewritten into a credential handover
        to an attacker's server. This module only ever emits `agent`.
  `k`   the API key, 10–300 characters.

Unknown fields are tolerated by clients, so the format can gain some
later without a v2.

Worked example, byte-pinned by the client's test suite (the key is fake
and exists only as a test vector — never put a real key in a doc):

    {"k":"caret_demo_key_1234567890","t":"agent","u":"https://agent.example.com"}
    → caret-connect:v1:eyJrIjoiY2FyZXRfZGVtb19rZXlfMTIzNDU2Nzg5MCIsInQiOiJhZ2VudCIsInUiOiJodHRwczovL2FnZW50LmV4YW1wbGUuY29tIn0

## This is a credential

The QR is not a pointer to a credential. It *is* one. Anyone who scans it
— or photographs the screen, or finds the screenshot, or scrolls back
through the terminal, or is watching the screen share — can connect as
you, and keeps that access until the API key is rotated. There is no
expiry and no revocation short of rotation.

So the code here is deliberately awkward in three specific ways:

  * generating a QR is never automatic. It happens because someone asked
    for it, explicitly, in that moment.
  * the payload string is never logged and never printed in full. Ordinary
    output shows `mask()`, which is enough to tell two payloads apart and
    useless to anyone reading over your shoulder.
  * a saved PNG is written `0600`, to a path the caller named, and the
    caller is told to delete it once the phone has scanned it.

`caret_backend.pairing` is a server-side alternative that hands over a
one-time token instead of the key. It is an extension, *not* part of the
client contract — the Caret app connects by scanning a
`caret-connect:v1:` code, so this module is what the setup flow offers.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
from urllib.parse import urlparse

from .errors import CaretError

CONNECT_PREFIX = "caret-connect:v1:"
CONNECT_TYPE = "agent"
HOSTED_TYPE = "hosted"

KEY_MIN_CHARS = 10
KEY_MAX_CHARS = 300

# Beyond this the QR needs a symbol too dense to scan comfortably off a
# laptop screen, and something is wrong with the inputs anyway.
MAX_PAYLOAD_CHARS = 362


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_pad(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _plaintext_is_allowed(host: str) -> bool:
    """Where the Caret client tolerates `http://`.

    Matches the client's own rule, which is the only one that matters: a
    URL this backend accepts but the app refuses produces a QR that scans
    and then fails, which is worse than refusing here. Loopback, private
    networks, Tailscale names and Tailscale's CGNAT range — all reachable
    only from inside a network the user controls."""
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        return True
    if host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def normalise_base_url(url: str) -> str:
    """Validate and canonicalise the base URL the phone will use.

    `https://` is required for anything reachable from outside the user's
    own network: the payload carries the API key, and a phone told to talk
    to a plaintext endpoint would publish that key on every request from
    then on."""
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise CaretError(
            400, "bad_request", f"base URL must be http:// or https://, got {url!r}"
        )
    if not parsed.hostname:
        raise CaretError(400, "bad_request", f"base URL has no host: {url!r}")
    host = parsed.hostname.lower()
    if scheme == "http" and not _plaintext_is_allowed(host):
        raise CaretError(
            400,
            "bad_request",
            "base URL must be https:// outside your own network: this payload "
            "carries the API key, so a phone pointed at a plaintext endpoint "
            "would publish it on every request. http:// is accepted for "
            "localhost, private ranges, and *.ts.net.",
        )
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return f"{scheme}://{netloc}{parsed.path.rstrip('/')}"


def encode_payload(base_url: str, api_key: str) -> str:
    """Build the `caret-connect:v1:` string. Treat the result as a secret."""
    key = (api_key or "").strip()
    if not key:
        raise CaretError(
            400, "bad_request", "cannot build a connect payload without an API key"
        )
    if not KEY_MIN_CHARS <= len(key) <= KEY_MAX_CHARS:
        # The client enforces this range and rejects the scan otherwise, so
        # catching it here turns a mystifying failure on the phone into a
        # sentence in the terminal.
        raise CaretError(
            400,
            "bad_request",
            f"API key must be {KEY_MIN_CHARS}-{KEY_MAX_CHARS} characters for a "
            f"connect payload; this one is {len(key)}",
        )
    document = {"k": key, "t": CONNECT_TYPE, "u": normalise_base_url(base_url)}
    # Minified and key-sorted: the client's test suite pins this encoding
    # byte for byte, so neither the separators nor the ordering is a
    # stylistic choice.
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload = CONNECT_PREFIX + _b64url_nopad(raw)
    if len(payload) > MAX_PAYLOAD_CHARS:
        raise CaretError(
            400,
            "bad_request",
            f"connect payload is {len(payload)} characters; anything over "
            f"{MAX_PAYLOAD_CHARS} needs a QR too dense to scan reliably. Check "
            "the base URL and key lengths.",
        )
    return payload


def decode_payload(payload: str) -> dict:
    """Inverse of `encode_payload`, for tests and for clients in any
    language that want a reference implementation to check against."""
    text = (payload or "").strip()
    if not text.startswith(CONNECT_PREFIX):
        raise CaretError(
            400,
            "bad_request",
            f"not a Caret connect payload: expected the {CONNECT_PREFIX!r} prefix",
        )
    try:
        document = json.loads(_b64url_pad(text[len(CONNECT_PREFIX) :]))
    except (ValueError, TypeError) as exc:
        raise CaretError(
            400, "bad_request", f"connect payload is not valid base64url JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise CaretError(400, "bad_request", "connect payload is not a JSON object")
    kind = document.get("t")
    if kind not in (CONNECT_TYPE, HOSTED_TYPE):
        raise CaretError(
            400,
            "bad_request",
            f"unknown connect payload type {kind!r}: expected "
            f"{CONNECT_TYPE!r} or {HOSTED_TYPE!r}",
        )
    if not document.get("k"):
        raise CaretError(400, "bad_request", "connect payload is missing k")
    if kind == CONNECT_TYPE and not document.get("u"):
        raise CaretError(
            400, "bad_request", "an agent connect payload must carry u (the base URL)"
        )
    if kind == HOSTED_TYPE and document.get("u"):
        # The anti-redirect rule. A hosted code that names a URL is either
        # malformed or an attempt to aim a phone at someone else's server,
        # and the client rejects it outright rather than guessing which.
        raise CaretError(
            400,
            "bad_request",
            "a hosted connect payload must omit u: the client pins its own "
            "canonical URL and refuses a payload that names a different one",
        )
    return document


def mask(payload: str) -> str:
    """What may appear in console output, a log line, or a bug report.

    Enough to tell two payloads apart when debugging; useless to anyone
    who reads it. Never print or log the payload itself.

    A short digest rather than the first and last few characters: those
    would be identical for every payload this backend emits — same prefix,
    same JSON shape — so they would identify nothing while still handing
    out real ciphertext."""
    body = payload[len(CONNECT_PREFIX) :] if payload.startswith(CONNECT_PREFIX) else payload
    if not body:
        return CONNECT_PREFIX + "… (empty)"
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f"{CONNECT_PREFIX}… ({len(body)} chars, redacted, #{fingerprint})"


__all__ = [
    "CONNECT_PREFIX",
    "CONNECT_TYPE",
    "decode_payload",
    "encode_payload",
    "mask",
    "normalise_base_url",
]
