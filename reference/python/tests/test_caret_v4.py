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
import time
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


def dial(base_url: str, route: str = protocol.ROUTE_DICTATE, key: str = "test-key",
         timeout: float = 5.0) -> ws.Connection:
    """A raw client connection for tests that need to misbehave on the
    wire in ways the checker's `_operate` will not."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    conn, status, _ = ws.dial(base_url.replace("http://", "ws://") + "/" + route,
                              headers=headers, timeout=timeout)
    if conn is None:
        raise AssertionError(f"upgrade refused with HTTP {status}")
    conn.set_timeout(timeout)
    return conn


def read_until_close(conn: ws.Connection) -> tuple[list, int]:
    """Every event until the server closes, and the close code."""
    events = []
    try:
        while True:
            binary, data = conn.read_message()
            if not binary:
                events.append(json.loads(data))
    except ws.CloseError as closed:
        return events, closed.code


# Fake lanes. The loopback lanes are honest providers; these are
# providers that misbehave on cue, which is what the reliability rules
# are about. A lane object is passed straight to the Server.


class FakeStream:
    def __init__(self, lane: "FakeSTT", options: lanes.STTOptions, on_partial) -> None:
        self.lane = lane
        self.options = options
        self.on_partial = on_partial
        self.buffer = bytearray()
        self.writes = 0

    def write(self, pcm: bytes) -> None:
        self.writes += 1
        if self.lane.die_on_write and self.writes >= self.lane.die_on_write:
            raise lanes.LaneError("the stream died")
        self.buffer.extend(pcm)
        text = lanes.loopback_words(bytes(self.buffer), self.options.vocabulary)
        if text:
            self.on_partial(text)

    def finish(self) -> str:
        time.sleep(self.lane.delay)
        return lanes.loopback_words(bytes(self.buffer), self.options.vocabulary)

    def abort(self) -> None:
        self.lane.aborts += 1


class FakeSTT:
    """Loopback recognition with knobs: `streaming` False raises
    NoStreaming from open, `open_fails` raises an error from it,
    `die_on_write` kills the stream on that write, `delay` slows the
    batch route and the stream flush, `fail_batch` fails the batch
    route."""

    name = "fake"
    uses_vocabulary = True

    def __init__(self, *, streaming: bool = True, open_fails: bool = False,
                 die_on_write: int = 0, delay: float = 0.0, fail_batch: bool = False) -> None:
        self.streaming = streaming
        self.open_fails = open_fails
        self.die_on_write = die_on_write
        self.delay = delay
        self.fail_batch = fail_batch
        self.opens = 0
        self.transcribes = 0
        self.aborts = 0

    def open(self, options: lanes.STTOptions, on_partial):
        self.opens += 1
        if self.open_fails:
            raise lanes.LaneError("no streaming today")
        if not self.streaming:
            raise lanes.NoStreaming()
        return FakeStream(self, options, on_partial)

    def transcribe(self, pcm: bytes, options: lanes.STTOptions) -> str:
        self.transcribes += 1
        time.sleep(self.delay)
        if self.fail_batch:
            raise lanes.LaneError("batch recognition failed")
        return lanes.loopback_words(pcm, options.vocabulary)


class FakeCleanup:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def polish(self, framing: str, transcript: str) -> str:
        self.calls += 1
        if self.fail:
            raise lanes.LaneError("cleanup is down")
        return transcript.upper()


class FakeAgent:
    uses_vocabulary = False

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.prompts: list[str] = []

    def respond(self, prompt: str, visible_text: str, vocabulary: list[str]) -> str:
        self.prompts.append(prompt)
        time.sleep(self.delay)
        return f"answer to: {prompt}"


class FakeImage:
    name = "fake-image"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, aspect_ratio: str, quality: str) -> tuple[str, bytes]:
        self.prompts.append(prompt)
        return "image/png", b"\x89PNG\r\n\x1a\n" + prompt.encode("utf-8")


def audio_operation(label: str, ms: int = 2000, **finalize_extra) -> dict:
    chunks = chunk_audio(tone(ms), 200)
    return dict(start=start_frame(label), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks), **finalize_extra})


def text_operation(label: str, text: str, **finalize_extra) -> dict:
    return dict(start={"type": "start", "protocol": 4, "client_request_id": label,
                       "input": {"type": "text", "text": text}},
                finalize={"type": "finalize", **finalize_extra})


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


class CancelTest(unittest.TestCase):
    def test_cancel_during_finish_discards_the_operation(self):
        """§4 and §8: a cancel that lands while the recognizer is still
        working closes 1000 with no terminal event, and nothing is
        cached, so a replay does the work again."""
        stt = FakeSTT(streaming=False, delay=0.6)
        chunks = chunk_audio(tone(1000), 200)
        with backend(stt=stt) as (server, url):
            conn = dial(url)
            try:
                conn.send_text(json.dumps(start_frame("late-cancel")))
                _, data = conn.read_message()
                self.assertEqual("ready", json.loads(data)["event"])
                for chunk in chunks:
                    conn.send_binary(chunk)
                conn.send_text(json.dumps({"type": "finalize", "audio": totals(chunks)}))
                time.sleep(0.1)  # the lane is now mid-transcribe
                conn.send_text(json.dumps({"type": "cancel"}))
                events, code = read_until_close(conn)
            finally:
                conn.close(1000, "")
            self.assertEqual(1000, code)
            self.assertEqual([], [e for e in events if e["event"] in ("result", "error")])
            self.assertEqual(1, stt.transcribes)
            self.assertIsNone(server.cache_take(server.credential_digest("Bearer test-key"),
                                                protocol.ROUTE_DICTATE, "late-cancel"))

            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE, start=start_frame("late-cancel"), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertEqual(2, stt.transcribes, "the cancelled operation must not be cached")

    def test_peer_closing_before_start_is_quiet(self):
        """A client that connects and leaves is not an incident."""
        with full_backend() as (_, url):
            with self.assertNoLogs(QUIET, level="INFO"):
                conn = dial(url)
                conn.close(1000, "")
                time.sleep(0.2)
            # The server is still fine afterwards.
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE, **audio_operation("after"))
        self.assertEqual("result", probe.terminal_kind, probe.error)


class ReplayCacheTest(unittest.TestCase):
    def test_cache_is_per_credential(self):
        """§8: the same client_request_id under another credential is
        another operation. Each key gets its own vocabulary echoed back,
        which is only possible if neither saw the other's cache entry."""
        with full_backend(api_keys=["key-a", "key-b"]) as (_, url):
            chunks = chunk_audio(tone(2000), 200)
            finalize = {"type": "finalize", "audio": totals(chunks), "polish": False}
            first = checker_for(url, "key-a")._operate(
                protocol.ROUTE_DICTATE, start=start_frame("shared", vocabulary=["Alpha"]),
                audio=chunks, finalize=finalize)
            second = checker_for(url, "key-b")._operate(
                protocol.ROUTE_DICTATE, start=start_frame("shared", vocabulary=["Bravo"]),
                audio=chunks, finalize=finalize)
        for probe in (first, second):
            self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertIn("Alpha", first.terminal["result"]["text"])
        self.assertIn("Bravo", second.terminal["result"]["text"])
        self.assertNotEqual(first.terminal["request_id"], second.terminal["request_id"])

    def test_cached_result_is_purged_on_delivery(self):
        """§10: one replay is served from cache and carries the original
        request_id; the replay after that is a fresh operation."""
        stt = FakeSTT(streaming=False)
        with backend(stt=stt) as (_, url):
            check = checker_for(url)
            operation = audio_operation("replayed")
            original = check._operate(protocol.ROUTE_DICTATE, **operation)
            replay = check._operate(protocol.ROUTE_DICTATE, upload_delay=2.0, **operation)
            again = check._operate(protocol.ROUTE_DICTATE, **operation)
        for probe in (original, replay, again):
            self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertFalse(replay.uploaded, "a cached replay needs no audio")
        self.assertEqual(original.terminal["request_id"], replay.terminal["request_id"])
        self.assertNotEqual(original.terminal["request_id"], again.terminal["request_id"])
        self.assertEqual(2, stt.transcribes)

    def test_failures_are_not_cached(self):
        """§8: a failed operation is exactly the one a client retries."""
        stt = FakeSTT(streaming=False, fail_batch=True)
        with backend(stt=stt) as (_, url):
            check = checker_for(url)
            operation = audio_operation("flaky")
            first = check._operate(protocol.ROUTE_DICTATE, **operation)
            self.assertEqual(protocol.ERR_TRANSCRIPTION_FAILED, first.error_code)
            stt.fail_batch = False
            second = check._operate(protocol.ROUTE_DICTATE, **operation)
        self.assertEqual("result", second.terminal_kind, second.error)
        self.assertTrue(second.uploaded)
        self.assertEqual(2, stt.transcribes)


