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

`grokbot`
    A first-party agent backend, not a special case of `custom-http`.
    GrokBot hosts this reference backend inside its own system and serves
    the agent surfaces natively from its own LLM, over one endpoint
    (`CARET_GROKBOT_URL`) that discriminates on a `task` field:

        {"task": "ask",     "prompt"}                  → {"text"}
        {"task": "cleanup", "prompt"}                  → {"text"}
        {"task": "imagine", "prompt", "aspect_ratio",
                            "quality"}                 → {"image_base64"}

    Ask and the constrained transcript cleanup are always GrokBot's own
    LLM. Imagine is served natively by GrokBot too, but only when its
    image capability is enabled (`CARET_GROKBOT_IMAGE=on`) — that is the
    one shipped preset whose adapter can carry the Imagine capability, so
    capability routing sends Imagine to the agent instead of leaving it
    off. STT is not part of the contract: no stable non-interactive
    GrokBot transcription interface was verified, so dictation stays on
    the backend's own STT lane.

    Nothing here is reverse-engineered from GrokBot internals. This is a
    public configuration contract that a GrokBot deployment implements on
    its side; the backend half is covered by the hermetic suite, and no
    live GrokBot run has been performed from this repository.

`custom-http`
    The generic fallback for any *other* hosted agent: one
    `POST {"prompt"} → {"text"}` per call to `CARET_AGENT_HTTP_URL`, with
    an optional bearer token that is sent and never logged. Text-only by
    definition — neither STT nor Imagine routes through it.

`off`
    No Ask adapter. Ask is optional in caret/v2: a backend with no agent
    reports `ask: false` and answers 404 rather than pretending with a
    stand-in.

`echo`
    A dependency-free stand-in that reflects the prompt. The conformance
    checker and the test suite run against it, so you can verify contract
    conformance without spending a single model token. Explicit only —
    never auto-selected, because a stand-in that looks like a working
    agent is exactly the dishonesty this backend avoids.

`auto` (the default)
    The first of hermes, claude-code, codex whose executable is on PATH;
    otherwise `off`.

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

from . import cleanup
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

# Cleanup framing is not written here. It is the canonical `caret-cleanup`
# spec under `spec/cleanup/v1/`, loaded and composed by `cleanup.py`: one
# public source of wording, so this backend and any other implementation
# can be checked against the same bytes rather than against a paraphrase.
# `POLISH_FRAMING` is the composed system prompt for the default glossary;
# a deployment with its own vocabulary gets its own framing from
# `cleanup.framing_from_env()`, which `agent_from_env` attaches below.
#
# The transcript is NOT interpolated into the framing. It travels after
# it, inside the `<transcript>` … `</transcript>` envelope the prompt
# declares inert data (`cleanup.build_polish_prompt`). That is what makes
# a transcript that reads as an instruction stay text to format — and it
# also means a transcript containing braces or tags cannot disturb the
# framing, because there is no format string left to disturb.
POLISH_FRAMING = cleanup.POLISH_FRAMING
build_polish_prompt = cleanup.build_polish_prompt


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
            "ask_timeout" if what == "agent" else "transcription_failed",
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

    #: The composed cleanup system prompt this adapter sends. A class
    #: attribute so every adapter has a sane default; `agent_from_env`
    #: overwrites it per instance when the deployment configures its own
    #: glossary.
    cleanup_framing: str = POLISH_FRAMING

    def draft(self, *, instruction: str, visible_text: str | None, app_hint: str | None) -> str:
        return self.complete(build_draft_prompt(instruction, visible_text, app_hint))

    def polish(self, transcript: str) -> str:
        return self.complete(build_polish_prompt(transcript, self.cleanup_framing))


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


