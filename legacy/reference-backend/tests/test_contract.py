"""Contract tests: every rule in the spec that a client can depend on.

Each test names the guarantee it protects rather than the function it
calls, because the guarantee is what must survive a rewrite of this
backend — or a reimplementation of it in another language.
"""

from __future__ import annotations

import base64
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import adapters  # noqa: E402
from caret_backend.server import CHUNK_MAX_BYTES  # noqa: E402
from helpers import (  # noqa: E402
    API_KEY,
    BlockingAgent,
    TestServer,
    duration_ms,
    pcm,
    sha256_hex,
)


class ServerCase(unittest.TestCase):
    inline_jobs = True
    options: dict = {}

    def setUp(self) -> None:
        self.server = TestServer(inline_jobs=self.inline_jobs, **self.options)
        self.addCleanup(self.server.close)

    def assertError(self, payload, code):  # noqa: N802 - unittest convention
        self.assertEqual((payload.get("error") or {}).get("code"), code, payload)
        self.assertTrue(payload.get("request_id"), "every error carries a request_id")


def session_input(session_id: str, *, count: int = 2, ms: int = 3000, polish: bool = True) -> dict:
    return {
        "type": "session",
        "session_id": session_id,
        "client_chunk_count": count,
        "client_total_duration_ms": ms,
        "polish": polish,
    }


class HealthTests(ServerCase):
    def test_health_is_anonymous_and_declares_the_contract(self):
        status, body, _ = self.server.request("GET", "/v2/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], "caret/v2")
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["time"].endswith("Z"))
        self.assertEqual(body["auth"], {"presented": False, "valid": None})

    def test_health_reports_input_modes_per_operation(self):
        _, body, _ = self.server.request("GET", "/v2/health", key=None)
        caps = body["capabilities"]
        self.assertTrue(caps["dictate"] and caps["ask"] and caps["imagine"])
        self.assertEqual(caps["input_modes"]["dictate"], ["text", "session"])
        self.assertEqual(caps["input_modes"]["ask"], ["text", "session"])
        self.assertEqual(caps["input_modes"]["imagine"], ["text", "session"])

    def test_health_validates_a_presented_key_without_requiring_one(self):
        _, ok, _ = self.server.request("GET", "/v2/health")
        self.assertEqual(ok["auth"], {"presented": True, "valid": True})
        _, bad, _ = self.server.request("GET", "/v2/health", key="nope")
        self.assertEqual(bad["auth"], {"presented": True, "valid": False})


class DictateIsMandatoryTests(unittest.TestCase):
    """Dictate is not one capability among three.

    A valid Caret backend takes speech. Ask and Imagine are optional
    extensions on top of that, and a backend advertising Ask with Dictate
    off is not a lightweight deployment — it is a broken one, and it has to
    say so rather than reporting `ok`."""

    def test_a_backend_with_stt_and_keys_is_ready(self):
        server = TestServer()
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["readiness"], {"ready": True, "blockers": []})
        self.assertTrue(health["capabilities"]["dictate"])

    def test_no_stt_is_not_ready_and_never_reports_ok(self):
        server = TestServer(transcriber=adapters.NullTranscriber())
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        self.assertEqual(health["status"], "not_ready")
        self.assertFalse(health["readiness"]["ready"])
        self.assertIn(
            "no_stt_adapter", [b["code"] for b in health["readiness"]["blockers"]]
        )
        self.assertEqual(health["routes"]["dictate"], {"route": "off"})

    def test_ask_on_with_dictate_off_is_still_not_ready(self):
        # The exact shape the public docs must never present as valid.
        server = TestServer(
            agent=adapters.EchoAgent(),
            transcriber=adapters.NullTranscriber(),
            image_generator=None,
        )
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        self.assertTrue(health["capabilities"]["ask"])
        self.assertFalse(health["capabilities"]["dictate"])
        self.assertEqual(health["status"], "not_ready")

    def test_missing_keys_is_degraded_and_missing_stt_outranks_it(self):
        server = TestServer(api_keys=(), transcriber=adapters.NullTranscriber())
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        self.assertEqual(health["status"], "not_ready")
        self.assertEqual(
            sorted(b["code"] for b in health["readiness"]["blockers"]),
            ["no_api_keys", "no_stt_adapter"],
        )

    def test_dictate_is_404_without_a_transcriber(self):
        server = TestServer(transcriber=adapters.NullTranscriber())
        self.addCleanup(server.close)
        status, body, _ = server.request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "d1", "input": {"type": "text", "text": "hi"}},
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found")