class ReadyTest(unittest.TestCase):
    def test_stt_is_buffered_when_the_stream_will_not_open(self):
        """§4: ready.stt describes what is true when ready is sent. A
        streaming lane whose open fails is buffered for this operation,
        and the operation still completes from the buffered audio."""
        stt = FakeSTT(open_fails=True)
        with backend(stt=stt) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE, **audio_operation("open"))
        self.assertEqual("buffered", probe.ready["stt"])
        self.assertEqual([], probe.partials)
        self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertEqual("fallback", probe.terminal["result"]["stt_route"])

    def test_stt_is_streaming_when_the_stream_opens(self):
        stt = FakeSTT()
        with backend(stt=stt) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE, **audio_operation("live"))
        self.assertEqual("streaming", probe.ready["stt"])
        self.assertEqual(1, stt.opens)
        self.assertEqual("stream", probe.terminal["result"]["stt_route"])

    def test_stt_is_null_for_text_input(self):
        with full_backend() as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE,
                                              **text_operation("typed", "hello there"))
        self.assertIn("stt", probe.ready)
        self.assertIsNone(probe.ready["stt"])


class AuthTest(unittest.TestCase):
    def test_missing_authorization_is_unauthorized(self):
        with full_backend() as (_, url):
            probe = checker_for(url, key="")._operate(protocol.ROUTE_DICTATE,
                                                      start=start_frame("noauth"))
        self.assertEqual(protocol.ERR_UNAUTHORIZED, probe.error_code, probe.error)
        self.assertEqual(4401, probe.close_code)
        self.assertFalse(probe.terminal["retryable"])

    def test_no_configured_keys_fails_closed(self):
        with backend(api_keys=[], stt="loopback") as (_, url):
            probe = checker_for(url, key="anything")._operate(protocol.ROUTE_DICTATE,
                                                              start=start_frame("nokeys"))
        self.assertEqual(protocol.ERR_UNAUTHORIZED, probe.error_code, probe.error)
        self.assertEqual(4401, probe.close_code)


