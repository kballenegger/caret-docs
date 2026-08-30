"""Hermetic tests for the pluggable Ask and STT adapters.

Every agent and STT tool here is a stub — an in-process fake, a tiny
`python3 -c` script, or a loopback stub HTTP server. No network beyond
loopback, no real agent runtime, no model tokens, no credentials.

Be precise about what these tests buy: preset command lines are asserted,
and the adapter mechanics ({prompt}/{out}/{audio}/{out_dir}, error
mapping) are executed against stubs — but the suite never executes
hermes, claude, codex, or whisper themselves.
"""
from __future__ import annotations

import json
import shlex
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caret_backend import adapters  # noqa: E402
from caret_backend.errors import CaretError  # noqa: E402

PY = sys.executable


class AgentPresetTests(unittest.TestCase):
    def test_claude_code_preset_uses_documented_read_only_mode(self):
        template = adapters.AGENT_PRESETS["claude-code"]
        self.assertIn("--permission-mode plan", template)
        self.assertIn("--no-session-persistence", template)
        self.assertTrue(template.startswith("claude -p"))

    def test_codex_preset_uses_documented_read_only_sandbox(self):
        template = adapters.AGENT_PRESETS["codex"]
        self.assertIn("--sandbox read-only", template)
        self.assertIn("--ephemeral", template)
        self.assertIn("--output-last-message {out}", template)
        self.assertTrue(template.startswith("codex exec"))

    def test_hermes_preset_is_stock_public_chat_syntax(self):
        parts = adapters.AGENT_PRESETS["hermes"].replace('"{prompt}"', "{prompt}").split()
        self.assertEqual(parts, ["hermes", "chat", "-q", "{prompt}", "-Q"])

    def test_presets_resolve_by_name_regardless_of_path(self):
        for preset in adapters.AGENT_PRESETS:
            agent = adapters.agent_from_env({"CARET_AGENT": preset})
            self.assertIsInstance(agent, adapters.CommandAgent)
            self.assertEqual(agent.name, preset)
            self.assertEqual(agent.template, adapters.AGENT_PRESETS[preset])

    def test_openclaw_without_explicit_command_is_a_startup_error(self):
        with self.assertRaises(CaretError) as ctx:
            adapters.agent_from_env({"CARET_AGENT": "openclaw"})
        self.assertIn("not yet verified", ctx.exception.message)

    def test_openclaw_with_explicit_command_is_accepted(self):
        agent = adapters.agent_from_env(
            {"CARET_AGENT": "openclaw", "CARET_AGENT_COMMAND": "openclaw run {prompt}"}
        )
        self.assertIsInstance(agent, adapters.CommandAgent)
        self.assertEqual(agent.name, "openclaw")

    def test_unknown_preset_is_refused_with_the_valid_list(self):
        with self.assertRaises(CaretError) as ctx:
            adapters.agent_from_env({"CARET_AGENT": "skynet"})
        for preset in ("custom-http", "grokbot", "off"):
            self.assertIn(preset, ctx.exception.message)

    def test_auto_picks_the_first_installed_runtime(self):
        installed = {"claude"}
        with mock.patch.object(
            adapters.shutil, "which", side_effect=lambda name: name in installed or None
        ):
            agent = adapters.agent_from_env({})
        self.assertEqual(agent.name, "claude-code")

    def test_auto_falls_back_to_no_agent_never_to_the_stand_in(self):
        # Ask is optional, so "nothing installed" is a valid backend — but
        # it must be an honest one. Falling back to EchoAgent here would
        # ship a backend that answers every draft with a stand-in and looks
        # healthy doing it.
        with mock.patch.object(adapters.shutil, "which", return_value=None):
            agent = adapters.agent_from_env({})
        self.assertIsInstance(agent, adapters.NullAgent)
        self.assertNotIsInstance(agent, adapters.EchoAgent)

    def test_the_stand_in_is_only_ever_selected_explicitly(self):
        self.assertIsInstance(adapters.agent_from_env({"CARET_AGENT": "echo"}), adapters.EchoAgent)

    def test_off_selects_no_agent(self):
        for value in ("off", "none"):
            self.assertIsInstance(
                adapters.agent_from_env({"CARET_AGENT": value}), adapters.NullAgent
            )

    def test_no_agent_refuses_every_surface_rather_than_answering(self):
        agent = adapters.NullAgent()
        for call in (
            lambda: agent.complete("hi"),
            lambda: agent.draft(instruction="hi", visible_text=None, app_hint=None),
            lambda: agent.polish("hi"),
        ):
            with self.assertRaises(CaretError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 404)