class AskIsOptionalTests(unittest.TestCase):
    """No agent configured is a valid dictate-only backend — provided it
    reports that, and does not answer asks with a stand-in."""

    def server(self, **overrides):
        server = TestServer(agent=adapters.NullAgent(), **overrides)
        self.addCleanup(server.close)
        return server

    def test_health_reports_ask_off_but_stays_ready(self):
        _, health, _ = self.server().request("GET", "/v2/health", key=None)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["readiness"]["ready"])
        self.assertFalse(health["capabilities"]["ask"])
        self.assertEqual(health["capabilities"]["input_modes"]["ask"], [])
        self.assertEqual(health["routes"]["ask"], {"route": "off"})
        self.assertEqual(health["routes"]["dictate"]["cleanup"], {"route": "off"})
        self.assertEqual(health["adapters"]["agent"], "none")

    def test_ask_is_404_not_an_echo(self):
        status, body, _ = self.server().request(
            "POST",
            "/v2/ask",
            body={"client_request_id": "c1", "input": {"type": "text", "text": "hi"}},
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_dictate_still_works_and_returns_the_raw_transcript(self):
        server = self.server()
        session_id = server.open_session()
        server.upload(session_id, 0, pcm(1500))
        status, body, _ = server.request(
            "POST",
            "/v2/dictate",
            body={
                "client_request_id": "d1",
                "input": session_input(session_id, count=1, ms=1500, polish=True),
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "complete")
        # `polish: true` was asked for and cannot be served without an agent;
        # the transcript comes back raw rather than the request failing.
        self.assertIn("transcribed", body["text"])

    def test_text_dictate_returns_the_text_unchanged_without_an_agent(self):
        status, body, _ = self.server().request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "d2", "input": {"type": "text", "text": "um hi"}},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "um hi")
        self.assertEqual(body["input_type"], "text")


