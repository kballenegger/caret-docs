"""`python -m caret_backend` — start the reference backend.

Configuration is environment variables only (see the README), because that
is what every process supervisor already knows how to set and it keeps the
API key out of your shell history and out of this repository.

Four modes:

    python3 -m caret_backend                 serve
    python3 -m caret_backend --check         validate config and exit
    python3 -m caret_backend --qr --url …    show the connection QR
    python3 -m caret_backend --pair --url …  show a one-time pairing QR

The two QR modes differ in what the code carries, and the difference
matters — see `connect.py` and `pairing.py`. Both are explicit: neither
ever runs as a side effect of starting or checking the server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import connect, qrcode
from .errors import CaretError
from .server import backend_from_env, make_server


def _readiness_lines(backend) -> list[str]:
    readiness = backend.readiness()
    if readiness["ready"]:
        return []
    return [f"  not ready: [{b['code']}] {b['message']}" for b in readiness["blockers"]]


def _api_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("CARET_API_KEYS", "").split(",") if k.strip()]


def _qr(args) -> int:
    """Show the connection QR: base URL and API key, in one scan.

    Deliberately explicit — this never runs as part of `--check` or of
    starting the server. The payload it renders is a live credential (see
    `connect.py`), so it is printed as an image, masked in text, and never
    logged."""
    keys = _api_keys()
    if not keys:
        print(
            "caret-backend: CARET_API_KEYS is not set — there is no key to put "
            "in the QR",
            file=sys.stderr,
        )
        return 2

    # A QR is only worth showing for a backend that works. Pointing a phone
    # at one that cannot take dictation produces a keyboard with the
    # microphone missing and no explanation of why.
    try:
        backend = backend_from_env()
    except CaretError as exc:
        print(f"caret-backend: configuration error: {exc.message}", file=sys.stderr)
        return 2
    blockers = _readiness_lines(backend)
    if blockers and not args.force:
        print("caret-backend: NOT READY — refusing to hand out a connection", file=sys.stderr)
        for line in blockers:
            print(line, file=sys.stderr)
        print("  fix the above, or pass --force to show it anyway", file=sys.stderr)
        return 1

    try:
        payload = connect.encode_payload(args.url, keys[0])
        matrix = qrcode.encode(payload)
    except (CaretError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        print(f"caret-backend: cannot build the connection QR: {message}", file=sys.stderr)
        return 1

    if args.save:
        target = Path(args.save).expanduser()
        try:
            target.write_bytes(qrcode.render_png(matrix))
            # The file is the credential. Owner-only, from the moment it
            # exists as far as this process can arrange it.
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            print(f"caret-backend: cannot write {target}: {exc}", file=sys.stderr)
            return 1
        print(f"  saved {target} (mode 0600) — delete it once the phone has scanned it")
    if not args.no_display:
        print()
        print(qrcode.render_text(matrix, plain=args.plain))
    print()
    print(f"  scan with Caret to connect to {connect.normalise_base_url(args.url)}")
    # Never the payload itself: it is the API key in another encoding.
    print(f"  payload {connect.mask(payload)}")
    print()
    print("  This QR is a credential. Anyone who scans it — or photographs")
    print("  the screen, or finds the saved file — can connect as you, and")
    print("  keeps that access until you rotate CARET_API_KEYS. Do not put")
    print("  it in a ticket, a screenshot, or a screen share.")
    print("  For a code that expires and can be revoked without rotating the")
    print("  key, use --pair instead.")
    return 0


def _pair(args) -> int:
    """Mint a one-time pairing token against a running backend.

    An **extension**, not part of the client contract: the Caret app
    connects by scanning a `caret-connect:v1:` QR (`--qr`), and does not
    claim pairing tokens. This exists for operators who would rather not
    hand a device a durable key at all, and for anyone building their own
    client — the token expires, is single-use, and can be revoked without
    rotating the API key. It is deliberately not rendered as a QR, because
    a code the Caret app cannot scan is a trap.

    This talks to the HTTP API rather than reaching into the process,
    because pairing tokens live in the serving process's memory — a second
    process has no way to see them. It also means this command exercises
    exactly the contract a client would use, instead of a private
    shortcut."""
    keys = _api_keys()
    if not keys:
        print(
            "caret-backend: CARET_API_KEYS is not set — pairing hands over an "
            "API key, so there has to be one",
            file=sys.stderr,
        )
        return 2
    body = json.dumps({"server_url": args.url, "ttl_seconds": args.ttl}).encode()
    request = urllib.request.Request(
        args.local.rstrip("/") + "/v1/pairing/tokens",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {keys[0]}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"caret-backend: pairing refused (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            f"caret-backend: cannot reach the backend at {args.local}: {exc}\n"
            "  start it first (python3 -m caret_backend), or pass --local",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"  pairing token for {payload['server_url']}")
    print(f"    {payload['payload']}")
    print()
    print(f"  single use, expires {payload['expires_at']} "
          f"(in {payload['expires_in_seconds']}s)")
    print(f"  token id {payload['token_id']} — revoke with:")
    print(f"    curl -sX POST {args.local.rstrip('/')}/v1/pairing/revoke \\")
    print('      -H "Authorization: Bearer $CARET_API_KEYS" \\')
    print(f"      -H 'Content-Type: application/json' -d '{{\"token_id\":\"{payload['token_id']}\"}}'")
    print()
    print("  This is a one-time token, not your API key — a client exchanges")
    print("  it once at POST /v1/pairing/claim and it dies there.")
    print("  The Caret app does not use this flow; it scans a connection QR")
    print("  (python3 -m caret_backend --qr --url …). Pairing tokens are for")
    print("  your own clients and for out-of-band handover.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caret-backend", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CARET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CARET_PORT", "8787")))
    parser.add_argument("--log-level", default=os.environ.get("CARET_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration, print the resolved adapters and "
        "capabilities, and exit (0 = ready, 1 = not a valid backend yet, "
        "2 = configuration error)",
    )
    qr = parser.add_argument_group(
        "connection QR",
        "Show the QR the Caret app scans. Never automatic: the code it "
        "renders carries your API key and is valid until you rotate it.",
    )
    qr.add_argument(
        "--qr",
        action="store_true",
        help="display the caret-connect:v1 QR for this backend",
    )
    qr.add_argument(
        "--url",
        help="the https:// base URL the phone will use (required with --qr "
        "and --pair)",
    )
    qr.add_argument(
        "--save",
        metavar="PATH",
        help="also write the QR to PATH as a PNG, mode 0600 (delete it once "
        "the phone has scanned it)",
    )
    qr.add_argument(
        "--no-display",
        action="store_true",
        help="with --save, do not also draw the QR in the terminal",
    )
    qr.add_argument(
        "--plain",
        action="store_true",
        help="render the QR without ANSI colour (assumes a light background)",
    )
    qr.add_argument(
        "--force",
        action="store_true",
        help="show the QR even though the backend is not ready",
    )
    pair = parser.add_argument_group(
        "pairing tokens (extension)",
        "A one-time token instead of the key. The Caret app does not use "
        "this; it is for your own clients and out-of-band handover.",
    )
    pair.add_argument(
        "--pair",
        action="store_true",
        help="mint a single-use pairing token against a running backend",
    )
    pair.add_argument(
        "--local",
        default=None,
        help="where the running backend is, as seen from this machine "
        "(default http://<host>:<port>)",
    )
    pair.add_argument("--ttl", type=int, default=300, help="token lifetime in seconds")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if args.qr and args.pair:
        parser.error("--qr and --pair issue different things; run one at a time")
    if args.qr:
        if not args.url:
            parser.error("--qr requires --url (the base URL the phone will use)")
        return _qr(args)
    if args.pair:
        if not args.url:
            parser.error("--pair requires --url (the base URL the phone will use)")
        if args.local is None:
            args.local = f"http://{args.host}:{args.port}"
        return _pair(args)

    try:
        backend = backend_from_env()
    except CaretError as exc:
        # Adapter selection failed — a configuration problem, not a bug.
        print(f"caret-backend: configuration error: {exc.message}", file=sys.stderr)
        return 2
    caps = backend.capabilities()

    if args.check:
        print(
            "config: agent={} stt={} imagine={} keys={} capabilities={}".format(
                getattr(backend.agent, "name", "?") if caps["draft"] else "none",
                getattr(backend.transcriber, "name", "none")
                if caps["dictation"]
                else "none",
                getattr(backend.image_generator, "name", "off") if caps["imagine"] else "off",
                len(backend.api_keys),
                caps,
            )
        )
        blockers = _readiness_lines(backend)
        if blockers:
            # Dictation is mandatory: a backend without it is not a Caret
            # backend, and `--check` must not exit 0 on one. Ask and Imagine
            # being off are fine and say nothing here.
            print("caret-backend: NOT READY", file=sys.stderr)
            for line in blockers:
                print(line, file=sys.stderr)
            return 1
        print("config ok: ready")
        # Validated — so offer the last step, rather than leaving the
        # operator to type a 43-character key into a phone. Offered, not
        # done: the QR is a credential and only appears when asked for.
        print()
        print("  connect a phone by scanning a QR:")
        print("    python3 -m caret_backend --qr --url https://your-backend.example")
        print("  (that code carries your API key — see --qr for the warning)")
        return 0

    for line in _readiness_lines(backend):
        # Serving anyway — an operator mid-setup wants the endpoint up so
        # they can watch health change — but never silently.
        logging.getLogger("caret").warning(line.strip())
    server = make_server(backend, args.host, args.port)
    logging.getLogger("caret").info(
        "listening on http://%s:%d  agent=%s stt=%s imagine=%s",
        args.host,
        args.port,
        getattr(backend.agent, "name", "?") if caps["draft"] else "none",
        getattr(backend.transcriber, "name", "none") if caps["dictation"] else "none",
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