class CommandSafetyTests(unittest.TestCase):
    def test_permission_bypass_flags_are_refused_at_startup(self):
        for token in adapters.FORBIDDEN_COMMAND_TOKENS:
            with self.assertRaises(CaretError):
                adapters.agent_from_env(
                    {"CARET_AGENT_COMMAND": f"some-agent {token} {{prompt}}"}
                )

    def test_shipped_presets_contain_no_forbidden_tokens(self):
        for template in adapters.AGENT_PRESETS.values():
            adapters.check_command_safety(template)  # must not raise


class CommandAgentOutFileTests(unittest.TestCase):
    def test_answer_file_mode_reads_out_not_stdout(self):
        script = (
            "import sys, pathlib; "
            "pathlib.Path(sys.argv[1]).write_text('from file'); "
            "print('event log noise')"
        )
        agent = adapters.CommandAgent(
            name="stub", template=f'{PY} -c "{script}" {{out}} {{prompt}}'
        )
        self.assertEqual(agent.complete("hi"), "from file")

    def test_missing_answer_file_is_an_error_not_an_empty_draft(self):
        agent = adapters.CommandAgent(
            name="stub", template=f'{PY} -c "print()" {{out}} {{prompt}}'
        )
        with self.assertRaises(CaretError) as ctx:
            agent.complete("hi")
        self.assertEqual(ctx.exception.status, 503)