class CapabilityHonestyTests(unittest.TestCase):
    def test_a_backend_without_a_transcriber_says_so_and_refuses_sessions(self):
        server = TestServer(transcriber=adapters.NullTranscriber(), image_generator=None)
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        caps = health["capabilities"]
        self.assertFalse(caps["dictate"])
        self.assertFalse(caps["imagine"])
        self.assertEqual(caps["input_modes"]["dictate"], [])
        self.assertEqual(caps["input_modes"]["ask"], ["text"])
        self.assertEqual(caps["input_modes"]["imagine"], [])

        status, body, _ = server.request(
            "POST",
            "/v2/ask",
            body={
                "client_request_id": "c1",
                "input": session_input("sess_x", count=1, ms=1000),
            },
        )
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "unsupported_input_type")

    def test_imagine_is_404_when_unconfigured_not_a_fake_success(self):
        server = TestServer(image_generator=None)
        self.addCleanup(server.close)
        status, body, _ = server.request(
            "POST",
            "/v2/imagine",
            body={"client_request_id": "c1", "input": {"type": "text", "text": "a cat"}},
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found")


class AuthTests(ServerCase):
    def test_every_operation_but_health_requires_a_bearer_key(self):
        for method, path in (
            ("POST", "/v2/dictate"),
            ("POST", "/v2/ask"),
            ("POST", "/v2/imagine"),
            ("POST", "/v2/sessions"),
        ):
            status, body, _ = self.server.request(method, path, body={}, key=None)
            self.assertEqual(status, 401, path)
            self.assertError(body, "unauthorized")

    def test_a_wrong_key_is_rejected(self):
        status, body, _ = self.server.request("POST", "/v2/ask", body={}, key="wrong")
        self.assertEqual(status, 401)
        self.assertError(body, "unauthorized")

    def test_a_backend_with_no_keys_configured_fails_closed(self):
        server = TestServer(api_keys=())
        self.addCleanup(server.close)
        status, _, _ = server.request("POST", "/v2/ask", body={}, key="anything")
        self.assertEqual(status, 401)
        _, health, _ = server.request("GET", "/v2/health", key=None)
        self.assertEqual(health["status"], "degraded")

    def test_an_unknown_route_is_a_contract_shaped_404(self):
        status, body, _ = self.server.request("GET", "/v2/nope")
        self.assertEqual(status, 404)
        self.assertError(body, "not_found")


class InputModelTests(ServerCase):
    def ask(self, **payload):
        return self.server.request("POST", "/v2/ask", body=payload)

    def test_typed_input_object_is_accepted(self):
        status, body, _ = self.ask(
            client_request_id="c1", input={"type": "text", "text": "say hi"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "[draft] say hi")
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["input_type"], "text")
        self.assertTrue(body["request_id"])

    def test_the_v1_instruction_alias_is_gone(self):
        # `instruction` is not part of caret/v2. It is an unknown field, so
        # the request simply has no input — and says so.
        status, body, _ = self.ask(client_request_id="c1", instruction="say hi")
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_the_v1_audio_discriminator_is_gone(self):
        status, body, _ = self.ask(
            client_request_id="c1",
            input={
                "type": "audio",
                "session_id": "sess_x",
                "client_chunk_count": 1,
                "client_total_duration_ms": 1000,
            },
        )
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_supplying_no_input_is_rejected(self):
        status, body, _ = self.ask(client_request_id="c1")
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_an_unknown_input_type_is_rejected(self):
        status, body, _ = self.ask(client_request_id="c1", input={"type": "video"})
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_empty_and_oversized_text_are_rejected(self):
        for text in ("", "x" * 4001):
            status, body, _ = self.ask(
                client_request_id="c1", input={"type": "text", "text": text}
            )
            self.assertEqual(status, 422)
            self.assertError(body, "input_invalid")

    def test_session_input_requires_the_completeness_fields(self):
        for missing in ("client_chunk_count", "client_total_duration_ms"):
            value = session_input("sess_x")
            del value[missing]
            status, body, _ = self.ask(client_request_id="c1", input=value)
            self.assertEqual(status, 422, missing)
            self.assertError(body, "input_invalid")

    def test_client_request_id_is_required(self):
        status, body, _ = self.ask(input={"type": "text", "text": "hi"})
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")

    def test_a_repeated_client_request_id_replays_the_first_answer(self):
        _, first, _ = self.ask(
            client_request_id="same", input={"type": "text", "text": "one"}
        )
        _, second, _ = self.ask(
            client_request_id="same", input={"type": "text", "text": "two"}
        )
        self.assertEqual(second["text"], first["text"])

    def test_a_malformed_body_is_a_400_not_a_traceback(self):
        status, body, _ = self.server.request(
            "POST", "/v2/ask", body=b"{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")


class SessionTests(ServerCase):
    def test_a_session_declares_the_chunk_limits_the_client_must_honour(self):
        status, body, _ = self.server.request(
            "POST",
            "/v2/sessions",
            body={
                "client_request_id": "c1",
                "codec": "pcm16",
                "sample_rate_hz": 16000,
                "channels": 1,
            },
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["session_id"].startswith("sess_"))
        self.assertEqual(body["chunk_max_bytes"], CHUNK_MAX_BYTES)
        self.assertEqual(body["chunk_target_duration_ms"], 3000)
        self.assertTrue(body["expires_at"].endswith("Z"))

    def test_only_the_contract_audio_format_is_accepted(self):
        base = {"client_request_id": "c1", "codec": "pcm16", "sample_rate_hz": 16000, "channels": 1}
        status, body, _ = self.server.request(
            "POST", "/v2/sessions", body={**base, "codec": "opus"}
        )
        self.assertEqual(status, 415)
        self.assertError(body, "unsupported_audio_codec")

        for field, value in (("sample_rate_hz", 44100), ("channels", 2)):
            status, body, _ = self.server.request(
                "POST", "/v2/sessions", body={**base, field: value}
            )
            self.assertEqual(status, 400, field)
            self.assertError(body, "bad_request")

    def test_intent_is_advisory_and_optional(self):
        base = {"client_request_id": "c-intent", "codec": "pcm16", "sample_rate_hz": 16000, "channels": 1}
        status, body, _ = self.server.request("POST", "/v2/sessions", body=base)
        self.assertEqual(status, 200, body)
        status, body, _ = self.server.request(
            "POST", "/v2/sessions", body={**base, "client_request_id": "c-intent2", "intent": "nope"}
        )
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")

    def test_reopening_with_the_same_client_request_id_replays_the_session(self):
        first = self.server.open_session(client_request_id="dup")
        second = self.server.open_session(client_request_id="dup")
        self.assertEqual(first, second)

    def test_an_unknown_session_is_404(self):
        status, body, _ = self.server.request(
            "POST",
            "/v2/dictate",
            body={
                "client_request_id": "c1",
                "input": session_input("sess_missing", count=1, ms=1000),
            },
        )
        self.assertEqual(status, 404)
        self.assertError(body, "unknown_session")

    def test_an_expired_session_is_410(self):
        session_id = self.server.open_session()
        self.server.backend.store.update_session(session_id, expires_at=time.time() - 1)
        status, body, _ = self.server.upload(session_id, 0, pcm(500))
        self.assertEqual(status, 410)
        self.assertError(body, "session_expired")


class ChunkTests(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_id = self.server.open_session()

    def test_a_chunk_is_accepted_and_acknowledged(self):
        status, body, _ = self.server.upload(self.session_id, 0, pcm(3000))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["seq"], 0)
        self.assertTrue(body["accepted"])
        self.assertFalse(body["duplicate"])

    def test_re_uploading_identical_bytes_is_idempotent(self):
        chunk = pcm(3000)
        self.server.upload(self.session_id, 0, chunk)
        status, body, _ = self.server.upload(self.session_id, 0, chunk)
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])

    def test_a_body_that_does_not_match_its_digest_is_retryable(self):
        status, body, _ = self.server.upload(
            self.session_id, 0, pcm(1000), sha=sha256_hex(b"other")
        )
        self.assertEqual(status, 409)
        self.assertError(body, "chunk_checksum_mismatch")
        self.assertTrue(body["error"]["retryable"], "a transport corruption is worth retrying")

    def test_two_different_chunks_for_one_seq_is_a_client_bug(self):
        self.server.upload(self.session_id, 0, pcm(1000))
        status, body, _ = self.server.upload(self.session_id, 0, pcm(2000))
        self.assertEqual(status, 409)
        self.assertError(body, "chunk_seq_conflict")
        self.assertFalse(body["error"]["retryable"], "retrying cannot fix a client bug")

    def test_a_missing_digest_header_is_rejected(self):
        status, body, _ = self.server.request(
            "PUT",
            f"/v2/sessions/{self.session_id}/chunks/0",
            body=pcm(500),
        )
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")

    def test_an_oversized_chunk_is_413(self):
        oversized = b"\x00" * (CHUNK_MAX_BYTES + 2)
        status, body, _ = self.server.upload(self.session_id, 0, oversized)
        self.assertEqual(status, 413)
        self.assertError(body, "chunk_too_large")


