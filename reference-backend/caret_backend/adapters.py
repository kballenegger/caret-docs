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

Presets
-------
`hermes`
    Used as the default when the `hermes` executable is on PATH. Runs
    `hermes chat -q <prompt> -Q`, which is stock documented public CLI
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

`echo`
    A dependency-free stand-in that reflects the prompt. The conformance
    checker and the test suite run against it, so you can verify contract
    conformance without spending a single model token.

Other agents
    Set `CARET_AGENT_COMMAND` to any command line. `{prompt}` is replaced
    with the prompt; if the template contains no `{prompt}`, the prompt is
    written to the process's stdin instead. Whatever the command prints on
    stdout is the answer. Example shapes are in the README.

    The tests here are hermetic and spend no model tokens, so be precise
    about what that buys you: the `echo` preset is exercised end to end,
    and the `hermes` preset has its command line asserted but not executed.
    A command line for any other agent is a configuration claim, not a
    tested one, and the README says so per agent.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from .errors import CaretError

DRAFT_TIMEOUT_SECONDS = 90
STT_TIMEOUT_SECONDS = 300
IMAGE_TIMEOUT_SECONDS = 300

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


@dataclass
class CommandAgent:
    """An agent that is a command line. The only adapter you need to write."""

    name: str
    template: str
    timeout: int = DRAFT_TIMEOUT_SECONDS

    def _argv(self, prompt: str) -> tuple[list[str], str | None]:
        parts = shlex.split(self.template)
        if any("{prompt}" in part for part in parts):
            return [part.replace("{prompt}", prompt) for part in parts], None
        return parts, prompt

    def complete(self, prompt: str) -> str:
        argv, stdin_text = self._argv(prompt)
        text = _run(argv, stdin_text=stdin_text, timeout=self.timeout, what="agent")
        if not text:
            raise CaretError(
                503,
                "internal_error",
                f"{self.name} returned an empty answer",
                retryable=True,
            )
        return text

    def draft(self, *, instruction: str, visible_text: str | None, app_hint: str | None) -> str:
        prompt = DRAFT_FRAMING.format(instruction=instruction)
        if visible_text:
            # Near-cursor text only. The keyboard cannot see the whole
            # conversation and this backend must not pretend otherwise.
            prompt += f"\n\nText near the cursor: {visible_text}"
        if app_hint:
            prompt += f"\n\nHost app (soft hint): {app_hint}"
        return self.complete(prompt)

    def polish(self, transcript: str) -> str:
        return self.complete(POLISH_FRAMING.format(transcript=transcript))


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
    return CommandAgent(
        name="hermes",
        template='hermes chat -q "{prompt}" -Q',
        timeout=timeout,
    )


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
    """STT as a command line. `{audio}` is a WAV file path; stdout is text."""

    name: str
    template: str
    timeout: int = STT_TIMEOUT_SECONDS

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            path.write_bytes(pcm16_to_wav(pcm, sample_rate=sample_rate))
            parts = shlex.split(self.template)
            if not any("{audio}" in part for part in parts):
                raise CaretError(
                    500,
                    "internal_error",
                    "transcriber command must contain {audio}",
                    retryable=False,
                )
            argv = [part.replace("{audio}", str(path)) for part in parts]
            return _run(argv, stdin_text=None, timeout=self.timeout, what="transcriber")


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


def agent_from_env(env: dict[str, str] | None = None) -> object:
    """Pick the agent adapter. Explicit configuration always wins."""
    env = os.environ if env is None else env
    command = env.get("CARET_AGENT_COMMAND")
    if command:
        return CommandAgent(name=env.get("CARET_AGENT_NAME", "custom"), template=command)
    preset = env.get("CARET_AGENT", "auto")
    if preset == "echo":
        return EchoAgent()
    if preset == "hermes" or (preset == "auto" and shutil.which("hermes")):
        return hermes_agent()
    if preset == "auto":
        return EchoAgent()
    raise CaretError(500, "internal_error", f"unknown CARET_AGENT: {preset}")


def transcriber_from_env(env: dict[str, str] | None = None) -> object:
    env = os.environ if env is None else env
    command = env.get("CARET_STT_COMMAND")
    if command:
        return CommandTranscriber(name=env.get("CARET_STT_NAME", "custom"), template=command)
    if env.get("CARET_STT") == "echo":
        return EchoTranscriber()
    return NullTranscriber()


def image_generator_from_env(env: dict[str, str] | None = None) -> object | None:
    env = os.environ if env is None else env
    command = env.get("CARET_IMAGE_COMMAND")
    if command:
        return CommandImageGenerator(
            name=env.get("CARET_IMAGE_NAME", "custom"), template=command
        )
    if env.get("CARET_IMAGE") == "fake":
        return FakeImageGenerator()
    # Imagine is a capability extension: unconfigured means the surface
    # reports itself off and answers 404, not a fake success.
    return None
