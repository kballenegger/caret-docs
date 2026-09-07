"""Providers: where the reference backend meets a real model, or does not.

A lane is one string. The grammar is the same for every capability, and
the same as the Go implementation's:

    ""  "none"  "off"      the capability is off; the route is not served
    "loopback"             the built-in deterministic provider
    "command:<argv>"       run a program; see each lane for its contract
    "https://host/path"    POST JSON to a service

The loopback providers are not models. They exist so the lifecycle,
vocabulary routing, silence handling, and image verification are all
exercisable on a laptop with no API key, no network, and no GPU. A
loopback transcript is a fixed word list paced by how much non-silent
audio arrived; a loopback image is a gradient. Nothing here pretends
otherwise, and no deployment should ship them to a user.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

from .protocol import AUDIO_SAMPLE_RATE_HZ

LANE_OFF = "off"
LANE_LOOPBACK = "loopback"
LANE_COMMAND = "command"
LANE_HTTP = "http"

#: 20 ms at 16 kHz mono PCM16.
LOOPBACK_WINDOW_BYTES = 640
LOOPBACK_MS_PER_WORD = 400

LOOPBACK_FILLER = [
    "let's", "push", "the", "review", "to", "thursday", "afternoon",
    "and", "tell", "the", "team", "before", "standup",
]

#: Provider stderr is an operator diagnostic. It never reaches the user,
#: and only this much of it reaches the log.
STDERR_LOG_CHARS = 400


class LaneError(Exception):
    """A provider failed. The session maps this to transcription_failed
    or generation_failed; the detail stays in the log."""


class NoStreaming(Exception):
    """This recognizer has no streaming interface. Not a failure: the
    session buffers and transcribes once at finalize, and reports
    `stt_route: "fallback"`."""


def parse_lane(spec: str) -> tuple[str, str]:
    """`(kind, argument)` for a lane string. A bad spec is an error at
    startup rather than a surprise mid-dictation."""
    spec = (spec or "").strip()
    if spec in ("", "none", "off"):
        return LANE_OFF, ""
    if spec == "loopback":
        return LANE_LOOPBACK, ""
    if spec.startswith("command:"):
        argv = spec[len("command:"):].strip()
        if not argv:
            raise ValueError("command: lane needs a program to run")
        return LANE_COMMAND, argv
    if spec.startswith("http://") or spec.startswith("https://"):
        return LANE_HTTP, spec
    raise ValueError(
        f"unrecognized lane {spec!r}: use loopback, command:<argv>, an https URL, or none"
    )


# ------------------------------------------------------------------ audio


def encode_wav(pcm: bytes, sample_rate: int = AUDIO_SAMPLE_RATE_HZ) -> bytes:
    """A 44-byte canonical WAV header around raw PCM16 mono, so a command
    lane can be an ordinary transcription program that reads a file."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def loopback_words(pcm: bytes, vocabulary: list[str]) -> str:
    """The whole of loopback recognition: count 20 ms windows containing a
    non-zero sample, spend 400 ms of them per word, spell the client's
    vocabulary first and then cycle filler.

    Digital silence yields the empty string, which is what makes
    `no_speech_detected` testable without a recognizer.
    """
    voiced = 0
    for offset in range(0, len(pcm) - 1, LOOPBACK_WINDOW_BYTES):
        window = pcm[offset:offset + LOOPBACK_WINDOW_BYTES]
        if any(window[i] or window[i + 1] for i in range(0, len(window) - 1, 2)):
            voiced += 1
    count = voiced * 20 // LOOPBACK_MS_PER_WORD
    if count <= 0:
        return ""
    words = []
    for i in range(count):
        if i < len(vocabulary):
            words.append(vocabulary[i])
        else:
            words.append(LOOPBACK_FILLER[(i - len(vocabulary)) % len(LOOPBACK_FILLER)])
    return " ".join(words)


