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


class HealthTests(ServerCase):
    def test_health_is_anonymous_and_declares_the_contract(self):
        status, body, _ = self.server.request("GET", "/v1/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["contract"], "caret/v1")
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["time"].endswith("Z"))
        self.assertEqual(body["auth"], {"presented": False, "valid": None})

    def test_health_reports_input_modes_per_surface(self):
        _, body, _ = self.server.request("GET", "/v1/health", key=None)
        caps = body["capabilities"]
        self.assertTrue(caps["draft"] and caps["dictation"] and caps["imagine"])
        self.assertEqual(caps["input_modes"]["draft"], ["text", "audio"])
        self.assertEqual(caps["input_modes"]["imagine"], ["text", "audio"])
        self.assertEqual(caps["input_modes"]["dictation"], ["audio"])

    def test_health_validates_a_presented_key_without_requiring_one(self):
        _, ok, _ = self.server.request("GET", "/v1/health")
        self.assertEqual(ok["auth"], {"presented": True, "valid": True})
        _, bad, _ = self.server.request("GET", "/v1/health", key="nope")
        self.assertEqual(bad["auth"], {"presented": True, "valid": False})


class CapabilityHonestyTests(unittest.TestCase):
    def test_a_backend_without_a_transcriber_says_so_and_refuses_audio(self):
        server = TestServer(transcriber=adapters.NullTranscriber(), image_generator=None)
        self.addCleanup(server.close)
        _, health, _ = server.request("GET", "/v1/health", key=None)
        caps = health["capabilities"]
        self.assertFalse(caps["dictation"])
        self.assertFalse(caps["imagine"])
        self.assertEqual(caps["input_modes"]["draft"], ["text"])
        self.assertNotIn("imagine", caps["input_modes"])

        status, body, _ = server.request(
            "POST",
            "/v1/draft",
            body={
                "client_request_id": "c1",
                "input": {
                    "type": "audio",
                    "session_id": "sess_x",
                    "client_chunk_count": 1,
                    "client_total_duration_ms": 1000,
                },
            },
        )
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"]["code"], "unsupported_input_type")

    def test_imagine_is_404_when_unconfigured_not_a_fake_success(self):
        server = TestServer(image_generator=None)
        self.addCleanup(server.close)
        status, body, _ = server.request(
            "POST",
            "/v1/imagine",
            body={"client_request_id": "c1", "input": {"type": "text", "text": "a cat"}},
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["error"]["code"], "not_found")


