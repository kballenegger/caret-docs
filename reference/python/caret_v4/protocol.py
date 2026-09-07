"""The caret/v4 wire vocabulary: names, limits, and the error table.

Nothing here does any work. It is the part of the protocol that both
sides must agree on letter for letter, kept in one file so a change to
the contract is a change to one file. The Go reference implementation
has the same file, with the same values, for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_NAME = "caret/v4"
PROTOCOL_VERSION = 4

ROUTE_DICTATE = "dictate"
ROUTE_ASK = "ask"
ROUTE_IMAGINE = "imagine"
ROUTES = (ROUTE_DICTATE, ROUTE_ASK, ROUTE_IMAGINE)

# §10's error table. The close code is what a client switches on when the
# terminal event never arrives, so the two must never drift apart.
ERR_UNAUTHORIZED = "unauthorized"
ERR_RATE_LIMITED = "rate_limited"
ERR_NOT_SUPPORTED = "not_supported"
ERR_PROTOCOL_ERROR = "protocol_error"
ERR_BAD_REQUEST = "bad_request"
ERR_TIMEOUT = "timeout"
ERR_AUDIO_INCOMPLETE = "audio_incomplete"
ERR_AUDIO_TOO_LONG = "audio_too_long"
ERR_AUDIO_TOO_SHORT = "audio_too_short"
ERR_NO_SPEECH_DETECTED = "no_speech_detected"
ERR_TRANSCRIPTION_FAILED = "transcription_failed"
ERR_GENERATION_FAILED = "generation_failed"
ERR_INTERNAL_ERROR = "internal_error"

CLOSE_CODES = {
    ERR_UNAUTHORIZED: 4401,
    ERR_RATE_LIMITED: 4429,
    ERR_NOT_SUPPORTED: 4404,
    ERR_PROTOCOL_ERROR: 4400,
    ERR_BAD_REQUEST: 4400,
    ERR_TIMEOUT: 4408,
    ERR_AUDIO_INCOMPLETE: 4409,
    ERR_AUDIO_TOO_LONG: 4413,
    ERR_AUDIO_TOO_SHORT: 4422,
    ERR_NO_SPEECH_DETECTED: 4422,
    ERR_TRANSCRIPTION_FAILED: 4503,
    ERR_GENERATION_FAILED: 4503,
    ERR_INTERNAL_ERROR: 4500,
}

# Retryable means "the same request, sent again, might work". A bad
# credential will not fix itself; a rate limit will.
RETRYABLE = {
    ERR_RATE_LIMITED,
    ERR_TIMEOUT,
    ERR_AUDIO_INCOMPLETE,
    ERR_TRANSCRIPTION_FAILED,
    ERR_GENERATION_FAILED,
    ERR_INTERNAL_ERROR,
}

#: The only audio the protocol carries.
AUDIO_CODEC = "pcm16"
AUDIO_SAMPLE_RATE_HZ = 16000
AUDIO_CHANNELS = 1
BYTES_PER_SECOND = AUDIO_SAMPLE_RATE_HZ * 2 * AUDIO_CHANNELS

#: §6's closed sets.
ASPECT_RATIOS = ("1:1", "3:2", "2:3")
QUALITIES = ("low", "medium", "high")

MAX_VOCABULARY_ENTRY_CHARS = 64


def close_code_for(code: str) -> int:
    """The WebSocket close code §10 pairs with an error code."""
    try:
        return CLOSE_CODES[code]
    except KeyError:  # pragma: no cover — a typo in an error constant
        raise KeyError(f"no close code for error {code!r}") from None


def retryable_for(code: str) -> bool:
    return code in RETRYABLE


class OpError(Exception):
    """One of §10's failures, on its way to the terminal error event."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def normalize_vocabulary(entries, limit: int) -> list[str]:
    """§7's shape rules, applied forgivingly where that changes nothing.

    Unique strings of 1–64 characters, at most `limit` of them, earlier
    entries higher priority. Surrounding whitespace is a client-side
    accident rather than part of a term, so it is trimmed, and a repeat
    is dropped rather than refused — the list is priority-ordered, so
    first occurrence wins either way. What is still out of range after
    that is the client's mistake and becomes `bad_request`.
    """
    if entries is None:
        return []
    if not isinstance(entries, list) or any(not isinstance(e, str) for e in entries):
        raise OpError(ERR_BAD_REQUEST, "vocabulary must be an array of strings")
    if len(entries) > limit:
        raise OpError(ERR_BAD_REQUEST, f"at most {limit} vocabulary entries")
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        entry = entry.strip()
        if not 1 <= len(entry) <= MAX_VOCABULARY_ENTRY_CHARS:
            raise OpError(ERR_BAD_REQUEST, "vocabulary entries are 1-64 characters")
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


@dataclass
class AudioSpec:
    """What the client says it sent, and what the server says it heard.

    The equality of those two is the whole of §8's completeness check.
    """

    frames: int = 0
    bytes: int = 0
    duration_ms: int = 0

    def as_json(self) -> dict:
        return {"frames": self.frames, "bytes": self.bytes, "duration_ms": self.duration_ms}