def encode_png(width: int, height: int, pixels: bytes) -> bytes:
    """A minimal RGBA PNG. `pixels` is width*height*4 bytes, row-major."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack("!I", len(payload)) + body + struct.pack("!I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter type 0
        raw.extend(pixels[y * stride:(y + 1) * stride])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- helpers


def _run_command(argv: list[str], stdin: bytes = b"", env_extra: dict | None = None,
                 timeout: float = 120.0, text_output: bool = True):
    env = dict(os.environ)
    env.update(env_extra or {})
    try:
        completed = subprocess.run(
            argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=timeout, check=False,
        )
    except OSError as exc:
        raise LaneError(f"cannot run {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaneError(f"{argv[0]!r} did not finish within {timeout:.0f}s") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:STDERR_LOG_CHARS]
        raise LaneError(f"{argv[0]!r} exited {completed.returncode}: {detail}")
    if text_output:
        return completed.stdout.decode("utf-8", "replace").strip()
    return completed.stdout


def _post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    token = os.environ.get("CARET_LANE_TOKEN", "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — operator-supplied URL
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LaneError(f"{url} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise LaneError(f"{url}: {exc}") from exc


def _substitute(argv: list[str], replacements: dict) -> list[str]:
    out = []
    for arg in argv:
        for placeholder, value in replacements.items():
            arg = arg.replace(placeholder, value)
        out.append(arg)
    return out


# -------------------------------------------------------------------- STT


@dataclass
class STTOptions:
    vocabulary: list[str]
    language_hint: str = ""
    sample_rate_hz: int = AUDIO_SAMPLE_RATE_HZ


class LoopbackStream:
    """A streaming recognizer that needs no model: it re-derives the whole
    transcript from everything heard so far, which is exactly the
    cumulative semantics §5 requires of `partial`."""

    def __init__(self, options: STTOptions, on_partial) -> None:
        self.options = options
        self.on_partial = on_partial
        self.buffer = bytearray()

    def write(self, pcm: bytes) -> None:
        self.buffer.extend(pcm)
        text = loopback_words(bytes(self.buffer), self.options.vocabulary)
        if text and self.on_partial:
            self.on_partial(text)

    def finish(self) -> str:
        return loopback_words(bytes(self.buffer), self.options.vocabulary)

    def abort(self) -> None:
        self.buffer = bytearray()


class LoopbackSTT:
    name = "loopback"
    streaming = True
    uses_vocabulary = True

    def open(self, options: STTOptions, on_partial):
        return LoopbackStream(options, on_partial)

    def transcribe(self, pcm: bytes, options: STTOptions) -> str:
        return loopback_words(pcm, options.vocabulary)


class CommandSTT:
    """`command:whisper --file {audio}`.

    The audio is written to a temporary WAV, `{audio}` is replaced with
    its path, and stdout is the transcript. The temporary file is removed
    before the result is returned, whatever happens.
    """

    streaming = False
    uses_vocabulary = True

    def __init__(self, argv: str, timeout: float = 120.0) -> None:
        self.argv = shlex.split(argv)
        self.name = f"command:{self.argv[0]}"
        self.timeout = timeout

    def open(self, options: STTOptions, on_partial):
        raise NoStreaming()

    def transcribe(self, pcm: bytes, options: STTOptions) -> str:
        with tempfile.TemporaryDirectory(prefix="caret-audio-") as directory:
            path = Path(directory) / "audio.wav"
            path.write_bytes(encode_wav(pcm, options.sample_rate_hz))
            argv = _substitute(self.argv, {"{audio}": str(path)})
            return _run_command(argv, env_extra={
                "CARET_AUDIO_PATH": str(path),
                "CARET_VOCABULARY": "\n".join(options.vocabulary),
                "CARET_LANGUAGE_HINT": options.language_hint,
            }, timeout=self.timeout)


class HTTPSTT:
    """POST `{codec, sample_rate_hz, channels, audio_base64, vocabulary,
    language_hint}`, read `{"text": "…"}`."""

    streaming = False
    uses_vocabulary = True

    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url
        self.name = f"http:{url}"
        self.timeout = timeout

    def open(self, options: STTOptions, on_partial):
        raise NoStreaming()

    def transcribe(self, pcm: bytes, options: STTOptions) -> str:
        answer = _post_json(self.url, {
            "codec": "pcm16",
            "sample_rate_hz": options.sample_rate_hz,
            "channels": 1,
            "audio_base64": base64.b64encode(pcm).decode("ascii"),
            "vocabulary": options.vocabulary,
            "language_hint": options.language_hint,
        }, self.timeout)
        return str(answer.get("text", "")).strip()


# ------------------------------------------------------------------ agent


class LoopbackAgent:
    name = "loopback"
    uses_vocabulary = False

    def respond(self, prompt: str, visible_text: str, vocabulary: list[str]) -> str:
        prompt = prompt.strip()
        return prompt if prompt else "(nothing to say)"


class CommandAgent:
    """The instruction arrives on stdin; the message is stdout."""

    uses_vocabulary = True

    def __init__(self, argv: str, timeout: float = 120.0) -> None:
        self.argv = shlex.split(argv)
        self.name = f"command:{self.argv[0]}"
        self.timeout = timeout

    def respond(self, prompt: str, visible_text: str, vocabulary: list[str]) -> str:
        return _run_command(self.argv, stdin=prompt.encode("utf-8"), env_extra={
            "CARET_VISIBLE_TEXT": visible_text,
            "CARET_VOCABULARY": "\n".join(vocabulary),
        }, timeout=self.timeout)


class HTTPAgent:
    uses_vocabulary = True

    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url
        self.name = f"http:{url}"
        self.timeout = timeout

    def respond(self, prompt: str, visible_text: str, vocabulary: list[str]) -> str:
        answer = _post_json(self.url, {
            "prompt": prompt, "visible_text": visible_text, "vocabulary": vocabulary,
        }, self.timeout)
        return str(answer.get("text", "")).strip()


# ------------------------------------------------------------------ image


class LoopbackImage:
    name = "loopback"

    def generate(self, prompt: str, aspect_ratio: str, quality: str) -> tuple[str, bytes]:
        width, height = 512, 512
        if aspect_ratio == "3:2":
            width, height = 600, 400
        elif aspect_ratio == "2:3":
            width, height = 400, 600
        if quality == "low":
            width, height = width // 4, height // 4
        elif quality == "medium":
            width, height = width // 2, height // 2

        seed = 0
        for char in prompt:
            seed = (seed * 31 + ord(char)) & 0xFFFF
        pixels = bytearray()
        for y in range(height):
            green = (y * 255 // height) % 256
            blue = (seed // 256) % 256
            for x in range(width):
                pixels.extend((((x * 255 // width) + seed) % 256, green, blue, 255))
        return "image/png", encode_png(width, height, bytes(pixels))


class CommandImage:
    """The prompt arrives on stdin; the image bytes are stdout."""

    def __init__(self, argv: str, timeout: float = 300.0) -> None:
        self.argv = shlex.split(argv)
        self.name = f"command:{self.argv[0]}"
        self.timeout = timeout

    def generate(self, prompt: str, aspect_ratio: str, quality: str) -> tuple[str, bytes]:
        data = _run_command(self.argv, stdin=prompt.encode("utf-8"), env_extra={
            "CARET_ASPECT_RATIO": aspect_ratio, "CARET_QUALITY": quality,
        }, timeout=self.timeout, text_output=False)
        if not data:
            raise LaneError("the image command produced no bytes")
        return sniff_image_mime(data), data


class HTTPImage:
    def __init__(self, url: str, timeout: float = 300.0) -> None:
        self.url = url
        self.name = f"http:{url}"
        self.timeout = timeout

    def generate(self, prompt: str, aspect_ratio: str, quality: str) -> tuple[str, bytes]:
        answer = _post_json(self.url, {
            "prompt": prompt, "aspect_ratio": aspect_ratio, "quality": quality,
        }, self.timeout)
        encoded = str(answer.get("data_base64", ""))
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LaneError("the image service did not return valid base64") from exc
        if not data:
            raise LaneError("the image service returned no bytes")
        return str(answer.get("mime_type") or sniff_image_mime(data)), data


def sniff_image_mime(data: bytes) -> str:
    """§6 requires the declared MIME type to describe the delivered
    bytes, so it is read from the bytes rather than assumed."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