class DeadlineTest(unittest.TestCase):
    """§4's two clocks, shortened to milliseconds so the suite stays
    fast; the defaults are the protocol's 10 s and 60 s."""

    def test_no_start_frame_times_out(self):
        with full_backend(start_deadline=0.2) as (_, url):
            began = time.monotonic()
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE)
            elapsed = time.monotonic() - began
        self.assertEqual(protocol.ERR_TIMEOUT, probe.error_code, probe.error)
        self.assertEqual(4408, probe.close_code)
        self.assertTrue(probe.terminal["retryable"])
        self.assertLess(elapsed, 5.0)

    def test_frame_gap_times_out(self):
        with full_backend(frame_gap=0.2) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE, start=start_frame("gap"),
                                              audio=[], finalize=None)
        self.assertIsNotNone(probe.ready)
        self.assertEqual(protocol.ERR_TIMEOUT, probe.error_code, probe.error)
        self.assertEqual(4408, probe.close_code)


class ProgressTest(unittest.TestCase):
    def test_slow_lane_sends_progress_and_terminal_is_last(self):
        """§5: a lane that takes a while keeps the socket honest with
        progress events, and the terminal event still comes last with
        nothing after it."""
        stt = FakeSTT(streaming=False, delay=0.3)
        with backend(stt=stt, progress_interval=0.05) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE, **audio_operation("slow"))
        self.assertEqual("result", probe.terminal_kind, probe.error)
        progress = [e for e in probe.events if e["event"] == "progress"]
        self.assertGreaterEqual(len(progress), 2)
        self.assertEqual("transcribing", progress[0]["stage"])
        self.assertEqual("result", probe.events[-1]["event"])
        self.assertLessEqual(progress[0]["elapsed_ms"], progress[-1]["elapsed_ms"])


