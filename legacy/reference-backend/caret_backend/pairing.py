"""Pairing tokens: hand over access without handing over the key.

**This is a server-side extension, not part of the caret/v2 client
contract.** The Caret app configures itself by scanning a
`caret-connect:v1:` code — see `connect.py`, which is what the setup flow
offers and what the client implements. Nothing in the shipped iOS client
claims a pairing token.

What this is for is the case where handing a device a durable key is the
wrong trade: your own client, a shared machine, a device you expect to
hand back. A `connect.py` payload is the API key in another encoding, and
it stays valid until the key is rotated. This module's alternative is a
**pairing token**: a fresh random secret that is

  * short-lived — five minutes by default, fifteen at the outside;
  * single-use — claiming it consumes it, and the second claim fails;
  * bound to one server URL — a token minted for one backend cannot be
    replayed against another, and a client that scanned a different URL
    than the token was minted for is refused rather than silently
    redirected;
  * revocable — individually or all at once, without touching the API
    key;
  * never stored — the registry is in memory, so a restart invalidates
    every pending pairing. That is the correct behaviour, not a
    limitation: the alternative is writing short-lived secrets to disk,
    where they would outlive their own expiry.

The client receives the token out of band, POSTs it back over TLS to
`/v2/pairing/claim`, and gets the durable API key in the response body —
one TLS-protected exchange, with the credential never rendered as pixels.

Every failure is a distinct, machine-readable code so a client can say
what actually went wrong instead of "pairing failed":

    pairing_token_invalid    no such token (or malformed)
    pairing_token_expired    past its expiry
    pairing_token_consumed   already claimed once
    pairing_token_revoked    revoked by the operator
    pairing_url_mismatch     scanned URL is not the one it was minted for
    backend_not_ready        the backend is not a valid Caret backend yet
    pairing_disabled         pairing turned off (CARET_PAIRING=off)

Note the last two. Pairing a device to a backend that cannot take
dictation would hand the user a keyboard with the microphone missing and
no explanation, so minting refuses while any readiness blocker stands —
see `Backend.readiness`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

from .errors import CaretError

PAIRING_SCHEME = "caret"
PAIRING_HOST = "pair"
PAIRING_PAYLOAD_VERSION = "1"

DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 900
# Enough for a few retries and a second device; low enough that a stuck
# script cannot fill memory with live credentials.
MAX_ACTIVE_TOKENS = 16
TOKEN_BYTES = 24  # 192 bits, url-safe base64 → 32 characters


def normalise_server_url(url: str) -> str:
    """Canonical form for binding and comparison.

    Scheme and host are case-insensitive per RFC 3986 and a trailing slash
    is meaningless on a base URL, so both are normalised before a token is
    bound — otherwise `https://Mac.ts.net/` and `https://mac.ts.net` would
    look like different servers to the same user."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise CaretError(
            400,
            "bad_request",
            f"server_url must be http:// or https://, got {url!r}",
        )
    if not parsed.hostname:
        raise CaretError(400, "bad_request", f"server_url has no host: {url!r}")
    host = parsed.hostname.lower()
    if parsed.scheme.lower() == "http" and host not in ("127.0.0.1", "::1", "localhost"):
        # Pairing over plaintext would put the API key on the wire in
        # clear, which is the one thing this flow exists to avoid. Loopback
        # is allowed so the flow is testable before TLS is in front.
        raise CaretError(
            400,
            "bad_request",
            "server_url must be https:// for anything but loopback: the claim "
            "response carries the API key, so pairing over plaintext would "
            "publish it",
        )
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{netloc}{path}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class PairingRecord:
    """One pairing token. The token itself is never kept — only its digest,
    so a memory dump or a debug log of this object cannot be replayed."""

    token_id: str
    token_digest: str
    server_url: str
    api_key: str
    created_at: float
    expires_at: float
    claimed_at: float | None = None
    revoked_at: float | None = None

    def state(self, now: float) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.claimed_at is not None:
            return "claimed"
        if now >= self.expires_at:
            return "expired"
        return "pending"

    def public(self, now: float) -> dict:
        """Metadata safe to list: no token, no API key."""
        return {
            "token_id": self.token_id,
            "server_url": self.server_url,
            "state": self.state(now),
            "created_at": int(self.created_at),
            "expires_at": int(self.expires_at),
        }

    def payload(self, token: str) -> str:
        """The URI the QR encodes.

        Deliberately compact — every character is a QR module, and a code
        that needs a version-20 symbol is a code nobody can scan from
        across a desk. `v` lets a future client recognise a payload shape
        it does not understand and say so."""
        return (
            f"{PAIRING_SCHEME}://{PAIRING_HOST}"
            f"?v={PAIRING_PAYLOAD_VERSION}"
            f"&u={quote(self.server_url, safe='')}"
            f"&t={token}"
        )


