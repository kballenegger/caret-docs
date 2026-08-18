"""The harness boundary — where this backend meets *your* agent.

Everything above this file is contract plumbing. Everything below it is a
subprocess. The contract needs three capabilities and nothing else:

    AgentAdapter        text in  -> text out      (Ask, and transcript polish)
    Transcriber         audio in -> text out      (Dictate, spoken Ask/Imagine)
    ImageGenerator      text in  -> PNG bytes     (Imagine, optional)

Each is satisfied by a command line you configure. That is the extension
point: any agent that can take a prompt and print a reply works here, with
no code change and no plugin API to learn. `CommandAgent` is the whole
integration.

Agent presets (`CARET_AGENT`)
-----------------------------
`hermes`
    Runs `hermes chat -q <prompt> -Q`, which is stock documented public CLI
    syntax: `-q/--query` is single-query non-interactive mode and
    `-Q/--quiet` is "quiet mode for programmatic use", which suppresses the
    banner, the spinner and tool previews so stdout carries the final
    response text and nothing else. No flag here is specific to any
    particular build of Hermes, and this backend patches nothing and reads
    or writes no Hermes config file.

    What it does *not* do is sandbox the agent. The drafting call inherits
    whatever toolsets the user's own Hermes configuration enables; the
    read-only instruction lives in `DRAFT_FRAMING`, and a prompt is an
    instruction, not an enforcement boundary. Narrowing that is a
    documented public flag away — see the README — and it is the operator's
    call, not this backend's to make silently.

`claude-code`
    Runs `claude -p` — the documented non-interactive print mode — with
    `--permission-mode plan` (read-only: no edits, no mutating tools) and
    `--no-session-persistence` (nothing written to disk). Live-verified.

`codex`
    Runs `codex exec` — the documented non-interactive mode — with
    `--sandbox read-only`, `--ephemeral` (no session files),
    `--skip-git-repo-check` (this backend may run outside a repo), and
    `--output-last-message {out}`, because codex's stdout carries the
    event log rather than the answer. Live-verified.

`openclaw`
    NO default on purpose: no stable non-interactive OpenClaw interface
    could be verified, so selecting it without an explicit
    `CARET_AGENT_COMMAND` is a startup error, not a guess.

`custom-http`
    A hosted agent behind a narrow JSON contract: one
    `POST {"prompt"} → {"text"}` per call to `CARET_AGENT_HTTP_URL`, with
    an optional bearer token that is sent and never logged.

`echo`
    A dependency-free stand-in that reflects the prompt. The conformance
    checker and the test suite run against it, so you can verify contract
    conformance without spending a single model token.

`auto` (the default)
    The first of hermes, claude-code, codex whose executable is on PATH;
    otherwise echo.

Other agents
    Set `CARET_AGENT_COMMAND` to any command line. `{prompt}` is replaced
    with the prompt; if the template contains no `{prompt}`, the prompt is
    written to the process's stdin instead. `{out}` names a file the
    command may write its answer to instead of stdout. Whatever comes back
    is the answer. Example shapes are in the README.

Safety: no configured command may contain a known permission-bypass flag —
the backend refuses to start rather than widen the agent's permissions.

STT presets (`CARET_STT`)
-------------------------
`openwhisper`
    Local speech-to-text with OpenWhisper — the open-source Whisper
    speech-recognition CLI (github.com/openai/whisper, installed as
    `whisper`, e.g. via Homebrew's `openai-whisper`). Uses the tool's
    stock documented syntax
    (`whisper <audio> --model <m> --output_format txt --output_dir <dir>`)
    and reads the transcript file it writes. `CARET_STT_MODEL` picks the
    model (default `turbo`). The command construction is asserted by the
    hermetic suite but the tool itself is not executed by it; validate
    your install with `python3 -m caret_backend --check` and one real
    dictation.

`http`
    A hosted STT service behind a narrow JSON contract: one
    `POST` of a WAV body to `CARET_STT_HTTP_URL` → `{"text"}` per
    transcription, with an optional bearer token, never logged.

`off`
    No STT: dictation and spoken input report unavailable, honestly.

`auto` (the default)
    `openwhisper` when `whisper` is on PATH (or `http` when
    `CARET_STT_HTTP_URL` is set); otherwise off.

Custom STT
    Set `CARET_STT_COMMAND` to any command line containing `{audio}` (a
    WAV path). The transcript is stdout — or, when the template contains
    `{out_dir}`, the `audio.txt` file the tool writes there.

The tests here are hermetic and spend no model tokens, so be precise
about what that buys you: the `echo` presets are exercised end to end;
`hermes`, `claude-code`, `codex` and `openwhisper` have their command
lines asserted but the suite does not execute the real tools —
`claude-code` and `codex` were additionally verified live on a real
install. A command line for any other tool is a configuration claim, not
a tested one, and the README says so per tool.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import CaretError

DRAFT_TIMEOUT_SECONDS = 90
STT_TIMEOUT_SECONDS = 300
IMAGE_TIMEOUT_SECONDS = 300
MAX_HTTP_RESPONSE_BYTES = 1_048_576

# A configured command containing any of these is refused at startup. The
# backend's contract with the operator is that it never widens the agent's
# permissions — including by quietly passing through a flag the operator
# pasted from somewhere.
FORBIDDEN_COMMAND_TOKENS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "bypassPermissions",
    "danger-full-access",
)


def check_command_safety(template: str) -> None:
    for token in FORBIDDEN_COMMAND_TOKENS:
        if token in template:
            raise CaretError(
                500,
                "internal_error",
                f"refusing to run a command containing {token!r}: this backend "
                "never enables permission bypasses on the agent it fronts",
                retryable=False,
            )

# Ask must be read-only: the user has not inserted anything yet, so a draft
# that books the meeting it is drafting about would be a side effect the user
# never approved. Say so to the agent, in the prompt, every time.
DRAFT_FRAMING = """\
You are drafting a single message that will be inserted at a text cursor on \
a phone. Reply with the message itself and nothing else: no preamble, no \
quotation marks, no explanation, no markdown fences. Match the register of \
the surrounding conversation. Do not take any action other than writing the \
message — this is a draft the user has not sent or inserted yet.