class LaneFailureTest(unittest.TestCase):
    def test_cleanup_failure_returns_the_raw_transcript(self):
        cleanup_lane = FakeCleanup(fail=True)
        with backend(stt=FakeSTT(), cleanup=cleanup_lane) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_DICTATE,
                                              **audio_operation("unpolished", polish=True))
        self.assertEqual("result", probe.terminal_kind, probe.error)
        result = probe.terminal["result"]
        self.assertEqual(1, cleanup_lane.calls)
        self.assertFalse(result["polish_applied"])
        self.assertEqual(result["raw_transcript"], result["text"])

    def test_stream_death_falls_back_with_the_whole_recording(self):
        """§8: the streaming recognizer dying is not the operation dying.
        Audio after the failure is still buffered, and the batch route
        sees all of it."""
        stt = FakeSTT(die_on_write=3)
        chunks = chunk_audio(tone(2000), 200)
        with backend(stt=stt) as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE, start=start_frame("dying"), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        self.assertEqual("fallback", probe.terminal["result"]["stt_route"])
        self.assertEqual(lanes.loopback_words(tone(2000), []),
                         probe.terminal["result"]["raw_transcript"])
        self.assertEqual(1, stt.aborts)
        self.assertEqual(totals(chunks)["frames"], probe.terminal["audio"]["frames"])


class ProtocolErrorTest(unittest.TestCase):
    def test_protocol_errors(self):
        """§9: every way of speaking out of turn is protocol_error, close
        4400, not retryable."""
        cases = [
            ("binary before start", dict(binary_first=tone(200))),
            ("cancel before start", dict(raw_first='{"type":"cancel"}')),
            ("unknown control type", dict(raw_first='{"type":"pause"}')),
            ("not an object", dict(raw_first='[1,2,3]')),
        ]
        with full_backend() as (_, url):
            check = checker_for(url)
            for name, operation in cases:
                with self.subTest(name):
                    probe = check._operate(protocol.ROUTE_DICTATE, **operation)
                    self.assertEqual(protocol.ERR_PROTOCOL_ERROR, probe.error_code,
                                     f"{probe.terminal_kind!r} {probe.error}")
                    self.assertEqual(4400, probe.close_code)
                    self.assertFalse(probe.terminal["retryable"])
            with self.subTest("start twice"):
                conn = dial(url)
                try:
                    conn.send_text(json.dumps(start_frame("twice")))
                    _, data = conn.read_message()
                    self.assertEqual("ready", json.loads(data)["event"])
                    conn.send_text(json.dumps(start_frame("twice")))
                    events, code = read_until_close(conn)
                finally:
                    conn.close(1000, "")
                self.assertEqual(protocol.ERR_PROTOCOL_ERROR, events[-1].get("code"), events)
                self.assertEqual(4400, code)

    def test_finalize_audio_presence(self):
        """§5: finalize.audio is required for audio input and forbidden
        for text input; either mistake is bad_request."""
        chunks = chunk_audio(tone(1000), 200)
        with full_backend() as (_, url):
            check = checker_for(url)
            with self.subTest("audio input without totals"):
                probe = check._operate(protocol.ROUTE_DICTATE, start=start_frame("no-totals"),
                                       audio=chunks, finalize={"type": "finalize"})
                self.assertEqual(protocol.ERR_BAD_REQUEST, probe.error_code, probe.error)
                self.assertEqual(4400, probe.close_code)
            with self.subTest("text input with totals"):
                probe = check._operate(protocol.ROUTE_DICTATE,
                                       **text_operation("with-totals", "typed",
                                                        audio=totals(chunks)))
                self.assertEqual(protocol.ERR_BAD_REQUEST, probe.error_code, probe.error)
                self.assertEqual(4400, probe.close_code)