@dataclass
class PairingRegistry:
    """In-memory store of live pairing tokens. See the module docstring for
    why it is deliberately not persisted."""

    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_active: int = MAX_ACTIVE_TOKENS
    _records: dict[str, PairingRecord] = field(default_factory=dict)

    # ------------------------------------------------------------- minting

    def mint(
        self,
        *,
        server_url: str,
        api_key: str,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> tuple[str, PairingRecord]:
        """Create a token. Returns `(token, record)` — the plaintext token
        is returned once here and never recoverable afterwards."""
        now = time.time() if now is None else now
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise CaretError(
                400,
                "bad_request",
                f"ttl_seconds must be between {MIN_TTL_SECONDS} and "
                f"{MAX_TTL_SECONDS}; a pairing window longer than that is a "
                "standing invitation, not a setup step",
            )
        if not api_key:
            raise CaretError(
                409,
                "backend_not_ready",
                "cannot mint a pairing token: this backend has no API key to "
                "hand over",
            )
        self.prune(now)
        if len(self._live(now)) >= self.max_active:
            raise CaretError(
                429,
                "rate_limited",
                f"more than {self.max_active} pairing tokens are already live; "
                "revoke one or wait for them to expire",
                retryable=True,
            )
        token = secrets.token_urlsafe(TOKEN_BYTES)
        record = PairingRecord(
            token_id="pt_" + secrets.token_hex(4),
            token_digest=_digest(token),
            server_url=normalise_server_url(server_url),
            api_key=api_key,
            created_at=now,
            expires_at=now + ttl,
        )
        self._records[record.token_id] = record
        return token, record

    # ------------------------------------------------------------ claiming

    def claim(
        self, token: str, *, server_url: str, now: float | None = None
    ) -> PairingRecord:
        """Consume a token and return its record, or raise the specific
        reason it could not be consumed."""
        now = time.time() if now is None else now
        record = self._find(token)
        if record is None:
            raise CaretError(
                401,
                "pairing_token_invalid",
                "no such pairing token: generate a new QR on the server",
            )
        state = record.state(now)
        if state == "revoked":
            raise CaretError(
                410, "pairing_token_revoked", "this pairing token was revoked"
            )
        if state == "claimed":
            raise CaretError(
                409,
                "pairing_token_consumed",
                "this pairing token was already used: pairing tokens are "
                "single-use, so generate a new QR for another device",
            )
        if state == "expired":
            raise CaretError(
                410,
                "pairing_token_expired",
                "this pairing token expired: generate a new QR on the server",
            )
        if normalise_server_url(server_url) != record.server_url:
            # Not a formality. If a client can claim a token against a URL
            # the operator never minted it for, an attacker who relays the
            # QR gets the key delivered to a server of their choosing.
            raise CaretError(
                400,
                "pairing_url_mismatch",
                "this pairing token was issued for a different server URL",
            )
        record.claimed_at = now
        return record

    # ----------------------------------------------------------- lifecycle

    def revoke(self, token_id: str, *, now: float | None = None) -> PairingRecord:
        now = time.time() if now is None else now
        record = self._records.get(token_id)
        if record is None:
            raise CaretError(
                404, "pairing_token_invalid", f"no such pairing token: {token_id}"
            )
        if record.revoked_at is None:
            record.revoked_at = now
        return record

    def revoke_all(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        count = 0
        for record in self._records.values():
            if record.state(now) == "pending":
                record.revoked_at = now
                count += 1
        return count

    def list(self, *, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        self.prune(now)
        return [r.public(now) for r in sorted(
            self._records.values(), key=lambda r: r.created_at
        )]

    def prune(self, now: float | None = None) -> int:
        """Drop records that can never be claimed again. Keeps the API key
        copy in a claimed record from lingering longer than it must."""
        now = time.time() if now is None else now
        dead = [
            tid
            for tid, record in self._records.items()
            if record.state(now) != "pending"
            # A short grace window so a client that retries a claim gets
            # `pairing_token_consumed` rather than a confusing "no such
            # token" — the difference matters when debugging a phone.
            and now - max(
                record.claimed_at or 0, record.revoked_at or 0, record.expires_at
            ) > 60
        ]
        for tid in dead:
            del self._records[tid]
        return len(dead)

    # ------------------------------------------------------------ internal

    def _live(self, now: float) -> list[PairingRecord]:
        return [r for r in self._records.values() if r.state(now) == "pending"]

    def _find(self, token: str) -> PairingRecord | None:
        if not token:
            return None
        wanted = _digest(token)
        for record in self._records.values():
            if hmac.compare_digest(record.token_digest, wanted):
                return record
        return None


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "PairingRecord",
    "PairingRegistry",
    "normalise_server_url",
]
