"""A small, complete `caret/v1` backend. Python standard library only.

    caret_backend.server      contract: routing, auth, validation, async jobs
    caret_backend.store       filesystem state: sessions, chunks, jobs
    caret_backend.adapters    the boundary where your agent plugs in
    caret_backend.errors      the error envelope

Run it with `python -m caret_backend`.
"""

from .server import Backend, backend_from_env, make_server

__all__ = ["Backend", "backend_from_env", "make_server"]
__version__ = "1.2.0"