class SpokenFlowTests(ServerCase):
    def prepare(self, *, intent: str = "dictate", chunks: int = 2) -> str:
        session_id = self.server.open_session(intent=intent, client_request_id=f"c-{intent}")
        for seq in range(chunks):
            status, body, _ = self.server.upload(session_id, seq, pcm(1500))
            self.assertEqual(status, 200, body)
        return session_id

    def dictate(self, session_id: str, *, count: int = 2, ms: int = 3000, polish: bool = True, crid: str = "d1"):
        return self.server.request(
            "POST",
            "/v2/dictate",
            body={
                "client_request_id": crid,
                "input": session_input(session_id, count=count, ms=ms, polish=polish),
            },
        )

    def test_dictate_returns_the_transcript(self):
        session_id = self.prepare()
        status, body, _ = self.dictate(session_id, polish=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["input_type"], "session")
        self.assertIn("transcribed", body["text"])
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["missing_chunks"], [])
        self.assertEqual(body["duration_ms"], 3000)

    def test_a_gap_in_the_upload_is_a_200_not_an_error(self):
        session_id = self.server.open_session()
        self.server.upload(session_id, 0, pcm(1500))
        status, body, _ = self.dictate(session_id, count=3, ms=4500)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "missing_chunks")
        self.assertEqual(body["missing_chunks"], [1, 2])
        self.assertIsNone(body["text"])

    def test_ask_accepts_a_session_instead_of_typed_text(self):
        session_id = self.prepare(intent="ask")
        status, body, _ = self.server.request(
            "POST",
            "/v2/ask",
            body={"client_request_id": "ask-1", "input": session_input(session_id)},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["input_type"], "session")
        self.assertEqual(body["session_id"], session_id)
        self.assertTrue(body["transcript"])
        self.assertTrue(body["text"].startswith("[draft] "))
        self.assertEqual(body["duration_ms"], 3000)

    def test_polish_runs_on_the_transcript_before_it_is_returned(self):
        unpolished = self.dictate(self.prepare(), polish=False)[1]["text"]
        polished = self.dictate(self.prepare(intent="ask"), polish=True, crid="d2")[1]["text"]
        # EchoAgent.polish capitalises; the point is that the flag is honoured.
        self.assertNotEqual(polished, unpolished)
        self.assertEqual(polished, unpolished.capitalize())

    def test_text_dictate_runs_the_cleanup_pass(self):
        status, body, _ = self.server.request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "t1", "input": {"type": "text", "text": "um hi there"}},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["input_type"], "text")
        # EchoAgent.polish capitalises the raw text.
        self.assertEqual(body["text"], "Um hi there")

    def test_text_dictate_is_idempotent_by_client_request_id(self):
        body = {"client_request_id": "t-same", "input": {"type": "text", "text": "um one"}}
        first = self.server.request("POST", "/v2/dictate", body=body)[1]
        second = self.server.request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "t-same", "input": {"type": "text", "text": "um two"}},
        )[1]
        self.assertEqual(first["text"], second["text"])

    def test_imagine_accepts_the_same_session_input(self):
        session_id = self.prepare(intent="imagine")
        status, body, _ = self.server.request(
            "POST",
            "/v2/imagine",
            body={"client_request_id": "img-1", "input": session_input(session_id)},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["input_type"], "session")
        self.assertEqual(body["media"]["mime_type"], "image/png")
        self.assertTrue(body["transcript"])

    def test_a_session_carries_exactly_one_terminal_result(self):
        session_id = self.prepare()
        self.dictate(session_id)
        status, body, _ = self.server.request(
            "POST",
            "/v2/ask",
            body={"client_request_id": "steal", "input": session_input(session_id)},
        )
        self.assertEqual(status, 409, body)
        self.assertError(body, "session_conflict")

    def test_a_chunk_upload_after_consumption_is_a_conflict(self):
        session_id = self.prepare()
        self.dictate(session_id)
        status, body, _ = self.server.upload(session_id, 2, pcm(500))
        self.assertEqual(status, 409, body)
        self.assertError(body, "session_conflict")

    def test_audio_is_deleted_once_the_session_reaches_a_result(self):
        session_id = self.prepare()
        chunks = self.server.backend.store.sessions / session_id / "chunks"
        self.assertTrue(chunks.exists())
        self.dictate(session_id)
        self.assertFalse(chunks.exists(), "raw audio must not outlive its result")

    def test_re_posting_a_finished_request_replays_the_cached_result(self):
        session_id = self.prepare()
        first = self.dictate(session_id)[1]
        second = self.dictate(session_id)[1]
        self.assertEqual(first, second)