def post_json(
    url: str,
    payload: dict,
    *,
    bearer: str = "",
    timeout: int,
    what: str,
) -> dict:
    """One JSON POST, with every failure shaped like the caret/v2 envelope.

    Non-200, unreachable, timeout and non-JSON all become a retryable 503
    naming `what` — the caller never sees a urllib exception. The bearer
    token is sent and never logged."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_HTTP_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise CaretError(
            503, "internal_error", f"{what} answered HTTP {exc.code}", retryable=True
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CaretError(503, "internal_error", f"{what} unreachable", retryable=True) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise CaretError(
            503, "internal_error", f"{what} returned invalid JSON", retryable=True
        ) from exc
    if not isinstance(payload, dict):
        raise CaretError(
            503, "internal_error", f"{what} returned a non-object body", retryable=True
        )
    return payload


def _require_text(payload: dict, what: str) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise CaretError(
            503, "internal_error", f'{what} response has no "text"', retryable=True
        )
    return text.strip()


@dataclass
class HttpAgent(_DraftPolishMixin):
    """The generic hosted-agent fallback (`custom-http`) — any agent you can
    put behind one narrow JSON contract. One POST per call:

        request   {"prompt": "<framed prompt>"}
        response  {"text": "<the answer>"}          (HTTP 200)

    Anything else — non-200, unreachable, non-JSON, missing/empty `text` —
    is a contract-shaped 503. The optional bearer token is sent, never
    logged.

    Text only, by definition: this contract carries no audio and no image
    bytes, so a `custom-http` backend routes neither dictation nor Imagine
    through the agent. A first-party backend that does more than text gets
    its own adapter — see `GrokBotAgent` — rather than overloading this
    one."""

    name: str
    url: str
    bearer: str = ""
    timeout: int = DRAFT_TIMEOUT_SECONDS

    def complete(self, prompt: str) -> str:
        payload = post_json(
            self.url,
            {"prompt": prompt},
            bearer=self.bearer,
            timeout=self.timeout,
            what="upstream agent",
        )
        return _require_text(payload, "upstream agent")


# ------------------------------------------------------------------- grokbot
#
# GrokBot is a first-party agent backend, not a `custom-http` deployment.
# The difference is not the transport — it is the capability surface. A
# GrokBot deployment hosts this reference backend inside its own system and
# serves the agent surfaces from its own LLM, so:
#
#   * Ask is GrokBot's LLM, natively;
#   * cleanup is the same LLM under the same constrained framing this
#     backend applies to every agent (text in, text out, no actions);
#   * Imagine is GrokBot's own image capability, natively — when the
#     operator has enabled it (`CARET_GROKBOT_IMAGE=on`).
#
# One endpoint carries all three, discriminated by `task`, so a GrokBot
# deployment exposes a single route rather than three. `task` is explicit
# rather than inferred from the prompt: the GrokBot side needs to pick its
# no-tools path for cleanup deterministically, not by reading prose.
#
# What is NOT claimed here: nothing in this adapter is derived from GrokBot
# internals, and no live GrokBot deployment has been exercised from this
# repository. This is a public configuration contract — a GrokBot operator
# implements the endpoint on their side, and the hermetic suite covers this
# side of it against a stub. The Connect matrix labels it accordingly.
#
# STT is deliberately absent: no stable non-interactive GrokBot
# transcription interface was verified, so dictation stays on the backend's
# own STT lane, exactly as it does for every other preset.

GROKBOT_TASK_ASK = "ask"
GROKBOT_TASK_CLEANUP = "cleanup"
GROKBOT_TASK_IMAGINE = "imagine"


@dataclass
class GrokBotAgent(_DraftPolishMixin):
    """GrokBot serving Ask and cleanup from its own LLM.

        request   {"task": "ask"|"cleanup", "prompt": "<framed prompt>"}
        response  {"text": "<the answer>"}          (HTTP 200)

    Failures are contract-shaped 503s naming GrokBot. The bearer token, if
    configured, is sent and never logged."""

    url: str
    bearer: str = ""
    timeout: int = DRAFT_TIMEOUT_SECONDS
    image_timeout: int = IMAGE_TIMEOUT_SECONDS
    name: str = "grokbot"

    def _text(self, task: str, prompt: str) -> str:
        payload = post_json(
            self.url,
            {"task": task, "prompt": prompt},
            bearer=self.bearer,
            timeout=self.timeout,
            what="GrokBot",
        )
        return _require_text(payload, "GrokBot")

    def complete(self, prompt: str) -> str:
        return self._text(GROKBOT_TASK_ASK, prompt)

    def polish(self, transcript: str) -> str:
        # Same constrained framing every other adapter gets — the canonical
        # `caret-cleanup` prompt, with the transcript after it inside the
        # inert-data envelope — plus the explicit task so the GrokBot side
        # can select its no-tools path.
        return self._text(
            GROKBOT_TASK_CLEANUP, build_polish_prompt(transcript, self.cleanup_framing)
        )


@dataclass
class GrokBotImagingAgent(GrokBotAgent):
    """GrokBot with its image capability enabled.

        request   {"task": "imagine", "prompt", "aspect_ratio", "quality"}
        response  {"image_base64": "<base64 PNG>"}  (HTTP 200)

    Defining `generate` is what makes capability routing send Imagine to
    the agent (see `agent_supports_imagine`), so this class exists only
    when the operator opted in with `CARET_GROKBOT_IMAGE=on`. With images
    off, `GrokBotAgent` has no `generate` and Imagine reports honestly off
    unless a separate image provider is configured."""

    def generate(self, prompt: str, *, aspect_ratio: str, quality: str) -> bytes:
        payload = post_json(
            self.url,
            {
                "task": GROKBOT_TASK_IMAGINE,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "quality": quality,
            },
            bearer=self.bearer,
            timeout=self.image_timeout,
            what="GrokBot",
        )
        encoded = payload.get("image_base64")
        if not isinstance(encoded, str) or not encoded.strip():
            raise CaretError(
                503, "internal_error", 'GrokBot response has no "image_base64"', retryable=True
            )
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:  # binascii.Error subclasses ValueError
            raise CaretError(
                503, "internal_error", "GrokBot returned invalid base64", retryable=True
            ) from exc
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CaretError(
                503, "internal_error", "GrokBot returned a non-PNG image", retryable=True
            )
        return image


@dataclass
class NullAgent:
    """No Ask adapter configured.

    Ask is optional in caret/v2, so "no agent" is a legitimate, honest
    configuration — a dictate-only backend. What is not legitimate is
    pretending: this agent never answers, the backend reports
    `ask: false`, and `/v2/ask` is a 404. Anything that reaches these
    methods anyway is a bug in the caller, and says so."""

    name: str = "off"

    def _refuse(self) -> CaretError:
        return CaretError(
            404,
            "not_found",
            "this backend has no agent configured: Ask is off. Set CARET_AGENT "
            "to the runtime you run — see https://docs.typewithcaret.com/connect/",
            retryable=False,
        )

    def complete(self, prompt: str) -> str:
        raise self._refuse()

    def draft(self, *, instruction: str, visible_text: str | None, app_hint: str | None) -> str:
        raise self._refuse()

    def polish(self, transcript: str) -> str:
        raise self._refuse()


@dataclass
class EchoAgent:
    """Deterministic stand-in — makes conformance checks free and hermetic.

    Selectable only by asking for it (`CARET_AGENT=echo`). `auto` will
    never fall back to it: a stand-in that answers like a working agent is
    the one failure mode this backend refuses to ship. `auto` with nothing
    installed resolves to `NullAgent` and says Ask is off."""

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
    """Pick the Ask adapter, and give it this deployment's cleanup framing.

    The framing is resolved once, here, so a bad glossary file is a
    startup error the operator sees on `--check` rather than a surprise
    during someone's dictation.
    """
    env = os.environ if env is None else env
    agent = _select_agent(env)
    if isinstance(agent, _DraftPolishMixin):
        try:
            agent.cleanup_framing = cleanup.framing_from_env(env)
        except cleanup.SpecError as exc:
            raise _config_error(f"transcript cleanup prompt is unusable: {exc}")
    return agent


def _select_agent(env: dict[str, str]) -> object:
    """Pick the Ask adapter. Explicit configuration always wins."""
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
    if preset in ("off", "none"):
        return NullAgent()
    if preset == "grokbot":
        url = env.get("CARET_GROKBOT_URL", "")
        if not url:
            raise _config_error(
                "CARET_AGENT=grokbot requires CARET_GROKBOT_URL (the endpoint "
                "your GrokBot deployment exposes to the backend it hosts) — "
                "see https://docs.typewithcaret.com/connect/grokbot/"
            )
        if urlparse(url).scheme not in ("http", "https"):
            raise _config_error(
                f"CARET_GROKBOT_URL must be http:// or https://, got {url!r}"
            )
        images = env.get("CARET_GROKBOT_IMAGE", "off").strip().lower()
        if images not in ("on", "off"):
            raise _config_error(
                f"CARET_GROKBOT_IMAGE must be 'on' or 'off', got {images!r}: "
                "set it 'on' only if this GrokBot deployment actually serves "
                "the imagine task, so Imagine is not advertised falsely"
            )
        factory = GrokBotImagingAgent if images == "on" else GrokBotAgent
        return factory(
            url=url,
            bearer=env.get("CARET_GROKBOT_BEARER", ""),
            timeout=timeout,
        )
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
        # Nothing installed: Ask is off, and the backend says so. Falling
        # back to EchoAgent here would produce a backend that looks healthy
        # and answers every draft with a stand-in — the operator would find
        # out from their phone, not from health.
        return NullAgent()
    raise _config_error(
        f"unknown CARET_AGENT: {preset!r}; one of: auto, "
        + ", ".join(sorted(AGENT_PRESETS))
        + ", grokbot, openclaw, custom-http, echo, off"
    )


# Capability routing
# ------------------
# Every operation routes through the selected agent when — and only when —
# that agent adapter verifiably provides the capability:
#
#   Ask        the agent, when there is one. Ask is OPTIONAL in caret/v2:
#              `CARET_AGENT=off` (or `auto` with nothing installed) is a
#              valid dictate-only backend that reports `ask: false`
#              and answers /v2/ask with 404 rather than faking it.
#   Cleanup    the agent, as a constrained text-only cleanup request
#              (POLISH_FRAMING): fix the transcript, take no action. The
#              CLI presets run in their read-only modes, so "no action
#              tools" is enforced where the runtime can enforce it. With
#              no agent, cleanup is off and Dictate returns the raw
#              transcript (or, for text input, the text unchanged) —
#              still a complete, valid backend.
#   STT        the agent only if its adapter implements `transcribe()`.
#              NONE of the shipped presets does: no documented, stable
#              non-interactive audio-transcription interface could be
#              verified for Hermes, OpenClaw, Claude Code, Codex, GrokBot,
#              or the custom-http contract (which is text-JSON by
#              definition). So STT resolves to the local OpenWhisper
#              preset by default, or to the adapter you configure — and it
#              is MANDATORY: dictation is the one surface every valid
#              Caret backend must serve, so a backend with no working STT
#              adapter is not ready and reports itself that way.
#   Imagine    the agent only if its adapter implements `generate()`.
#              One shipped preset can: `grokbot` with
#              `CARET_GROKBOT_IMAGE=on` serves Imagine from GrokBot's own
#              image capability. Every other preset has no verified
#              image-output interface, so Imagine uses the image adapter
#              you configure, or stays honestly off. Imagine is OPTIONAL.
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
    back to local OpenWhisper when its executable is present.

    Dictation is mandatory in caret/v2, so `auto` finding nothing is not a
    working configuration — it returns `NullTranscriber`, which the server
    reports as a `not_ready` backend with a `no_stt_adapter` blocker, and
    which `--check` exits non-zero on. This function does not raise for it:
    the operator gets a running backend that tells them exactly what is
    missing, rather than a stack trace. `CARET_STT=off` is the same state,
    chosen deliberately — still not ready, still says so."""
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
        # images serves Imagine itself. Of the shipped presets, only
        # `grokbot` with CARET_GROKBOT_IMAGE=on does.
        return agent
    # Imagine is a capability extension: unconfigured means the surface
    # reports itself off and answers 404, not a fake success.
    return None