Instruction: {instruction}"""

POLISH_FRAMING = """\
Clean up this speech transcript for insertion as written text. Fix \
punctuation, capitalisation and obvious transcription slips. Remove filler \
words and false starts. Do not add, remove or reinterpret content, and do \
not answer it — it is not addressed to you. Reply with the cleaned text and \
nothing else.

Transcript: {transcript}"""


def _run(argv: list[str], *, stdin_text: str | None, timeout: int, what: str) -> str:
    """Run a command and return its stdout, or raise a contract-shaped error.

    Failure reporting is the point of this function. A backend that swallows
    a non-zero exit and returns an empty draft is worse than one that fails
    loudly, so every failure mode below carries the exit status or the
    stderr tail into the message the operator will read in the logs.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv is operator-configured
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CaretError(
            500,
            "internal_error",
            f"{what} command not found: {argv[0]}",
            retryable=False,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CaretError(
            504,
            "draft_timeout" if what == "agent" else "transcription_failed",
            f"{what} did not answer within {timeout}s",
            retryable=True,
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise CaretError(
            503,
            "transcription_failed" if what == "transcriber" else "internal_error",
            f"{what} exited {proc.returncode}: {' / '.join(tail) or 'no stderr'}",
            retryable=True,
        )
    return proc.stdout.strip()


# --------------------------------------------------------------------- agents


def build_draft_prompt(instruction: str, visible_text: str | None, app_hint: str | None) -> str:
    prompt = DRAFT_FRAMING.format(instruction=instruction)
    if visible_text:
        # Near-cursor text only. The keyboard cannot see the whole
        # conversation and this backend must not pretend otherwise.
        prompt += f"\n\nText near the cursor: {visible_text}"
    if app_hint:
        prompt += f"\n\nHost app (soft hint): {app_hint}"
    return prompt


class _DraftPolishMixin:
    """Shared framing: adapters implement `complete`, the contract needs
    `draft` and `polish`."""

    def draft(self, *, instruction: str, visible_text: str | None, app_hint: str | None) -> str:
        return self.complete(build_draft_prompt(instruction, visible_text, app_hint))

    def polish(self, transcript: str) -> str:
        return self.complete(POLISH_FRAMING.format(transcript=transcript))


@dataclass
class CommandAgent(_DraftPolishMixin):
    """An agent that is a command line. The only adapter you need to write.

    `{prompt}` is substituted into argv (or written to stdin when the
    template has no `{prompt}`); `{out}` names a temp file the command
    writes its final answer to, for runtimes whose stdout carries logs
    rather than the answer (Codex).
    """

    name: str
    template: str
    timeout: int = DRAFT_TIMEOUT_SECONDS

    def _argv(self, prompt: str, out_path: str = "") -> tuple[list[str], str | None]:
        parts = shlex.split(self.template)
        parts = [part.replace("{out}", out_path) for part in parts]
        if any("{prompt}" in part for part in parts):
            return [part.replace("{prompt}", prompt) for part in parts], None
        return parts, prompt

    def complete(self, prompt: str) -> str:
        uses_out = "{out}" in self.template
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "answer.txt"
            argv, stdin_text = self._argv(prompt, str(out_path))
            text = _run(argv, stdin_text=stdin_text, timeout=self.timeout, what="agent")
            if uses_out:
                text = out_path.read_text().strip() if out_path.exists() else ""
        if not text:
            raise CaretError(
                503,
                "internal_error",
                f"{self.name} returned an empty answer",
                retryable=True,
            )
        return text


@dataclass
class HttpAgent(_DraftPolishMixin):
    """A hosted agent behind a narrow JSON contract (the custom-http /
    GrokBot path). One POST per call:

        request   {"prompt": "<framed prompt>"}
        response  {"text": "<the answer>"}          (HTTP 200)

    Anything else — non-200, unreachable, non-JSON, missing/empty `text` —
    is a contract-shaped 503. The optional bearer token is sent, never
    logged."""

    name: str
    url: str
    bearer: str = ""
    timeout: int = DRAFT_TIMEOUT_SECONDS

    def complete(self, prompt: str) -> str:
        body = json.dumps({"prompt": prompt}).encode()
        headers = {"Content-Type": "application/json"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_HTTP_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            raise CaretError(
                503, "internal_error", f"upstream agent answered HTTP {exc.code}", retryable=True
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CaretError(
                503, "internal_error", "upstream agent unreachable", retryable=True
            ) from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CaretError(
                503, "internal_error", "upstream agent returned invalid JSON", retryable=True
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise CaretError(
                503, "internal_error", 'upstream agent response has no "text"', retryable=True
            )
        return text.strip()


@dataclass
class EchoAgent:
    """Deterministic stand-in — makes conformance checks free and hermetic."""

    name: str = "echo"

    def complete(self, prompt: str) -> str:
        return prompt.strip().splitlines()[-1][:4000]

    def draft(self, *, instruction: str, visible_text: str | None, app_hint: str | None) -> str:
        return f"[draft] {instruction}"

    def polish(self, transcript: str) -> str:
        return transcript.strip().capitalize()


# Default command per CLI agent preset. Each is the runtime's documented
# non-interactive interface in its most restricted documented mode.
# openclaw has NO entry on purpose: it was not available to verify, so
# selecting it without an explicit CARET_AGENT_COMMAND is a startup error,
# not a guess.
AGENT_PRESETS = {
    "hermes": 'hermes chat -q "{prompt}" -Q',
    "claude-code": "claude -p --permission-mode plan --no-session-persistence {prompt}",
    "codex": (
        "codex exec --sandbox read-only --skip-git-repo-check --ephemeral "
        "--output-last-message {out} {prompt}"
    ),
}


def hermes_agent(timeout: int = DRAFT_TIMEOUT_SECONDS) -> CommandAgent:
    # Stock public syntax only: `chat -q` is the documented non-interactive
    # single-query mode and `-Q` is the documented quiet mode for
    # programmatic use, so stdout is the answer and nothing else (Hermes
    # prints its session id on stderr, which this backend ignores).
    #
    # Deliberately no tool-restricting flag by default: the user's own
    # Hermes configuration decides what the agent can reach, and silently
    # overriding it would be a surprise. `-t/--toolsets` is the documented
    # way to narrow it — put it in CARET_AGENT_COMMAND if you want it.
    return CommandAgent(name="hermes", template=AGENT_PRESETS["hermes"], timeout=timeout)


# --------------------------------------------------------------- transcribers


def pcm16_to_wav(pcm: bytes, *, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw PCM16 in a WAV container.

    The contract carries headerless PCM because it is the cheapest thing a
    phone can produce, but essentially every STT tool wants a container.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audio.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)
        return path.read_bytes()


@dataclass
class CommandTranscriber:
    """STT as a command line. `{audio}` is a WAV file path; the transcript
    is stdout — or, when the template contains `{out_dir}`, the
    `audio.txt` file the tool writes into that directory (the shape of
    OpenWhisper's documented `--output_format txt --output_dir`)."""

    name: str
    template: str
    timeout: int = STT_TIMEOUT_SECONDS

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            path.write_bytes(pcm16_to_wav(pcm, sample_rate=sample_rate))
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            parts = shlex.split(self.template)
            if not any("{audio}" in part for part in parts):
                raise CaretError(
                    500,
                    "internal_error",
                    "transcriber command must contain {audio}",
                    retryable=False,
                )
            uses_out_dir = any("{out_dir}" in part for part in parts)
            argv = [
                part.replace("{audio}", str(path)).replace("{out_dir}", str(out_dir))
                for part in parts
            ]
            text = _run(argv, stdin_text=None, timeout=self.timeout, what="transcriber")
            if uses_out_dir:
                transcript = out_dir / "audio.txt"
                if not transcript.exists():
                    raise CaretError(
                        503,
                        "transcription_failed",
                        f"{self.name} wrote no transcript file",
                        retryable=True,
                    )
                text = transcript.read_text().strip()
            return text


# OpenWhisper — the open-source Whisper speech-recognition CLI
# (github.com/openai/whisper) — is the default local STT. The template is
# the tool's stock documented syntax; `--output_format txt --output_dir`
# is used because whisper's stdout carries progress and timestamped
# segments, while the .txt file is the plain transcript.
OPENWHISPER_EXECUTABLE = "whisper"
OPENWHISPER_DEFAULT_MODEL = "turbo"


def openwhisper_transcriber(
    model: str | None = None, timeout: int = STT_TIMEOUT_SECONDS
) -> CommandTranscriber:
    return CommandTranscriber(
        name="openwhisper",
        template=(
            f"{OPENWHISPER_EXECUTABLE} {{audio}} "
            f"--model {model or OPENWHISPER_DEFAULT_MODEL} "
            "--output_format txt --output_dir {out_dir}"
        ),
        timeout=timeout,
    )


@dataclass
class HttpTranscriber:
    """A hosted STT service behind a narrow contract: one POST of the WAV
    body per transcription, `{"text": "<transcript>"}` back. The optional
    bearer token is sent, never logged."""

    name: str
    url: str
    bearer: str = ""
    timeout: int = STT_TIMEOUT_SECONDS

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        wav = pcm16_to_wav(pcm, sample_rate=sample_rate)
        headers = {"Content-Type": "audio/wav"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        request = urllib.request.Request(self.url, data=wav, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_HTTP_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            raise CaretError(
                503,
                "transcription_failed",
                f"STT service answered HTTP {exc.code}",
                retryable=True,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CaretError(
                503, "transcription_failed", "STT service unreachable", retryable=True
            ) from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CaretError(
                503,
                "transcription_failed",
                "STT service returned invalid JSON",
                retryable=True,
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise CaretError(
                503,
                "transcription_failed",
                'STT service response has no "text"',
                retryable=True,
            )
        return text.strip()


@dataclass
class NullTranscriber:
    """No STT configured. Declares dictation unavailable rather than lying."""

    name: str = "none"
    available: bool = False

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        raise CaretError(
            503,
            "transcription_failed",
            "no transcriber configured on this backend",
            retryable=False,
        )


@dataclass
class EchoTranscriber:
    """Reports how much audio arrived — enough to prove the session flow."""

    name: str = "echo"

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        ms = int(len(pcm) / 2 / sample_rate * 1000)
        return f"transcribed {len(pcm)} bytes of audio ({ms} ms)"


# ------------------------------------------------------------------- imagine


@dataclass
class CommandImageGenerator:
    """Image generation as a command line: `{prompt}` in, `{out}` PNG out."""

    name: str
    template: str
    timeout: int = IMAGE_TIMEOUT_SECONDS

    def generate(self, prompt: str, *, aspect_ratio: str, quality: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "image.png"
            parts = shlex.split(self.template)
            argv = [
                part.replace("{prompt}", prompt)
                .replace("{out}", str(out))
                .replace("{aspect_ratio}", aspect_ratio)
                .replace("{quality}", quality)
                for part in parts
            ]
            _run(argv, stdin_text=None, timeout=self.timeout, what="image generator")
            if not out.exists() or out.stat().st_size == 0:
                raise CaretError(
                    503,
                    "image_generation_failed",
                    "image command produced no file",
                    retryable=True,
                )
            return out.read_bytes()


@dataclass
class FakeImageGenerator:
    """A 1x1 PNG. Lets the async/caching path be tested without a model."""

    name: str = "fake"

    _PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    def generate(self, prompt: str, *, aspect_ratio: str, quality: str) -> bytes:
        return self._PNG


# -------------------------------------------------------------------- wiring


def _config_error(message: str) -> CaretError:
    return CaretError(500, "internal_error", message, retryable=False)


def agent_from_env(env: dict[str, str] | None = None) -> object:
    """Pick the Ask adapter. Explicit configuration always wins."""
    env = os.environ if env is None else env
    preset = env.get("CARET_AGENT", "auto")
    command = env.get("CARET_AGENT_COMMAND")
    try:
        timeout = int(env.get("CARET_AGENT_TIMEOUT_S", str(DRAFT_TIMEOUT_SECONDS)))
    except ValueError:
        raise _config_error("CARET_AGENT_TIMEOUT_S must be an integer number of seconds")
    if command:
        check_command_safety(command)
        default_name = preset if preset not in ("", "auto") else "custom"
        return CommandAgent(
            name=env.get("CARET_AGENT_NAME", default_name), template=command, timeout=timeout
        )
    if preset == "echo":
        return EchoAgent()
    if preset == "custom-http":
        url = env.get("CARET_AGENT_HTTP_URL", "")
        if not url:
            raise _config_error(
                "CARET_AGENT=custom-http requires CARET_AGENT_HTTP_URL "
                "(the POST endpoint of your hosted agent)"
            )
        if urlparse(url).scheme not in ("http", "https"):
            raise _config_error(
                f"CARET_AGENT_HTTP_URL must be http:// or https://, got {url!r}"
            )
        return HttpAgent(
            name="custom-http",
            url=url,
            bearer=env.get("CARET_AGENT_HTTP_BEARER", ""),
            timeout=timeout,
        )
    if preset == "openclaw":
        raise _config_error(
            "the openclaw preset is not yet verified: no stable non-interactive "
            "OpenClaw interface could be checked, so this backend ships no "
            "default command for it. Set CARET_AGENT_COMMAND to a command line "
            "that reads a prompt ({prompt} or stdin) and prints the reply on "
            "stdout — see https://docs.typewithcaret.com/connect/openclaw/"
        )
    if preset in AGENT_PRESETS:
        return CommandAgent(name=preset, template=AGENT_PRESETS[preset], timeout=timeout)
    if preset == "auto":
        for candidate in ("hermes", "claude-code", "codex"):
            executable = shlex.split(AGENT_PRESETS[candidate])[0]
            if shutil.which(executable):
                return CommandAgent(
                    name=candidate, template=AGENT_PRESETS[candidate], timeout=timeout
                )
        return EchoAgent()
    raise _config_error(
        f"unknown CARET_AGENT: {preset!r}; one of: auto, "
        + ", ".join(sorted(AGENT_PRESETS))
        + ", openclaw, custom-http, echo"
    )


# Capability routing
# ------------------
# Every surface routes through the selected agent when — and only when —
# that agent adapter verifiably provides the capability:
#
#   Ask        always the agent. That is what an agent adapter is.
#   Cleanup    always the agent, as a constrained text-only cleanup request
#              (POLISH_FRAMING): fix the transcript, take no action. The
#              CLI presets run in their read-only modes, so "no action
#              tools" is enforced where the runtime can enforce it.
#   STT        the agent only if its adapter implements `transcribe()`.
#              NONE of the shipped presets does: no documented, stable
#              non-interactive audio-transcription interface could be
#              verified for Hermes, OpenClaw, Claude Code, Codex, or the
#              custom-http contract (which is text-JSON by definition).
#              So STT falls back to the local OpenWhisper preset by
#              default, or to the adapter you configure.
#   Imagine    the agent only if its adapter implements `generate()`.
#              Same finding: none of the shipped presets has a verified
#              image-output interface, so Imagine uses the image adapter
#              you configure, or stays honestly off.
#
# An operator integrating an agent that genuinely transcribes audio or
# renders images gives its adapter a `transcribe(pcm, *, sample_rate)` /
# `generate(prompt, *, aspect_ratio, quality)` method and the router
# prefers it automatically; nothing else changes.


def agent_supports_stt(agent: object) -> bool:
    return callable(getattr(agent, "transcribe", None))


def agent_supports_imagine(agent: object) -> bool:
    return callable(getattr(agent, "generate", None))


def transcriber_from_env(
    env: dict[str, str] | None = None, agent: object | None = None
) -> object:
    """Pick the STT adapter. Explicit configuration always wins; `auto`
    routes through the agent when it verifiably transcribes, then falls
    back to local OpenWhisper when its executable is present, and to
    honestly-off otherwise."""
    env = os.environ if env is None else env
    command = env.get("CARET_STT_COMMAND")
    if command:
        return CommandTranscriber(name=env.get("CARET_STT_NAME", "custom"), template=command)
    preset = env.get("CARET_STT", "auto")
    if preset == "agent" or (preset == "auto" and agent is not None and agent_supports_stt(agent)):
        if agent is None or not agent_supports_stt(agent):
            raise _config_error(
                "CARET_STT=agent but the selected agent adapter does not "
                "implement transcribe() — no shipped preset does; use "
                "openwhisper, http, or CARET_STT_COMMAND"
            )
        return agent
    if preset == "echo":
        return EchoTranscriber()
    if preset == "off":
        return NullTranscriber()
    if preset == "http":
        url = env.get("CARET_STT_HTTP_URL", "")
        if not url:
            raise _config_error(
                "CARET_STT=http requires CARET_STT_HTTP_URL "
                "(the POST endpoint of your STT service)"
            )
        return HttpTranscriber(
            name="http", url=url, bearer=env.get("CARET_STT_HTTP_BEARER", "")
        )
    if preset == "openwhisper":
        if not shutil.which(OPENWHISPER_EXECUTABLE):
            raise _config_error(
                f"CARET_STT=openwhisper but {OPENWHISPER_EXECUTABLE!r} is not on "
                "PATH — install OpenWhisper (e.g. `brew install openai-whisper`, "
                "see github.com/openai/whisper) or set CARET_STT_COMMAND / "
                "CARET_STT_HTTP_URL, or CARET_STT=off"
            )
        return openwhisper_transcriber(model=env.get("CARET_STT_MODEL"))
    if preset == "auto":
        if env.get("CARET_STT_HTTP_URL"):
            return HttpTranscriber(
                name="http",
                url=env["CARET_STT_HTTP_URL"],
                bearer=env.get("CARET_STT_HTTP_BEARER", ""),
            )
        if shutil.which(OPENWHISPER_EXECUTABLE):
            return openwhisper_transcriber(model=env.get("CARET_STT_MODEL"))
        return NullTranscriber()
    raise _config_error(
        f"unknown CARET_STT: {preset!r}; one of: auto, agent, openwhisper, http, echo, off"
    )


def image_generator_from_env(
    env: dict[str, str] | None = None, agent: object | None = None
) -> object | None:
    env = os.environ if env is None else env
    command = env.get("CARET_IMAGE_COMMAND")
    if command:
        return CommandImageGenerator(
            name=env.get("CARET_IMAGE_NAME", "custom"), template=command
        )
    if env.get("CARET_IMAGE") == "fake":
        return FakeImageGenerator()
    if agent is not None and agent_supports_imagine(agent):
        # Capability routing: an agent adapter that verifiably renders
        # images serves Imagine itself. None of the shipped presets does.
        return agent
    # Imagine is a capability extension: unconfigured means the surface
    # reports itself off and answers 404, not a fake success.
    return None
