"""`python -m caret_backend` — start the reference backend.

Configuration is environment variables only (see the README), because that
is what every process supervisor already knows how to set and it keeps the
API key out of your shell history and out of this repository.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .errors import CaretError
from .server import backend_from_env, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caret-backend", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CARET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CARET_PORT", "8787")))
    parser.add_argument("--log-level", default=os.environ.get("CARET_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, print the resolved adapters and "
        "capabilities, and exit (0 = ok)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        backend = backend_from_env()
    except CaretError as exc:
        # Adapter selection failed — a configuration problem, not a bug.
        print(f"caret-backend: configuration error: {exc.message}", file=sys.stderr)
        return 2
    caps = backend.capabilities()

    if args.check:
        print(
            "config ok: agent={} stt={} imagine={} keys={} capabilities={}".format(
                getattr(backend.agent, "name", "?"),
                getattr(backend.transcriber, "name", "none")
                if caps["dictation"]
                else "none",
                getattr(backend.image_generator, "name", "off") if caps["imagine"] else "off",
                len(backend.api_keys),
                caps,
            )
        )
        if not backend.api_keys:
            print(
                "warning: CARET_API_KEYS is empty — the backend fails closed "
                "(401 everywhere, health reports degraded)"
            )
        return 0
    server = make_server(backend, args.host, args.port)
    logging.getLogger("caret").info(
        "listening on http://%s:%d  agent=%s stt=%s imagine=%s",
        args.host,
        args.port,
        getattr(backend.agent, "name", "?"),
        getattr(backend.transcriber, "name", "none"),
        getattr(backend.image_generator, "name", "off") if caps["imagine"] else "off",
    )
    # Default host is loopback: exposing this process directly to the
    # internet would publish a plaintext HTTP endpoint. Terminate TLS in a
    # reverse proxy and point it here.
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger("caret").info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
