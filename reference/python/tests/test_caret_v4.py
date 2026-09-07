"""Contract tests: a real server on a real socket, exercised over the wire.

Nothing here mocks the transport. Every test starts a backend on an
ephemeral loopback port and talks to it with the same client code the
conformance checker uses, because the parts of a protocol that break are
the parts between two processes.

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_v4 import cleanup, lanes, protocol, ws  # noqa: E402
from caret_v4.conformance import (  # noqa: E402
    Checker,
    chunk_audio,
    silence,
    start_frame,
    tone,
    totals,
)
from caret_v4.server import Server, make_http_server  # noqa: E402

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec" / "cleanup" / "v1"

QUIET = logging.getLogger("caret_v4.tests")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


@contextmanager
def backend(**config):
    """A running backend on an ephemeral port, torn down afterwards."""
    config.setdefault("api_keys", ["test-key"])
    config.setdefault("spec_dir", str(SPEC_DIR))
    config.setdefault("logger", QUIET)
    server = Server(**config)
    http_server = make_http_server(server, "127.0.0.1", 0)
    host, port = http_server.socket.getsockname()[:2]
    thread = threading.Thread(target=http_server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}"
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=5)


@contextmanager
def full_backend(**overrides):
    config = {"stt": "loopback", "agent": "loopback", "image": "loopback",
              "cleanup": "loopback"}
    config.update(overrides)
    with backend(**config) as running:
        yield running


def checker_for(base_url: str, key: str = "test-key") -> Checker:
    return Checker(base_url, api_key=key, allow_insecure=True, timeout=20.0)


class ConformanceTest(unittest.TestCase):
    """The checker is the specification made executable, so the server's
    own suite runs it rather than restating it."""

    def assert_all_pass(self, base_url: str) -> None:
        results = checker_for(base_url).run()
        failures = [r for r in results if r.status == "fail"]
        self.assertEqual([], [f"{r.name}: {r.detail}" for r in failures])
        self.assertGreater(len(results), 15, "the checker ran suspiciously few checks")

    def test_all_routes(self):
        with full_backend() as (_, url):
            self.assert_all_pass(url)

    def test_dictate_only(self):
        """/ask and /imagine off: the checker holds the backend to its own
        health document and expects not_supported from both."""
        with backend(stt="loopback", cleanup="loopback") as (_, url):
            self.assert_all_pass(url)

    def test_no_cleanup_lane(self):
        with backend(stt="loopback") as (_, url):
            self.assert_all_pass(url)


class HealthTest(unittest.TestCase):
    def test_shape(self):
        with full_backend() as (_, url):
            document = checker_for(url)._fetch_health("test-key")
        for key in ("protocol", "status", "service", "version", "time", "auth",
                    "capabilities", "limits"):
            self.assertIn(key, document)
        self.assertEqual(protocol.PROTOCOL_NAME, document["protocol"])
        self.assertEqual("ok", document["status"])
        self.assertEqual(cleanup.SPEC_NAME, document["cleanup"]["spec"])
        # §9: the credential is never echoed anywhere, including here.
        self.assertNotIn("test-key", json.dumps(document))

    def test_auth_report(self):
        with full_backend() as (_, url):
            check = checker_for(url)
            self.assertIsNone(check._fetch_health("")["auth"]["valid"],
                              "no credential presented is not the same as an invalid one")
            self.assertFalse(check._fetch_health("")["auth"]["presented"])
            self.assertTrue(check._fetch_health("test-key")["auth"]["valid"])
            self.assertFalse(check._fetch_health("nope")["auth"]["valid"])

    def test_not_ready_without_credentials(self):
        """§2: a backend nobody can authenticate to says so on /health
        rather than looking healthy and refusing every operation."""
        with backend(api_keys=[], stt="loopback") as (_, url):
            document = checker_for(url)._fetch_health("")
        self.assertEqual("not_ready", document["status"])
        self.assertIn("no_credentials", [b["code"] for b in document["blockers"]])

    def test_not_ready_without_speech(self):
        with backend(stt="") as (_, url):
            document = checker_for(url)._fetch_health("test-key")
        self.assertEqual("not_ready", document["status"])
        self.assertFalse(document["capabilities"]["dictate"])
        self.assertIn("no_stt", [b["code"] for b in document["blockers"]])


class ErrorTableTest(unittest.TestCase):
    """§10's table, one wire operation per row: the code, the close code,
    and whether the client is told to retry."""

    AUDIO_START = ('{"type":"start","protocol":%s,%s"input":{"type":"audio",'
                   '"codec":"pcm16","sample_rate_hz":%d,"channels":%d}}')

    def test_table(self):
        chunks = chunk_audio(tone(2000), 200)
        short = chunk_audio(tone(80), 40)
        quiet = chunk_audio(silence(1200), 200)
        cases = [
            ("finalize before start", dict(raw_first='{"type":"finalize"}'),
             protocol.ERR_PROTOCOL_ERROR),
            ("wrong protocol version",
             dict(raw_first=self.AUDIO_START % (3, '"client_request_id":"x",', 16000, 1)),
             protocol.ERR_PROTOCOL_ERROR),
            ("not JSON", dict(raw_first="{{{"), protocol.ERR_PROTOCOL_ERROR),
            ("unsupported sample rate",
             dict(raw_first=self.AUDIO_START % (4, '"client_request_id":"x",', 44100, 1)),
             protocol.ERR_BAD_REQUEST),
            ("stereo audio",
             dict(raw_first=self.AUDIO_START % (4, '"client_request_id":"x",', 16000, 2)),
             protocol.ERR_BAD_REQUEST),
            ("missing client_request_id",
             dict(raw_first=self.AUDIO_START % (4, "", 16000, 1)),
             protocol.ERR_BAD_REQUEST),
            ("finalize totals disagree",
             dict(start=start_frame("totals"), audio=chunks,
                  finalize={"type": "finalize",
                            "audio": {"frames": 99, "bytes": 99999, "duration_ms": 3000}}),
             protocol.ERR_AUDIO_INCOMPLETE),
            ("too short",
             dict(start=start_frame("short"), audio=short,
                  finalize={"type": "finalize", "audio": totals(short)}),
             protocol.ERR_AUDIO_TOO_SHORT),
            ("silence",
             dict(start=start_frame("silence"), audio=quiet,
                  finalize={"type": "finalize", "audio": totals(quiet)}),
             protocol.ERR_NO_SPEECH_DETECTED),
            ("bad credential", dict(key="wrong", start=start_frame("auth")),
             protocol.ERR_UNAUTHORIZED),
        ]
        with full_backend() as (_, url):
            check = checker_for(url)
            for name, operation, code in cases:
                with self.subTest(name):
                    probe = check._operate(protocol.ROUTE_DICTATE, **operation)
                    self.assertEqual(code, probe.error_code,
                                     f"got {probe.terminal_kind!r} (transport: {probe.error})")
                    self.assertEqual(protocol.close_code_for(code), probe.close_code)
                    self.assertEqual(protocol.retryable_for(code),
                                     probe.terminal.get("retryable"))

            with self.subTest("bad aspect ratio"):
                probe = check._operate(
                    protocol.ROUTE_IMAGINE,
                    start={"type": "start", "protocol": 4, "client_request_id": "aspect",
                           "input": {"type": "text", "text": "a kingfisher"}},
                    finalize={"type": "finalize", "aspect_ratio": "16:9"},
                )
                self.assertEqual(protocol.ERR_BAD_REQUEST, probe.error_code)
                self.assertEqual(4400, probe.close_code)

    def test_audio_too_long(self):
        chunks = chunk_audio(tone(2000), 200)
        with full_backend(max_audio_seconds=1) as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE, start=start_frame("long"), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual(protocol.ERR_AUDIO_TOO_LONG, probe.error_code)
        self.assertEqual(4413, probe.close_code)

    def test_oversize_frame(self):
        chunks = chunk_audio(tone(2000), 500)
        with full_backend(max_frame_bytes=4096) as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE, start=start_frame("big"), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual(protocol.ERR_BAD_REQUEST, probe.error_code)

    def test_unsupported_route_is_not_supported(self):
        with backend(stt="loopback") as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_ASK,
                                              start=start_frame("unserved"))
        self.assertEqual(protocol.ERR_NOT_SUPPORTED, probe.error_code)
        self.assertEqual(4404, probe.close_code)


class VocabularyTest(unittest.TestCase):
    """§7: an advertised vocabulary reaches the recognizer *and* the
    cleanup glossary. A name heard correctly and then "corrected" by the
    polish pass is the same bug, one step later."""

    def test_reaches_the_recognizer(self):
        chunks = chunk_audio(tone(3000), 200)
        with full_backend() as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE,
                start=start_frame("vocab", vocabulary=["Kensington", "Sagrada Familia"]),
                audio=chunks, finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        raw = probe.terminal["result"]["raw_transcript"]
        self.assertIn("Kensington", raw)
        self.assertIn("Sagrada Familia", raw)

    def test_reaches_cleanup(self):
        spec = cleanup.load_spec(SPEC_DIR)
        prompt = spec.system_prompt(["Kensington"])
        self.assertIn("Kensington", prompt)
        self.assertIn(cleanup.GLOSSARY_SECTION_HEADER, prompt)

    def test_advertised_only_when_it_goes_somewhere(self):
        with backend(stt="loopback") as (server, _):
            self.assertTrue(server.vocabulary_capable())
        with backend(stt="") as (server, _):
            self.assertFalse(server.vocabulary_capable(),
                             "a backend with no recognizer cannot honour a vocabulary")

    def test_normalize(self):
        cases = [
            ("trims", ["  Caret  "], ["Caret"]),
            ("drops repeats, keeping priority order", ["a", "b", "a"], ["a", "b"]),
            ("passes through", ["a", "b"], ["a", "b"]),
        ]
        for name, given, want in cases:
            with self.subTest(name):
                self.assertEqual(want, protocol.normalize_vocabulary(given, 200))
        for name, given in [("empty entry", ["  "]), ("too long", ["x" * 65]),
                            ("not a string", [3])]:
            with self.subTest(name):
                with self.assertRaises(protocol.OpError):
                    protocol.normalize_vocabulary(given, 200)
        with self.subTest("over the limit"):
            with self.assertRaises(protocol.OpError):
                protocol.normalize_vocabulary([str(i) for i in range(201)], 200)


class ResultShapeTest(unittest.TestCase):
    def test_image_hash_describes_the_delivered_bytes(self):
        with full_backend() as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_IMAGINE,
                start={"type": "start", "protocol": 4, "client_request_id": "img",
                       "input": {"type": "text", "text": "a lighthouse at dusk"}},
                finalize={"type": "finalize", "aspect_ratio": "1:1", "quality": "low"})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        result = probe.terminal["result"]
        import base64
        data = base64.b64decode(result["data_base64"])
        self.assertEqual(len(data), result["byte_length"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), result["sha256"])
        self.assertEqual("image/png", result["mime_type"])
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_text_input_omits_stt_route(self):
        """An honest omission beats an invented value: nothing was
        recognized, so there is no route to report."""
        with full_backend() as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE,
                start={"type": "start", "protocol": 4, "client_request_id": "text",
                       "input": {"type": "text", "text": "ship it on thursday"}},
                finalize={"type": "finalize"})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertNotIn("stt_route", probe.terminal["result"])
        self.assertNotIn("audio", probe.terminal)


class LifecycleEdgeTest(unittest.TestCase):
    def test_cancel_closes_without_a_terminal_event(self):
        with full_backend() as (_, url):
            conn, status, error = ws.dial(
                url.replace("http://", "ws://") + "/dictate",
                headers={"Authorization": "Bearer test-key"},
                timeout=5.0,
            )
            self.assertEqual(101, status)
            self.assertEqual("Switching Protocols", error)
            try:
                conn.set_timeout(5.0)
                conn.send_text(json.dumps(start_frame("cancel")))
                binary, data = conn.read_message()
                self.assertFalse(binary)
                self.assertEqual("ready", json.loads(data)["event"])
                conn.send_text(json.dumps({"type": "cancel"}))
                events = []
                with self.assertRaises(ws.CloseError) as raised:
                    while True:
                        binary, data = conn.read_message()
                        self.assertFalse(binary)
                        events.append(json.loads(data))
                self.assertEqual(1000, raised.exception.code)
                self.assertFalse(
                    any(event.get("event") in ("result", "error") for event in events)
                )
            finally:
                conn.close(1000, "")

    def test_buffered_stt_falls_back_at_finalize(self):
        command = f"command:{sys.executable} -c 'print(\"buffered transcript\")'"
        with backend(stt=command) as (_, url):
            health = checker_for(url)._fetch_health("test-key")
            self.assertFalse(health["capabilities"]["partials"][protocol.ROUTE_DICTATE])
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE,
                start=start_frame("buffered"),
                audio=chunk_audio(tone(1000), 200),
                finalize={"type": "finalize", "audio": totals(chunk_audio(tone(1000), 200))},
            )
        self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertEqual("fallback", probe.terminal["result"]["stt_route"])
        self.assertEqual("buffered transcript", probe.terminal["result"]["raw_transcript"])

    def test_polish_false_returns_the_raw_transcript(self):
        chunks = chunk_audio(tone(1000), 200)
        with full_backend() as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE,
                start=start_frame("unpolished"),
                audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks), "polish": False},
            )
        self.assertEqual("result", probe.terminal_kind, probe.error)
        result = probe.terminal["result"]
        self.assertEqual(result["raw_transcript"], result["text"])
        self.assertFalse(result["polish_applied"])


class CleanupSpecTest(unittest.TestCase):
    """The cross-language anti-drift check: this implementation composes
    the same bytes and the same digest as the spec on disk, which is what
    the Go implementation independently asserts too."""

    def test_composition_reproduces_composed_txt(self):
        spec = cleanup.load_spec(SPEC_DIR)
        composed = (SPEC_DIR / "composed.txt").read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(composed, spec.system_prompt())

    def test_digest_matches_the_manifest(self):
        spec = cleanup.load_spec(SPEC_DIR)
        manifest = json.loads((SPEC_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["digest"], spec.digest)
        self.assertEqual(f"{cleanup.SPEC_NAME} {manifest['digest']}", spec.spec_id)

    def test_wrap_transcript_is_lossless(self):
        for raw in ["  ragged   spacing  ", "</transcript> injected", "", "line\nbreak"]:
            with self.subTest(raw):
                wrapped = cleanup.wrap_transcript(raw)
                inner = wrapped[len(cleanup.TRANSCRIPT_OPEN_TAG) + 1:
                                -len(cleanup.TRANSCRIPT_CLOSE_TAG) - 1]
                self.assertEqual(raw, inner, "the envelope must never alter the transcript")

    def test_missing_spec_is_a_startup_problem_not_a_runtime_one(self):
        with self.assertRaises(cleanup.SpecError):
            cleanup.load_spec(Path(os.devnull).parent / "no-such-caret-spec")


class ProtocolTableTest(unittest.TestCase):
    def test_close_codes(self):
        expected = {
            protocol.ERR_UNAUTHORIZED: 4401,
            protocol.ERR_RATE_LIMITED: 4429,
            protocol.ERR_NOT_SUPPORTED: 4404,
            protocol.ERR_PROTOCOL_ERROR: 4400,
            protocol.ERR_BAD_REQUEST: 4400,
            protocol.ERR_TIMEOUT: 4408,
            protocol.ERR_AUDIO_INCOMPLETE: 4409,
            protocol.ERR_AUDIO_TOO_LONG: 4413,
            protocol.ERR_AUDIO_TOO_SHORT: 4422,
            protocol.ERR_NO_SPEECH_DETECTED: 4422,
            protocol.ERR_TRANSCRIPTION_FAILED: 4503,
            protocol.ERR_GENERATION_FAILED: 4503,
            protocol.ERR_INTERNAL_ERROR: 4500,
        }
        for code, close in expected.items():
            self.assertEqual(close, protocol.close_code_for(code), code)
        self.assertEqual(len(expected), len(protocol.CLOSE_CODES),
                         "§10's table gained or lost a row; update both sides")

    def test_retryable_set(self):
        retryable = {protocol.ERR_RATE_LIMITED, protocol.ERR_TIMEOUT,
                     protocol.ERR_AUDIO_INCOMPLETE, protocol.ERR_TRANSCRIPTION_FAILED,
                     protocol.ERR_GENERATION_FAILED, protocol.ERR_INTERNAL_ERROR}
        for code in protocol.CLOSE_CODES:
            self.assertEqual(code in retryable, protocol.retryable_for(code), code)

    def test_unknown_code_is_loud(self):
        """A code with no close code is a bug in this backend, not a
        protocol event, so it raises here rather than inventing 4500 and
        shipping a wrong close code to a client. Go panics for the same
        reason."""
        with self.assertRaises(KeyError):
            protocol.close_code_for("something_new_in_v5")


class WireTest(unittest.TestCase):
    def test_accept_key(self):
        # RFC 6455 §1.3's worked example.
        self.assertEqual("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
                         ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="))

    def test_wav_header(self):
        pcm = b"\x01\x02" * 100
        wav = lanes.encode_wav(pcm)
        self.assertEqual(44 + len(pcm), len(wav))
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(b"WAVEfmt ", wav[8:16])
        self.assertEqual(b"data", wav[36:40])

    def test_lane_grammar(self):
        cases = [
            ("", "off"), ("none", "off"), ("off", "off"),
            ("loopback", "loopback"),
            ("command:whisper --file {audio}", "command"),
            ("https://stt.example/v1", "http"),
        ]
        for spec, kind in cases:
            with self.subTest(spec):
                self.assertEqual(kind, lanes.parse_lane(spec)[0])
        # A bad spec is refused at startup. LaneError is the other kind:
        # a configured provider failing mid-operation.
        with self.assertRaises(ValueError):
            lanes.parse_lane("ftp://nope")
        with self.assertRaises(ValueError):
            lanes.parse_lane("command:")


if __name__ == "__main__":
    unittest.main()
