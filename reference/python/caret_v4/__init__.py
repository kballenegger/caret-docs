"""A runnable caret/v4 reference backend and conformance checker.

The Go implementation in `reference/go` is the recommended default and
the one the docs lead with. This package is the supported alternative:
same protocol, same checks, same cleanup-spec digest, standard library
only.

    python3 -m caret_v4 serve   --addr 127.0.0.1:8080 --keys dev-key
    python3 -m caret_v4 conform --url http://127.0.0.1:8080 --key dev-key --insecure

Running each language's checker against the other language's server is
how this repository knows the two agree about the protocol rather than
merely about themselves.
"""
from .protocol import (
    CLOSE_CODES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    RETRYABLE,
    ROUTES,
    OpError,
    close_code_for,
    retryable_for,
)
from .server import Server, make_handler, make_http_server

__version__ = "1.0.0"

__all__ = [
    "CLOSE_CODES",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "RETRYABLE",
    "ROUTES",
    "OpError",
    "Server",
    "close_code_for",
    "make_handler",
    "make_http_server",
    "retryable_for",
    "__version__",
]