class _StubUpstream(BaseHTTPRequestHandler):
    behaviour = staticmethod(lambda: (200, {"text": "hosted answer"}))
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        type(self).seen.append(
            {
                "body": body,
                "auth": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        status, payload = type(self).behaviour()
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


class _StubServerMixin:
    def start_upstream(self):
        handler = type("Upstream", (_StubUpstream,), {"seen": []})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return handler, f"http://127.0.0.1:{server.server_address[1]}/endpoint"


class HttpAgentTests(_StubServerMixin, unittest.TestCase):
    def test_happy_path_posts_prompt_and_returns_text(self):
        handler, url = self.start_upstream()
        agent = adapters.HttpAgent(name="custom-http", url=url)
        text = agent.draft(instruction="say hi", visible_text=None, app_hint=None)
        self.assertEqual(text, "hosted answer")
        self.assertIn("say hi", handler.seen[0]["body"]["prompt"])

    def test_bearer_token_is_sent_when_configured(self):
        handler, url = self.start_upstream()
        adapters.HttpAgent(name="custom-http", url=url, bearer="tok123").complete("hi")
        self.assertEqual(handler.seen[0]["auth"], "Bearer tok123")

    def test_upstream_failures_map_to_contract_shaped_503(self):
        handler, url = self.start_upstream()
        agent = adapters.HttpAgent(name="custom-http", url=url)
        for behaviour in (
            lambda: (500, {"error": "x"}),
            lambda: (200, b"not json"),
            lambda: (200, {"answer": "wrong key"}),
        ):
            handler.behaviour = staticmethod(behaviour)
            with self.assertRaises(CaretError) as ctx:
                agent.complete("hi")
            self.assertEqual(ctx.exception.status, 503)

    def test_unreachable_upstream_maps_to_503(self):
        agent = adapters.HttpAgent(
            name="custom-http", url="http://127.0.0.1:1/endpoint", timeout=1
        )
        with self.assertRaises(CaretError) as ctx:
            agent.complete("hi")
        self.assertEqual(ctx.exception.status, 503)

    def test_env_wiring_requires_a_url_with_a_web_scheme(self):
        with self.assertRaises(CaretError):
            adapters.agent_from_env({"CARET_AGENT": "custom-http"})
        with self.assertRaises(CaretError):
            adapters.agent_from_env(
                {
                    "CARET_AGENT": "custom-http",
                    "CARET_AGENT_HTTP_URL": "file:///etc/passwd",
                }
            )
        agent = adapters.agent_from_env(
            {
                "CARET_AGENT": "custom-http",
                "CARET_AGENT_HTTP_URL": "https://example.com/draft",
            }
        )
        self.assertIsInstance(agent, adapters.HttpAgent)

    def test_custom_http_is_not_grokbot(self):
        # They are different backends with different capability surfaces.
        # Selecting one must never quietly give you the other.
        agent = adapters.agent_from_env(
            {
                "CARET_AGENT": "custom-http",
                "CARET_AGENT_HTTP_URL": "https://example.com/draft",
            }
        )
        self.assertNotIsInstance(agent, adapters.GrokBotAgent)
        self.assertEqual(agent.name, "custom-http")


class GrokBotTests(_StubServerMixin, unittest.TestCase):
    """GrokBot as a first-party backend, against a stub.

    What this proves is exactly one half of the integration: that this
    backend speaks the documented contract correctly. The GrokBot side is a
    public configuration contract a GrokBot deployment implements, and no
    live GrokBot deployment has been exercised from this repository — the
    Connect matrix says so, and so does this docstring."""

    def env(self, url, **extra):
        return {"CARET_AGENT": "grokbot", "CARET_GROKBOT_URL": url, **extra}

    def test_ask_posts_the_ask_task_and_returns_text(self):
        handler, url = self.start_upstream()
        agent = adapters.agent_from_env(self.env(url))
        text = agent.draft(instruction="say hi", visible_text=None, app_hint=None)
        self.assertEqual(text, "hosted answer")
        self.assertEqual(handler.seen[0]["body"]["task"], "ask")
        self.assertIn("say hi", handler.seen[0]["body"]["prompt"])

    def test_cleanup_is_a_distinct_task_carrying_the_constrained_framing(self):
        handler, url = self.start_upstream()
        agent = adapters.agent_from_env(self.env(url))
        agent.polish("um so like the meeting is at four")
        body = handler.seen[0]["body"]
        self.assertEqual(body["task"], "cleanup")
        # The constraint has to travel with the request, not be assumed: the
        # framing leads, and the transcript follows inside the inert envelope.
        self.assertTrue(body["prompt"].startswith(adapters.POLISH_FRAMING))
        self.assertTrue(body["prompt"].endswith(
            "<transcript>\num so like the meeting is at four\n</transcript>"
        ))

    def test_bearer_is_sent_when_configured_and_absent_when_not(self):
        handler, url = self.start_upstream()
        adapters.agent_from_env(self.env(url, CARET_GROKBOT_BEARER="tok123")).complete("hi")
        self.assertEqual(handler.seen[0]["auth"], "Bearer tok123")
        handler2, url2 = self.start_upstream()
        adapters.agent_from_env(self.env(url2)).complete("hi")
        self.assertIsNone(handler2.seen[0]["auth"])

    def test_images_off_by_default_so_imagine_is_not_advertised_falsely(self):
        agent = adapters.agent_from_env(self.env("https://grok.example/caret"))
        self.assertIsInstance(agent, adapters.GrokBotAgent)
        self.assertFalse(adapters.agent_supports_imagine(agent))
        self.assertIsNone(adapters.image_generator_from_env({}, agent=agent))

    def test_images_on_routes_imagine_to_the_agent(self):
        agent = adapters.agent_from_env(
            self.env("https://grok.example/caret", CARET_GROKBOT_IMAGE="on")
        )
        self.assertIsInstance(agent, adapters.GrokBotImagingAgent)
        self.assertTrue(adapters.agent_supports_imagine(agent))
        self.assertIs(adapters.image_generator_from_env({}, agent=agent), agent)

    def test_imagine_posts_the_imagine_task_and_decodes_a_png(self):
        import base64

        png = adapters.FakeImageGenerator._PNG
        handler, url = self.start_upstream()
        handler.behaviour = staticmethod(
            lambda: (200, {"image_base64": base64.b64encode(png).decode()})
        )
        agent = adapters.agent_from_env(self.env(url, CARET_GROKBOT_IMAGE="on"))
        self.assertEqual(agent.generate("a cat", aspect_ratio="1:1", quality="low"), png)
        body = handler.seen[0]["body"]
        self.assertEqual(body["task"], "imagine")
        self.assertEqual(body["aspect_ratio"], "1:1")
        self.assertEqual(body["quality"], "low")

    def test_a_bad_image_response_is_a_503_not_a_corrupt_png(self):
        handler, url = self.start_upstream()
        agent = adapters.agent_from_env(self.env(url, CARET_GROKBOT_IMAGE="on"))
        for behaviour in (
            lambda: (200, {"text": "I can't draw"}),        # wrong key
            lambda: (200, {"image_base64": "!!!not base64"}),
            lambda: (200, {"image_base64": "aGVsbG8="}),     # valid base64, not a PNG
            lambda: (500, {"error": "x"}),
        ):
            handler.behaviour = staticmethod(behaviour)
            with self.assertRaises(CaretError) as ctx:
                agent.generate("a cat", aspect_ratio="1:1", quality="low")
            self.assertEqual(ctx.exception.status, 503)

    def test_grokbot_never_claims_stt(self):
        for images in ("off", "on"):
            agent = adapters.agent_from_env(
                self.env("https://grok.example/caret", CARET_GROKBOT_IMAGE=images)
            )
            self.assertFalse(adapters.agent_supports_stt(agent))

    def test_config_errors_are_specific(self):
        with self.assertRaises(CaretError) as ctx:
            adapters.agent_from_env({"CARET_AGENT": "grokbot"})
        self.assertIn("CARET_GROKBOT_URL", ctx.exception.message)
        with self.assertRaises(CaretError):
            adapters.agent_from_env(self.env("file:///etc/passwd"))
        with self.assertRaises(CaretError) as ctx:
            adapters.agent_from_env(
                self.env("https://grok.example/caret", CARET_GROKBOT_IMAGE="maybe")
            )
        self.assertIn("CARET_GROKBOT_IMAGE", ctx.exception.message)

    def test_upstream_failures_map_to_contract_shaped_503(self):
        handler, url = self.start_upstream()
        agent = adapters.agent_from_env(self.env(url))
        for behaviour in (
            lambda: (500, {"error": "x"}),
            lambda: (200, b"not json"),
            lambda: (200, {"answer": "wrong key"}),
            lambda: (200, {"text": "   "}),
        ):
            handler.behaviour = staticmethod(behaviour)
            with self.assertRaises(CaretError) as ctx:
                agent.complete("hi")
            self.assertEqual(ctx.exception.status, 503)
            self.assertIn("GrokBot", ctx.exception.message)


class OpenWhisperTests(unittest.TestCase):
    def test_preset_uses_stock_documented_syntax(self):
        transcriber = adapters.openwhisper_transcriber()
        parts = shlex.split(transcriber.template)
        self.assertEqual(parts[0], adapters.OPENWHISPER_EXECUTABLE)
        self.assertEqual(parts[1], "{audio}")
        self.assertIn("--model", parts)
        self.assertIn(adapters.OPENWHISPER_DEFAULT_MODEL, parts)
        self.assertIn("--output_format", parts)
        self.assertIn("txt", parts)
        self.assertIn("--output_dir", parts)
        self.assertIn("{out_dir}", parts)

    def test_model_is_configurable(self):
        transcriber = adapters.openwhisper_transcriber(model="base")
        self.assertIn("--model base", transcriber.template)

    def test_out_dir_mode_reads_the_transcript_file(self):
        # A stub with OpenWhisper's exact output shape: writes
        # <out_dir>/audio.txt, prints progress noise on stdout.
        script = (
            "import sys, pathlib; "
            "out = pathlib.Path(sys.argv[2]) / 'audio.txt'; "
            "out.write_text('hello from stt'); "
            "print('detected language: en')"
        )
        transcriber = adapters.CommandTranscriber(
            name="stub-whisper", template=f'{PY} -c "{script}" {{audio}} {{out_dir}}'
        )
        self.assertEqual(transcriber.transcribe(b"\x00\x00" * 160), "hello from stt")

    def test_missing_transcript_file_is_a_transcription_error(self):
        transcriber = adapters.CommandTranscriber(
            name="stub-whisper", template=f'{PY} -c "pass" {{audio}} {{out_dir}}'
        )
        with self.assertRaises(CaretError) as ctx:
            transcriber.transcribe(b"\x00\x00" * 160)
        self.assertEqual(ctx.exception.code, "transcription_failed")

    def test_auto_defaults_to_openwhisper_when_installed(self):
        with mock.patch.object(
            adapters.shutil,
            "which",
            side_effect=lambda name: name == adapters.OPENWHISPER_EXECUTABLE or None,
        ):
            transcriber = adapters.transcriber_from_env({})
        self.assertEqual(transcriber.name, "openwhisper")

    def test_auto_is_honestly_off_when_not_installed(self):
        with mock.patch.object(adapters.shutil, "which", return_value=None):
            transcriber = adapters.transcriber_from_env({})
        self.assertIsInstance(transcriber, adapters.NullTranscriber)

    def test_explicit_openwhisper_without_the_binary_is_a_startup_error(self):
        with mock.patch.object(adapters.shutil, "which", return_value=None):
            with self.assertRaises(CaretError) as ctx:
                adapters.transcriber_from_env({"CARET_STT": "openwhisper"})
        self.assertIn("openai-whisper", ctx.exception.message)


class HttpTranscriberTests(_StubServerMixin, unittest.TestCase):
    def test_posts_wav_and_returns_text(self):
        handler, url = self.start_upstream()
        handler.behaviour = staticmethod(lambda: (200, {"text": "spoken words"}))
        transcriber = adapters.HttpTranscriber(name="http", url=url)
        self.assertEqual(transcriber.transcribe(b"\x00\x00" * 160), "spoken words")
        self.assertEqual(handler.seen[0]["content_type"], "audio/wav")
        self.assertEqual(handler.seen[0]["body"][:4], b"RIFF")

    def test_failures_map_to_transcription_failed(self):
        handler, url = self.start_upstream()
        handler.behaviour = staticmethod(lambda: (500, {"error": "x"}))
        transcriber = adapters.HttpTranscriber(name="http", url=url)
        with self.assertRaises(CaretError) as ctx:
            transcriber.transcribe(b"\x00\x00" * 160)
        self.assertEqual(ctx.exception.code, "transcription_failed")

    def test_env_wiring_selects_http_stt(self):
        explicit = adapters.transcriber_from_env(
            {"CARET_STT": "http", "CARET_STT_HTTP_URL": "https://example.com/stt"}
        )
        self.assertIsInstance(explicit, adapters.HttpTranscriber)
        auto = adapters.transcriber_from_env(
            {"CARET_STT_HTTP_URL": "https://example.com/stt"}
        )
        self.assertIsInstance(auto, adapters.HttpTranscriber)
        with self.assertRaises(CaretError):
            adapters.transcriber_from_env({"CARET_STT": "http"})


class CapabilityRoutingTests(unittest.TestCase):
    """STT and Imagine route through the agent only when its adapter
    verifiably provides the capability; health tells the truth."""

    class TranscribingAgent(adapters.EchoAgent):
        def transcribe(self, pcm, *, sample_rate=16000):
            return "agent heard you"

        def generate(self, prompt, *, aspect_ratio, quality):
            return b"\x89PNG fake"

    def test_no_shipped_preset_claims_stt(self):
        # Dictation is mandatory, and no agent runtime has a verified
        # non-interactive transcription interface — so STT is always the
        # backend's own lane. If this ever fails, the docs are now lying.
        for preset in list(adapters.AGENT_PRESETS) + ["custom-http", "grokbot"]:
            env = {"CARET_AGENT": preset}
            if preset == "custom-http":
                env["CARET_AGENT_HTTP_URL"] = "https://example.com/draft"
            if preset == "grokbot":
                env["CARET_GROKBOT_URL"] = "https://grok.example/caret"
                env["CARET_GROKBOT_IMAGE"] = "on"
            agent = adapters.agent_from_env(env)
            self.assertFalse(adapters.agent_supports_stt(agent), preset)

    def test_grokbot_with_images_is_the_only_preset_that_claims_imagine(self):
        claims = set()
        for preset in list(adapters.AGENT_PRESETS) + ["custom-http", "grokbot", "echo", "off"]:
            for images in ("off", "on"):
                env = {"CARET_AGENT": preset, "CARET_GROKBOT_IMAGE": images}
                if preset == "custom-http":
                    env["CARET_AGENT_HTTP_URL"] = "https://example.com/draft"
                if preset == "grokbot":
                    env["CARET_GROKBOT_URL"] = "https://grok.example/caret"
                if adapters.agent_supports_imagine(adapters.agent_from_env(env)):
                    claims.add((preset, images))
        self.assertEqual(claims, {("grokbot", "on")})

    def test_auto_routes_stt_through_an_agent_that_transcribes(self):
        agent = self.TranscribingAgent()
        transcriber = adapters.transcriber_from_env({}, agent=agent)
        self.assertIs(transcriber, agent)

    def test_explicit_stt_config_beats_agent_stt(self):
        agent = self.TranscribingAgent()
        chosen = adapters.transcriber_from_env(
            {"CARET_STT_COMMAND": "my-stt {audio}"}, agent=agent
        )
        self.assertIsInstance(chosen, adapters.CommandTranscriber)

    def test_stt_agent_preset_requires_a_transcribing_agent(self):
        with self.assertRaises(CaretError):
            adapters.transcriber_from_env(
                {"CARET_STT": "agent"}, agent=adapters.EchoAgent()
            )

    def test_imagine_routes_through_an_agent_that_generates(self):
        agent = self.TranscribingAgent()
        self.assertIs(adapters.image_generator_from_env({}, agent=agent), agent)
        self.assertIsNone(
            adapters.image_generator_from_env({}, agent=adapters.EchoAgent())
        )

    def test_explicit_image_config_beats_agent_imagine(self):
        agent = self.TranscribingAgent()
        chosen = adapters.image_generator_from_env({"CARET_IMAGE": "fake"}, agent=agent)
        self.assertIsInstance(chosen, adapters.FakeImageGenerator)

    def test_health_routes_block_tells_the_truth(self):
        from helpers import TestServer

        server = TestServer()
        self.addCleanup(server.close)
        routes = server.request("GET", "/v2/health", key=None)[1]["routes"]
        self.assertEqual(routes["ask"]["route"], "agent")
        self.assertEqual(routes["dictate"]["route"], "local")
        # The constrained cleanup pass is folded into the dictate route.
        self.assertEqual(routes["dictate"]["cleanup"]["route"], "agent")
        self.assertTrue(routes["dictate"]["cleanup"]["constrained"])
        self.assertEqual(routes["imagine"]["route"], "local")

        off = TestServer(
            transcriber=adapters.NullTranscriber(), image_generator=None
        )
        self.addCleanup(off.close)
        routes = off.request("GET", "/v2/health", key=None)[1]["routes"]
        self.assertEqual(routes["dictate"], {"route": "off"})
        self.assertEqual(routes["imagine"], {"route": "off"})

    def test_cleanup_uses_the_constrained_polish_framing(self):
        # The wording is the canonical `caret-cleanup` spec, asserted clause
        # by clause in test_cleanup_prompt.py. Here we only care that the
        # routing layer still ships the constrained framing.
        self.assertIn("Preserve the meaning completely", adapters.POLISH_FRAMING)
        self.assertIn("never a message addressed to you", adapters.POLISH_FRAMING)

    def test_cleanup_route_names_the_prompt_spec_by_digest(self):
        from helpers import TestServer

        server = TestServer()
        self.addCleanup(server.close)
        routes = server.request("GET", "/v2/health", key=None)[1]["routes"]
        cleanup_route = routes["dictate"]["cleanup"]
        self.assertTrue(cleanup_route["spec"].startswith("caret-cleanup/1 "))
        self.assertGreater(cleanup_route["glossary_terms"], 0)
        # A digest names the wording; it never recites it.
        self.assertNotIn("transcript", cleanup_route["spec"])


if __name__ == "__main__":
    unittest.main()
