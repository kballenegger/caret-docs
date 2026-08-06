"""The Caret error envelope.

Every non-2xx response on every endpoint is::

    {"error": {"code": …, "message": …, "retryable": …}, "request_id": …}

`code` is machine-readable and the list is append-only, so clients that meet
an unknown code fall back to `retryable`. Nothing else may ever reach the
wire — a bare traceback or an HTML error page would break every client.
"""

from __future__ import annotations

import secrets


def new_request_id() -> str:
    """Server-generated correlation id, present on every response."""
    return "req_" + secrets.token_hex(6)


class CaretError(Exception):
    """A failure that is already contract-shaped.

    Raise this anywhere; the server turns it into the envelope. Anything
    else that escapes a handler becomes `500 internal_error`, so an
    unexpected bug still leaves the client with a valid response.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id

    def body(self, request_id: str | None = None) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
            "request_id": self.request_id or request_id,
        }


def bad_request(message: str) -> CaretError:
    return CaretError(400, "bad_request", message)


def unauthorized(message: str = "missing or invalid API key") -> CaretError:
    return CaretError(401, "unauthorized", message)


def input_invalid(message: str) -> CaretError:
    return CaretError(422, "input_invalid", message)


def unknown_session(session_id: str) -> CaretError:
    return CaretError(404, "unknown_session", f"no such session: {session_id}")


def session_expired() -> CaretError:
    return CaretError(410, "session_expired", "session expired; open a new one")


def session_conflict(consumer: str) -> CaretError:
    return CaretError(
        409, "session_conflict", f"session already consumed by {consumer}"
    )


def internal_error(message: str = "internal error") -> CaretError:
    return CaretError(500, "internal_error", message, retryable=True)