class AckTests(ServerCase):
    def test_ack_deletes_the_cached_result_and_is_idempotent(self):
        session_id = self.server.open_session()
        self.server.upload(session_id, 0, pcm(1500))
        self.server.request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "a1", "input": session_input(session_id, count=1, ms=1500)},
        )
        status, body, _ = self.server.request("POST", f"/v2/sessions/{session_id}/ack")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["acknowledged"])
        self.assertFalse(body["already_acknowledged"])
        status, replay, _ = self.server.request("POST", f"/v2/sessions/{session_id}/ack")
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["already_acknowledged"])

    def test_acking_an_unconsumed_session_abandons_it(self):
        session_id = self.server.open_session()
        self.server.upload(session_id, 0, pcm(1500))
        status, body, _ = self.server.request("POST", f"/v2/sessions/{session_id}/ack")
        self.assertEqual(status, 200, body)
        status, body, _ = self.server.request(
            "POST",
            "/v2/dictate",
            body={"client_request_id": "a2", "input": session_input(session_id, count=1, ms=1500)},
        )
        self.assertEqual(status, 409, body)
        self.assertError(body, "session_conflict")

    def test_acking_an_unknown_session_is_404(self):
        status, body, _ = self.server.request("POST", "/v2/sessions/sess_missing/ack")
        self.assertEqual(status, 404)
        self.assertError(body, "unknown_session")


