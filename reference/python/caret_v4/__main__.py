"""The command line: `serve` runs a backend, `conform` interrogates one.

    python3 -m caret_v4 serve   --addr 127.0.0.1:8080 --keys dev-key
    python3 -m caret_v4 conform --url https://backend.example --key KEY

With no lane flags `serve` answers /dictate with the built-in loopback
providers: no model, no network, no API keys of anyone's. That is enough
to point a client at, to run the checker against, and to develop against
on a plane. Wire real providers with --stt, --agent, --image and
--cleanup; the package README has the lane grammar.

Pass --tls-cert and --tls-key to serve HTTPS directly, or leave them off
and terminate TLS in front. Clients require https/wss, so plain HTTP is
for loopback development only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import sys
import threading

from .conformance import Checker, report
from .server import (
    DEFAULT_MAX_AUDIO_SECONDS,
    DEFAULT_MAX_FRAME_BYTES,
    VERSION,
    Server,
    make_http_server,
)

EXIT_OK = 0
EXIT_FAILED_CHECKS = 1
EXIT_USAGE = 2


def env_or(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback


def env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


def serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("caret_v4")

    credentials = [k.strip() for k in args.keys.split(",") if k.strip()]
    if not credentials:
        log.warning("no credentials configured: /health will report not_ready and every "
                    "operation will be refused. Pass --keys or set CARET_API_KEYS")

    host, _, port = args.addr.rpartition(":")
    if not host or not port.isdigit():
        print(f"--addr must look like host:port, got {args.addr!r}", file=sys.stderr)
        return EXIT_USAGE

    try:
        server = Server(
            service=args.service,
            api_keys=credentials,
            stt=args.stt,
            agent=args.agent,
            image=args.image,
            cleanup=args.cleanup,
            max_audio_seconds=args.max_audio_seconds,
            max_frame_bytes=args.max_frame_bytes,
            lane_timeout=args.lane_timeout,
            spec_dir=args.cleanup_spec_dir,
            logger=log,
        )
    except Exception as exc:  # a bad lane spec, mostly
        print(f"cannot start: {exc}", file=sys.stderr)
        return EXIT_USAGE

    http_server = make_http_server(server, host.strip("[]"), int(port))

    scheme = "http"
    if args.tls_cert and args.tls_key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            context.load_cert_chain(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            print(f"cannot load the TLS certificate: {exc}", file=sys.stderr)
            return EXIT_USAGE
        http_server.socket = context.wrap_socket(http_server.socket, server_side=True)
        scheme = "https"
    elif args.tls_cert or args.tls_key:
        print("--tls-cert and --tls-key must be passed together", file=sys.stderr)
        return EXIT_USAGE

    bound = http_server.socket.getsockname()
    log.info("caret/v4 reference backend on %s://%s:%s — service %r",
             scheme, bound[0], bound[1], args.service)
    log.info("routes: %s", " ".join(server.routes()))
    if scheme == "http":
        log.info("serving plain HTTP: a conforming client requires https/wss, so put TLS "
                 "in front before this faces a keyboard")

    def shutdown(*_ignored) -> None:
        threading.Thread(target=http_server.shutdown, daemon=True).start()

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, shutdown)
    try:
        http_server.serve_forever(poll_interval=0.2)
    finally:
        http_server.server_close()
        log.info("stopped")
    return EXIT_OK


def conform(args: argparse.Namespace) -> int:
    checker = Checker(
        base_url=args.url,
        api_key=args.key,
        allow_insecure=args.insecure,
        verify_tls=not args.insecure_skip_verify,
        timeout=args.timeout,
    )
    try:
        results = checker.run()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps({"base_url": args.url,
                          "results": [r.as_json() for r in results]}, indent=2))
        return EXIT_OK if all(r.status != "fail" for r in results) else EXIT_FAILED_CHECKS
    return EXIT_OK if report(results, sys.stdout) else EXIT_FAILED_CHECKS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m caret_v4",
        description="A caret/v4 reference backend and conformance checker.",
    )
    parser.add_argument("--version", action="version", version=f"caret-v4-reference {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("serve", help="serve the protocol")
    run.add_argument("--addr", default=env_or("CARET_ADDR", "127.0.0.1:8080"),
                     help="listen address (default 127.0.0.1:8080)")
    run.add_argument("--keys", default=env_or("CARET_API_KEYS", ""),
                     help="comma-separated bearer credentials; empty means the backend "
                          "reports not_ready and refuses every operation")
    run.add_argument("--stt", default=env_or("CARET_STT", "loopback"),
                     help="speech-to-text lane: loopback, command:<argv>, or an https URL")
    run.add_argument("--agent", default=env_or("CARET_AGENT", ""),
                     help="agent lane for /ask; empty turns /ask off")
    run.add_argument("--image", default=env_or("CARET_IMAGE", ""),
                     help="image lane for /imagine; empty turns /imagine off")
    run.add_argument("--cleanup", default=env_or("CARET_CLEANUP", ""),
                     help="cleanup lane; empty means dictation is returned unpolished")
    run.add_argument("--service", default=env_or("CARET_SERVICE", "caret-v4-reference-python"),
                     help="service name reported on /health")
    run.add_argument("--cleanup-spec-dir", default=env_or("CARET_CLEANUP_SPEC_DIR", ""),
                     help="path to spec/cleanup/v1; found automatically from the repository")
    run.add_argument("--tls-cert", default=env_or("CARET_TLS_CERT", ""),
                     help="TLS certificate file; serves HTTPS when set with --tls-key")
    run.add_argument("--tls-key", default=env_or("CARET_TLS_KEY", ""),
                     help="TLS private key file")
    run.add_argument("--max-audio-seconds", type=int,
                     default=env_int("CARET_MAX_AUDIO_SECONDS", DEFAULT_MAX_AUDIO_SECONDS),
                     help="longest single operation, in seconds")
    run.add_argument("--max-frame-bytes", type=int,
                     default=env_int("CARET_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES),
                     help="largest accepted binary audio frame")
    run.add_argument("--lane-timeout", type=float, default=120.0,
                     help="how long a provider may take before the operation fails")
    run.add_argument("--verbose", action="store_true", help="log per-request debug lines")
    run.set_defaults(func=serve)

    check = sub.add_parser("conform", help="check a backend against the protocol")
    check.add_argument("--url", default=env_or("CARET_BASE_URL", ""),
                       help="base URL of the backend under test")
    check.add_argument("--key", default=env_or("CARET_API_KEY", ""),
                       help="bearer credential the backend accepts")
    check.add_argument("--insecure", action="store_true",
                       help="allow an http:// base URL, for a loopback backend")
    check.add_argument("--insecure-skip-verify", action="store_true",
                       help="do not verify the server certificate, for a self-signed one")
    check.add_argument("--json", action="store_true", help="write results as JSON")
    check.add_argument("--timeout", type=float, default=30.0,
                       help="per-operation timeout in seconds")
    check.set_defaults(func=conform)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "conform" and not args.url:
        parser.error("conform needs --url (or CARET_BASE_URL)")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