class AuthTests(ServerCase):
    def test_every_surface_but_health_requires_a_bearer_key(self):
        for method, path in (
            ("POST", "/v1/draft"),
            ("POST", "/v1/imagine"),
            ("POST", "/v1/dictation/sessions"),
        ):
            status, body, _ = self.server.request(method, path, body={}, key=None)
            self.assertEqual(status, 401, path)
            self.assertError(body, "unauthorized")

    def test_a_wrong_key_is_rejected(self):
        status, body, _ = self.server.request("POST", "/v1/draft", body={}, key="wrong")
        self.assertEqual(status, 401)
        self.assertError(body, "unauthorized")

    def test_a_backend_with_no_keys_configured_fails_closed(self):
        server = TestServer(api_keys=())
        self.addCleanup(server.close)
        status, _, _ = server.request("POST", "/v1/draft", body={}, key="anything")
        self.assertEqual(status, 401)
        _, health, _ = server.request("GET", "/v1/health", key=None)
        self.assertEqual(health["status"], "degraded")

    def test_an_unknown_route_is_a_contract_shaped_404(self):
        status, body, _ = self.server.request("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertError(body, "not_found")


class InputModelTests(ServerCase):
    def draft(self, **payload):
        return self.server.request("POST", "/v1/draft", body=payload)

    def test_typed_input_object_is_accepted(self):
        status, body, _ = self.draft(
            client_request_id="c1", input={"type": "text", "text": "say hi"}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "[draft] say hi")
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["input_type"], "text")
        self.assertTrue(body["request_id"])

    def test_the_1_0_instruction_alias_still_works(self):
        status, body, _ = self.draft(client_request_id="c1", instruction="say hi")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "[draft] say hi")

    def test_supplying_both_input_and_the_alias_is_rejected(self):
        status, body, _ = self.draft(
            client_request_id="c1", input={"type": "text", "text": "a"}, instruction="a"
        )
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_supplying_neither_is_rejected(self):
        status, body, _ = self.draft(client_request_id="c1")
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_an_unknown_input_type_is_rejected(self):
        status, body, _ = self.draft(client_request_id="c1", input={"type": "video"})
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

    def test_empty_and_oversized_text_are_rejected_with_their_own_codes(self):
        status, body, _ = self.draft(client_request_id="c1", input={"type": "text", "text": ""})
        self.assertEqual(status, 422)
        self.assertError(body, "input_invalid")

        status, body, _ = self.draft(client_request_id="c2", instruction="x" * 4001)
        self.assertEqual(status, 422)
        self.assertError(body, "instruction_invalid")

    def test_client_request_id_is_required(self):
        status, body, _ = self.draft(input={"type": "text", "text": "hi"})
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")

    def test_a_repeated_client_request_id_replays_the_first_answer(self):
        _, first, _ = self.draft(client_request_id="same", instruction="one")
        _, second, _ = self.draft(client_request_id="same", instruction="two")
        self.assertEqual(second["text"], first["text"])

    def test_a_malformed_body_is_a_400_not_a_traceback(self):
        status, body, _ = self.server.request(
            "POST", "/v1/draft", body=b"{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertError(body, "bad_request")


class SessionTests(ServerCase):
    def test_a_session_declares_the_chunk_limits_the_client_must_honour(self):
        status, body, _ = self.server.request(
            "POST",
            "/v1/dictation/sessions",
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

    def test_only_the_v1_audio_format_is_accepted(self):
        base = {"client_request_id": "c1", "codec": "pcm16", "sample_rate_hz": 16000, "channels": 1}
        status, body, _ = self.server.request(
            "POST", "/v1/dictation/sessions", body={**base, "codec": "opus"}
        )
        self.assertEqual(status, 415)
        self.assertError(body, "unsupported_audio_codec")

        for field, value in (("sample_rate_hz", 44100), ("channels", 2)):
            status, body, _ = self.server.request(
                "POST", "/v1/dictation/sessions", body={**base, field: value}
            )
            self.assertEqual(status, 400, field)
            self.assertError(body, "bad_request")

    def test_reopening_with_the_same_client_request_id_replays_the_session(self):
        first = self.server.open_session(client_request_id="dup")
        second = self.server.open_session(client_request_id="dup")
        self.assertEqual(first, second)

    def test_an_unknown_session_is_404(self):
        status, body, _ = self.server.request(
            "POST",
            "/v1/dictation/sessions/sess_missing/transcript",
            body={"client_chunk_count": 1, "client_total_duration_ms": 1000},
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
            f"/v1/dictation/sessions/{self.session_id}/chunks/0",
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

    def audio(self, session_id: str, *, count: int = 2, polish: bool = True) -> dict:
        return {
            "type": "audio",
            "session_id": session_id,
            "client_chunk_count": count,
            "client_total_duration_ms": count * 1500,
            "polish": polish,
        }

    def test_dictation_returns_the_transcript(self):
        session_id = self.prepare()
        status, body, _ = self.server.request(
            "POST",
            f"/v1/dictation/sessions/{session_id}/transcript",
            body={"client_chunk_count": 2, "client_total_duration_ms": 3000, "polish": False},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "complete")
        self.assertIn("transcribed", body["text"])
        self.assertEqual(body["missing_chunks"], [])
        self.assertEqual(body["duration_ms"], 3000)

    def test_a_gap_in_the_upload_is_a_200_not_an_error(self):
        session_id = self.server.open_session()
        self.server.upload(session_id, 0, pcm(1500))
        status, body, _ = self.server.request(
            "POST",
            f"/v1/dictation/sessions/{session_id}/transcript",
            body={"client_chunk_count": 3, "client_total_duration_ms": 4500},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "missing_chunks")
        self.assertEqual(body["missing_chunks"], [1, 2])
        self.assertIsNone(body["text"])

    def test_ask_accepts_a_dictate_session_instead_of_typed_text(self):
        session_id = self.prepare(intent="ask")
        status, body, _ = self.server.request(
            "POST",
            "/v1/draft",
            body={"client_request_id": "ask-1", "input": self.audio(session_id)},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["input_type"], "audio")
        self.assertEqual(body["session_id"], session_id)
        self.assertTrue(body["transcript"])
        self.assertTrue(body["text"].startswith("[draft] "))
        self.assertEqual(body["duration_ms"], 3000)

    def test_polish_runs_on_the_transcript_before_the_agent_sees_it(self):
        unpolished = self.server.request(
            "POST",
            f"/v1/dictation/sessions/{self.prepare()}/transcript",
            body={"client_chunk_count": 2, "client_total_duration_ms": 3000, "polish": False},
        )[1]["text"]
        polished = self.server.request(
            "POST",
            f"/v1/dictation/sessions/{self.prepare(intent='ask')}/transcript",
            body={"client_chunk_count": 2, "client_total_duration_ms": 3000, "polish": True},
        )[1]["text"]
        # EchoAgent.polish capitalises; the point is that the flag is honoured.
        self.assertNotEqual(polished, unpolished)
        self.assertEqual(polished, unpolished.capitalize())

    def test_imagine_accepts_the_same_audio_input(self):
        session_id = self.prepare(intent="imagine")
        status, body, _ = self.server.request(
            "POST",
            "/v1/imagine",
            body={"client_request_id": "img-1", "input": self.audio(session_id)},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["input_type"], "audio")
        self.assertEqual(body["media"]["mime_type"], "image/png")
        self.assertTrue(body["transcript"])

    def test_a_session_carries_exactly_one_terminal_result(self):
        session_id = self.prepare()
        self.server.request(
            "POST",
            f"/v1/dictation/sessions/{session_id}/transcript",
            body={"client_chunk_count": 2, "client_total_duration_ms": 3000},
        )
        status, body, _ = self.server.request(
            "POST",
            "/v1/draft",
            body={"client_request_id": "steal", "input": self.audio(session_id)},
        )
        self.assertEqual(status, 409, body)
        self.assertError(body, "session_conflict")

    def test_audio_is_deleted_once_the_session_reaches_a_result(self):
        session_id = self.prepare()
        chunks = self.server.backend.store.sessions / session_id / "chunks"
        self.assertTrue(chunks.exists())
        self.server.request(
            "POST",
            f"/v1/dictation/sessions/{session_id}/transcript",
            body={"client_chunk_count": 2, "client_total_duration_ms": 3000},
        )
        self.assertFalse(chunks.exists(), "raw audio must not outlive its result")

    def test_re_posting_a_finished_request_replays_the_cached_result(self):
        session_id = self.prepare()
        body = {"client_chunk_count": 2, "client_total_duration_ms": 3000}
        first = self.server.request(
            "POST", f"/v1/dictation/sessions/{session_id}/transcript", body=body
        )[1]
        second = self.server.request(
            "POST", f"/v1/dictation/sessions/{session_id}/transcript", body=body
        )[1]
        self.assertEqual(first, second)


class AsyncTests(unittest.TestCase):
    """The 202 convention, observed against work that is genuinely slow."""

    def setUp(self) -> None:
        self.agent = BlockingAgent()
        self.server = TestServer(inline_jobs=False, agent=self.agent)
        self.addCleanup(self.server.close)
        self.addCleanup(self.agent.released.set)

    def post(self):
        return self.server.request(
            "POST",
            "/v1/draft",
            body={"client_request_id": "slow-1", "input": {"type": "text", "text": "wait"}},
        )

    def test_in_progress_is_202_with_retry_after_and_a_stable_request_id(self):
        session_id = self.server.open_session(intent="ask")
        self.server.upload(session_id, 0, pcm(1000))
        body = {
            "client_request_id": "slow-audio",
            "input": {
                "type": "audio",
                "session_id": session_id,
                "client_chunk_count": 1,
                "client_total_duration_ms": 1000,
            },
        }
        status, first, headers = self.server.request("POST", "/v1/draft", body=body)
        self.assertEqual(status, 202, first)
        self.assertEqual(first["status"], "in_progress")
        self.assertTrue(int(headers["Retry-After"]) >= 1)
        self.assertEqual(headers["Retry-After"], str(first["retry_after_seconds"]))

        # Polling is re-POSTing the identical body — not a new request.
        status, again, _ = self.server.request("POST", "/v1/draft", body=body)
        self.assertEqual(status, 202)
        self.assertEqual(again["request_id"], first["request_id"])

        self.agent.released.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            status, final, _ = self.server.request("POST", "/v1/draft", body=body)
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
            "input": {
                "type": "audio",
                "session_id": session_id,
                "client_chunk_count": 1,
                "client_total_duration_ms": 1000,
            },
        }
        seen: list[str] = []
        lock = threading.Lock()

        def poll():
            _, payload, _ = self.server.request("POST", "/v1/draft", body=body)
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
            "/v1/imagine",
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
                "/v1/imagine",
                body={
                    "client_request_id": f"i-{field}",
                    "input": {"type": "text", "text": "x"},
                    field: "enormous",
                },
            )
            self.assertEqual(status, 422, field)
            self.assertError(body, code)


class RateLimitTests(unittest.TestCase):
    def test_a_flood_from_one_key_is_throttled_not_served(self):
        server = TestServer(rate_limit_per_minute=3)
        self.addCleanup(server.close)
        codes = [
            server.request(
                "POST",
                "/v1/draft",
                body={"client_request_id": f"r{i}", "input": {"type": "text", "text": "hi"}},
            )[0]
            for i in range(5)
        ]
        self.assertEqual(codes[:3], [200, 200, 200], codes)
        self.assertEqual(codes[3:], [429, 429], codes)
        _, body, _ = server.request(
            "POST", "/v1/draft", body={"client_request_id": "r9", "input": {"type": "text", "text": "hi"}}
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
            "/v1/dictation/sessions/..%2F..%2Fetc/transcript",
            body={"client_chunk_count": 1, "client_total_duration_ms": 100},
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