class AsyncTests(unittest.TestCase):
    """The 202 convention, observed against work that is genuinely slow."""

    def setUp(self) -> None:
        self.agent = BlockingAgent()
        self.server = TestServer(inline_jobs=False, agent=self.agent)
        self.addCleanup(self.server.close)
        self.addCleanup(self.agent.released.set)

    def test_in_progress_is_202_with_retry_after_and_a_stable_request_id(self):
        session_id = self.server.open_session(intent="ask")
        self.server.upload(session_id, 0, pcm(1000))
        body = {
            "client_request_id": "slow-audio",
            "input": session_input(session_id, count=1, ms=1000),
        }
        status, first, headers = self.server.request("POST", "/v2/ask", body=body)
        self.assertEqual(status, 202, first)
        self.assertEqual(first["status"], "in_progress")
        self.assertTrue(int(headers["Retry-After"]) >= 1)
        self.assertEqual(headers["Retry-After"], str(first["retry_after_seconds"]))

        # Polling is re-POSTing the identical body — not a new request.
        status, again, _ = self.server.request("POST", "/v2/ask", body=body)
        self.assertEqual(status, 202)
        self.assertEqual(again["request_id"], first["request_id"])

        self.agent.released.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            status, final, _ = self.server.request("POST", "/v2/ask", body=body)
            if status == 200:
                break
            time.sleep(0.05)
        self.assertEqual(status, 200, final)
        self.assertEqual(final["request_id"], first["request_id"], "stable across polls")
        self.assertTrue(final["text"].startswith("[slow] "))

    def test_concurrent_polls_start_the_work_exactly_once(self):
        session_id = self.server.open_session(intent="ask")
        self.server.upload(session_id, 0, pcm(1000))
        body = {
            "client_request_id": "race",
            "input": session_input(session_id, count=1, ms=1000),
        }
        seen: list[str] = []
        lock = threading.Lock()

        def poll():
            _, payload, _ = self.server.request("POST", "/v2/ask", body=body)
            with lock:
                seen.append(payload["request_id"])

        threads = [threading.Thread(target=poll) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.agent.released.set()
        self.assertEqual(len(set(seen)), 1, f"one job, one request_id: {seen}")


class MediaTests(ServerCase):
    def test_imagine_media_declares_a_digest_that_matches_the_bytes(self):
        status, body, _ = self.server.request(
            "POST",
            "/v2/imagine",
            body={"client_request_id": "i1", "input": {"type": "text", "text": "a lighthouse"}},
        )
        self.assertEqual(status, 200, body)
        media = body["media"]
        raw = base64.b64decode(media["inline_base64"])
        self.assertEqual(len(raw), media["byte_length"])
        self.assertEqual(sha256_hex(raw), media["sha256"])
        self.assertEqual(media["kind"], "image")

    def test_unknown_aspect_ratio_and_quality_are_rejected(self):
        for field, code in (("aspect_ratio", "unsupported_aspect_ratio"), ("quality", "unsupported_quality")):
            status, body, _ = self.server.request(
                "POST",
                "/v2/imagine",
                body={
                    "client_request_id": f"i-{field}",
                    "input": {"type": "text", "text": "x"},
                    field: "enormous",
                },
            )
            self.assertEqual(status, 422, field)
            self.assertError(body, code)

    def test_the_advertised_ratios_and_qualities_are_accepted(self):
        status, body, _ = self.server.request(
            "POST",
            "/v2/imagine",
            body={
                "client_request_id": "i-ok",
                "input": {"type": "text", "text": "x"},
                "aspect_ratio": "3:2",
                "quality": "low",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["aspect_ratio"], "3:2")
        self.assertEqual(body["quality"], "low")


class RateLimitTests(unittest.TestCase):
    def test_a_flood_from_one_key_is_throttled_not_served(self):
        server = TestServer(rate_limit_per_minute=3)
        self.addCleanup(server.close)
        codes = [
            server.request(
                "POST",
                "/v2/ask",
                body={"client_request_id": f"r{i}", "input": {"type": "text", "text": "hi"}},
            )[0]
            for i in range(5)
        ]
        self.assertEqual(codes[:3], [200, 200, 200], codes)
        self.assertEqual(codes[3:], [429, 429], codes)
        _, body, _ = server.request(
            "POST", "/v2/ask", body={"client_request_id": "r9", "input": {"type": "text", "text": "hi"}}
        )
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertTrue(body["error"]["retryable"])


class AdapterTests(unittest.TestCase):
    """The extension point: an agent is a command line."""

    def test_a_command_agent_runs_a_real_subprocess(self):
        agent = adapters.CommandAgent(
            name="test",
            template=f'{sys.executable} -c "import sys;print(sys.argv[1][:40])" "{{prompt}}"',
        )
        self.assertEqual(agent.complete("hello from a subprocess"), "hello from a subprocess")

    def test_an_agent_with_no_prompt_placeholder_receives_stdin(self):
        agent = adapters.CommandAgent(
            name="test",
            template=f'{sys.executable} -c "import sys;print(sys.stdin.read().strip().upper())"',
        )
        self.assertEqual(agent.complete("quiet please"), "QUIET PLEASE")

    def test_a_missing_command_is_reported_not_swallowed(self):
        agent = adapters.CommandAgent(name="ghost", template="caret-no-such-binary {prompt}")
        with self.assertRaises(Exception) as ctx:
            agent.complete("hi")
        self.assertIn("not found", str(ctx.exception))

    def test_a_non_zero_exit_carries_the_stderr_tail(self):
        agent = adapters.CommandAgent(
            name="failing",
            template=f'{sys.executable} -c "import sys;sys.stderr.write(\'boom\\n\');sys.exit(3)"',
        )
        with self.assertRaises(Exception) as ctx:
            agent.complete("hi")
        self.assertIn("boom", str(ctx.exception))

    def test_an_empty_answer_is_an_error_not_an_empty_draft(self):
        agent = adapters.CommandAgent(name="silent", template=f'{sys.executable} -c "pass"')
        with self.assertRaises(Exception):
            agent.complete("hi")

    def test_pcm_is_wrapped_in_a_wav_container_for_stt_tools(self):
        import io
        import wave

        raw = pcm(1000)
        container = adapters.pcm16_to_wav(raw, sample_rate=16000)
        with wave.open(io.BytesIO(container)) as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.readframes(handle.getnframes()), raw)

    def test_hermes_uses_stock_public_chat_syntax_only(self):
        # Stock `hermes chat`: -q/--query is non-interactive single-query
        # mode, -Q/--quiet is quiet mode for programmatic use. Nothing here
        # may drift towards a flag a stock install would reject.
        agent = adapters.hermes_agent()
        argv, stdin_text = agent._argv("say hi")
        self.assertEqual(argv[:3], ["hermes", "chat", "-q"])
        self.assertEqual(argv, ["hermes", "chat", "-q", "say hi", "-Q"])
        self.assertIsNone(stdin_text)

    def test_hermes_makes_no_sandbox_claim_it_cannot_enforce(self):
        # The old preset passed --safe-mode and the docs read it as a tool
        # restriction, which it is not. The honest posture is no
        # tool-restricting flag at all plus a README that says so, and the
        # operator opts in with -t via CARET_AGENT_COMMAND.
        argv, _ = adapters.hermes_agent()._argv("say hi")
        for flag in ("--safe-mode", "-z", "--oneshot", "--yolo", "-t", "--toolsets"):
            self.assertNotIn(flag, argv)

    def test_the_prompt_survives_as_one_argv_element_unmangled(self):
        # No shell is involved, so quotes, newlines, backticks, $ and ;
        # are inert data. This is the whole security posture of the
        # command adapter and it must not regress.
        nasty = 'a "b" `c`; rm -rf / $HOME\nsecond line \'q\''
        argv, stdin_text = adapters.hermes_agent()._argv(nasty)
        self.assertEqual(argv.count(nasty), 1)
        self.assertEqual(len(argv), 5)
        self.assertIsNone(stdin_text)

    def test_agent_timeouts_map_to_ask_timeout(self):
        agent = adapters.CommandAgent(
            name="sleepy",
            template=f'{sys.executable} -c "import time;time.sleep(5)" {{prompt}}',
            timeout=1,
        )
        from caret_backend.errors import CaretError

        with self.assertRaises(CaretError) as ctx:
            agent.complete("hi")
        self.assertEqual(ctx.exception.code, "ask_timeout")
        self.assertEqual(ctx.exception.status, 504)

    def test_env_wiring_prefers_an_explicit_command_over_a_preset(self):
        self.assertIsInstance(adapters.agent_from_env({"CARET_AGENT": "echo"}), adapters.EchoAgent)
        custom = adapters.agent_from_env({"CARET_AGENT_COMMAND": "my-agent {prompt}", "CARET_AGENT": "echo"})
        self.assertIsInstance(custom, adapters.CommandAgent)
        self.assertIsInstance(
            adapters.transcriber_from_env({"CARET_STT": "off"}), adapters.NullTranscriber
        )
        self.assertIsNone(adapters.image_generator_from_env({}))


class StoreTests(unittest.TestCase):
    def test_the_janitor_removes_sessions_a_ttl_past_expiry(self):
        server = TestServer()
        self.addCleanup(server.close)
        store = server.backend.store
        keep = server.open_session(client_request_id="keep")
        drop = server.open_session(client_request_id="drop")
        store.update_session(drop, expires_at=time.time() - 7200)

        self.assertEqual(store.sweep(), 1)
        self.assertIsNone(store.read_session(drop))
        self.assertIsNotNone(store.read_session(keep))

    def test_a_session_id_cannot_escape_the_data_directory(self):
        server = TestServer()
        self.addCleanup(server.close)
        self.assertIsNone(server.backend.store.read_session("../../etc/passwd"))
        status, body, _ = server.request(
            "POST",
            "/v2/sessions/..%2F..%2Fetc/ack",
            body={},
        )
        self.assertIn(status, (400, 404), body)


class ConformanceTests(unittest.TestCase):
    def test_the_shipped_conformance_checker_passes_against_this_backend(self):
        import conformance

        server = TestServer()
        self.addCleanup(server.close)
        code = conformance.main(["--base-url", server.base_url, "--api-key", API_KEY])
        self.assertEqual(code, 0, "conformance.py must pass against the reference backend")


if __name__ == "__main__":
    unittest.main(verbosity=2)