class RouteInputTest(unittest.TestCase):
    def test_audio_input_carries_the_transcript(self):
        agent, image = FakeAgent(), FakeImage()
        with backend(stt=FakeSTT(), agent=agent, image=image) as (_, url):
            check = checker_for(url)
            ask = check._operate(protocol.ROUTE_ASK, **audio_operation("ask", visible_text=""))
            imagine = check._operate(protocol.ROUTE_IMAGINE,
                                     **audio_operation("imagine", aspect_ratio="1:1",
                                                       quality="low"))
        for probe in (ask, imagine):
            self.assertEqual("result", probe.terminal_kind, probe.error)
            self.assertEqual(lanes.loopback_words(tone(2000), []),
                             probe.terminal["result"]["transcript"])
        self.assertEqual([ask.terminal["result"]["transcript"]], agent.prompts)
        self.assertEqual([imagine.terminal["result"]["transcript"]], image.prompts)

    def test_text_input_omits_the_transcript(self):
        """§6 marks transcript "audio input only"; for typed input it is
        left out rather than echoed."""
        with backend(stt=FakeSTT(), agent=FakeAgent(), image=FakeImage()) as (_, url):
            check = checker_for(url)
            ask = check._operate(protocol.ROUTE_ASK,
                                 **text_operation("ask-text", "what time is it", visible_text=""))
            imagine = check._operate(protocol.ROUTE_IMAGINE,
                                     **text_operation("imagine-text", "a fox",
                                                      aspect_ratio="1:1", quality="low"))
        for probe in (ask, imagine):
            self.assertEqual("result", probe.terminal_kind, probe.error)
            self.assertNotIn("transcript", probe.terminal["result"])
            self.assertNotIn("audio", probe.terminal)

    def test_audio_without_a_recognizer_is_transcription_failed(self):
        """§9: /ask is served, so it is not not_supported; the speech
        cannot be transcribed, so it is transcription_failed, and the
        client may retry once a recognizer is back."""
        with backend(stt="", agent=FakeAgent()) as (_, url):
            probe = checker_for(url)._operate(protocol.ROUTE_ASK,
                                              **audio_operation("deaf", visible_text=""))
        self.assertEqual(protocol.ERR_TRANSCRIPTION_FAILED, probe.error_code, probe.error)
        self.assertEqual(4503, probe.close_code)
        self.assertTrue(probe.terminal["retryable"])

    def test_result_echoes_the_audio_consumed(self):
        chunks = chunk_audio(tone(1800), 150)
        with backend(stt=FakeSTT()) as (_, url):
            probe = checker_for(url)._operate(
                protocol.ROUTE_DICTATE, start=start_frame("echo"), audio=chunks,
                finalize={"type": "finalize", "audio": totals(chunks)})
        self.assertEqual("result", probe.terminal_kind, probe.error)
        sent = totals(chunks)
        echoed = probe.terminal["audio"]
        self.assertEqual(sent["frames"], echoed["frames"])
        self.assertEqual(sent["bytes"], echoed["bytes"])
        self.assertEqual(sent["duration_ms"], echoed["duration_ms"])


if __name__ == "__main__":
    unittest.main()