# ---------------------------------------------------------------- cleanup


class LoopbackCleanup:
    """Capitalize the first letter, add a full stop. This is emphatically
    not the `caret-cleanup/1` pass: it makes the polish step observable
    without a language model, and nothing more."""

    name = "loopback"

    def polish(self, framing: str, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return ""
        text = text[0].upper() + text[1:]
        if text[-1] not in ".!?":
            text += "."
        return text


class CommandCleanup:
    def __init__(self, argv: str, timeout: float = 120.0) -> None:
        self.argv = shlex.split(argv)
        self.name = f"command:{self.argv[0]}"
        self.timeout = timeout

    def polish(self, framing: str, transcript: str) -> str:
        from .cleanup import wrap_transcript
        prompt = f"{framing}\n\n{wrap_transcript(transcript)}"
        return _run_command(self.argv, stdin=prompt.encode("utf-8"), timeout=self.timeout)


class HTTPCleanup:
    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.url = url
        self.name = f"http:{url}"
        self.timeout = timeout

    def polish(self, framing: str, transcript: str) -> str:
        from .cleanup import wrap_transcript
        answer = _post_json(self.url, {
            "system": framing,
            "transcript": transcript,
            "prompt": f"{framing}\n\n{wrap_transcript(transcript)}",
        }, self.timeout)
        return str(answer.get("text", "")).strip()


# --------------------------------------------------------------- resolving


def resolve_stt(spec: str, timeout: float = 120.0):
    kind, argument = parse_lane(spec)
    return {
        LANE_OFF: lambda: None,
        LANE_LOOPBACK: LoopbackSTT,
        LANE_COMMAND: lambda: CommandSTT(argument, timeout),
        LANE_HTTP: lambda: HTTPSTT(argument, timeout),
    }[kind]()


def resolve_agent(spec: str, timeout: float = 120.0):
    kind, argument = parse_lane(spec)
    return {
        LANE_OFF: lambda: None,
        LANE_LOOPBACK: LoopbackAgent,
        LANE_COMMAND: lambda: CommandAgent(argument, timeout),
        LANE_HTTP: lambda: HTTPAgent(argument, timeout),
    }[kind]()


def resolve_image(spec: str, timeout: float = 300.0):
    kind, argument = parse_lane(spec)
    return {
        LANE_OFF: lambda: None,
        LANE_LOOPBACK: LoopbackImage,
        LANE_COMMAND: lambda: CommandImage(argument, timeout),
        LANE_HTTP: lambda: HTTPImage(argument, timeout),
    }[kind]()


def resolve_cleanup(spec: str, timeout: float = 120.0):
    kind, argument = parse_lane(spec)
    return {
        LANE_OFF: lambda: None,
        LANE_LOOPBACK: LoopbackCleanup,
        LANE_COMMAND: lambda: CommandCleanup(argument, timeout),
        LANE_HTTP: lambda: HTTPCleanup(argument, timeout),
    }[kind]()
