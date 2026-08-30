"""Transcript cleanup — the prompt, the envelope, and the glossary.

Cleanup is the one place a Caret backend puts the user's own words in
front of a language model and asks it to give them back. Everything in
this file exists to make that round trip lossless and boring: the model
formats what it was given, and does nothing else.

The wording is not written here. It lives in the repository's canonical
spec directory, `spec/cleanup/v1/`, as data:

    prompt.md       the system prompt, verbatim
    glossary.json   the default glossary (public vocabulary only)
    composed.txt    prompt.md + the default glossary section, composed
    manifest.json   sha256 of each of the three, plus a short digest

`composed.txt` is the anti-drift artifact. Any implementation — this
backend, a hosted service, a port in another language — composes its
system prompt from `prompt.md` and a glossary and can assert the result
is byte-for-byte `composed.txt` for the default glossary. That is a test
two codebases can share without either importing the other, and the
`digest` in `manifest.json` is the short name for "the same spec".

The three rules the rest of this file implements:

1.  The transcript is data. It is wrapped in `<transcript>` …
    `</transcript>` and the prompt declares that region inert. A naked
    transcript that happens to read as an imperative ("check the deploy
    and tell me if it's green") looks like a request; the same words
    inside the envelope, under a prompt that says the envelope holds
    text to format, look like text to format. The envelope never alters
    the transcript — not even to escape a literal closing tag, because
    meaning preservation outranks tidiness.

2.  The glossary biases spelling, in context, and nothing else. It is a
    list of canonical spellings with the ways speech-to-text tends to
    mangle them. It is never a find-and-replace: "carrot" in a sentence
    about vegetables stays a carrot.

3.  Cleanup is best-effort. Every failure path returns the raw
    transcript. A dictation that arrives unpolished is a small
    disappointment; a dictation that arrives as an error message, or as
    an answer to itself, is a lost thought.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SPEC_ID = "caret-cleanup"
SPEC_VERSION = 1
SPEC_NAME = f"{SPEC_ID}/{SPEC_VERSION}"

#: Files covered by `manifest.json`, in digest order.
HASHED_FILES = ("prompt.md", "glossary.json", "composed.txt")

# parents[3]: this backend was archived under legacy/ in the V4 cutover;
# the spec it consumes stayed active at the repository root.
DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[3] / "spec" / "cleanup" / "v1"

TRANSCRIPT_OPEN_TAG = "<transcript>"
TRANSCRIPT_CLOSE_TAG = "</transcript>"

GLOSSARY_SECTION_HEADER = (
    "Glossary. Canonical spellings for terms this speaker uses. Apply an "
    "entry only when the surrounding words clearly refer to that term. "
    "Never replace an ordinary word that merely sounds like one, and never "
    "insert a term the speaker did not say."
)


class SpecError(Exception):
    """The cleanup spec is missing, unreadable, or does not match its
    manifest. Raised at configuration time, never mid-dictation."""


# ----------------------------------------------------------------- loading


def spec_dir(env: dict[str, str] | None = None) -> Path:
    """Where the canonical spec lives. `CARET_CLEANUP_SPEC_DIR` points a
    deployment at its own vendored copy (a container image that ships
    only the backend package, say)."""
    env = os.environ if env is None else env
    override = env.get("CARET_CLEANUP_SPEC_DIR", "").strip()
    return Path(override).expanduser() if override else DEFAULT_SPEC_DIR


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read {path.name} from {path.parent}: {exc}") from exc


def load_prompt(directory: Path | None = None) -> str:
    """The system prompt, with no glossary appended."""
    return _read_text((directory or spec_dir()) / "prompt.md").strip()


def load_composed(directory: Path | None = None) -> str:
    """The golden composed prompt: `prompt.md` plus the default glossary."""
    return _read_text((directory or spec_dir()) / "composed.txt").rstrip("\n")


def load_manifest(directory: Path | None = None) -> dict:
    raw = _read_text((directory or spec_dir()) / "manifest.json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecError(f"manifest.json is not valid JSON: {exc}") from exc


def load_glossary(path: str | Path | None = None, directory: Path | None = None) -> list[dict]:
    """Glossary entries from a JSON file: the default spec glossary, or
    `path` when a deployment supplies its own vocabulary.

    Shape: `{"entries": [{"canonical", "common_misrecognitions", "context"}]}`.
    Only `canonical` is required. A malformed file is an error here, at
    configuration time, rather than a surprise at dictation time.
    """
    target = Path(path).expanduser() if path else (directory or spec_dir()) / "glossary.json"
    raw = _read_text(target)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecError(f"{target} is not valid JSON: {exc}") from exc
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise SpecError(f"{target}: expected an object with an 'entries' list")
    cleaned: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SpecError(f"{target}: every glossary entry must be an object")
        canonical = str(entry.get("canonical", "")).strip()
        if not canonical:
            raise SpecError(f"{target}: every glossary entry needs a 'canonical' term")
        misheard = entry.get("common_misrecognitions") or []
        if not isinstance(misheard, list):
            raise SpecError(f"{target}: 'common_misrecognitions' must be a list ({canonical})")
        cleaned.append({
            "canonical": canonical,
            "common_misrecognitions": [str(m).strip() for m in misheard if str(m).strip()],
            "context": str(entry.get("context", "")).strip(),
        })
    return cleaned


# --------------------------------------------------------------- composing


def render_glossary_entry(entry: dict) -> str:
    line = f"- {entry['canonical']}"
    context = entry.get("context", "").strip()
    if context:
        line += f" — {context}"
        if not line.endswith("."):
            line += "."
    misheard = entry.get("common_misrecognitions") or []
    if misheard:
        line += " Sometimes mis-heard as: " + ", ".join(misheard) + "."
    return line


def render_glossary_section(entries: list[dict]) -> str:
    """The glossary appendix, or the empty string when there is nothing to
    say — so an empty glossary is invisible rather than a dangling header."""
    if not entries:
        return ""
    return "\n".join([GLOSSARY_SECTION_HEADER, ""] + [render_glossary_entry(e) for e in entries])


def build_system_prompt(entries: list[dict] | None = None, *, prompt: str | None = None) -> str:
    """`prompt.md` plus the glossary section, with no trailing newline.

    This composition is the contract `composed.txt` pins. Two
    implementations that agree on this function and on the spec files
    produce the same bytes, which is the only drift check that survives
    two repositories and two languages.
    """
    base = load_prompt() if prompt is None else prompt.strip()
    section = render_glossary_section(entries or [])
    return f"{base}\n\n{section}" if section else base


def wrap_transcript(raw_text: str) -> str:
    """The inert-data envelope. The transcript is passed through
    unmodified: never escaped, never trimmed, never truncated."""
    return f"{TRANSCRIPT_OPEN_TAG}\n{raw_text}\n{TRANSCRIPT_CLOSE_TAG}"


def build_polish_prompt(transcript: str, framing: str | None = None) -> str:
    """The whole cleanup request as one string.

    Caret's adapter boundary is text in → text out: an agent CLI has no
    system-message slot, so the framing and the enveloped transcript
    travel together. A backend with a real system role should send
    `framing` as the system message and `wrap_transcript(transcript)` as
    the user message — same two halves, same order.
    """
    return f"{framing or POLISH_FRAMING}\n\n{wrap_transcript(transcript)}"


# --------------------------------------------------------------- integrity


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_digest(hashes: dict[str, str]) -> str:
    """A short name for "these exact spec files". Order is fixed by
    `HASHED_FILES` so the digest does not depend on dict ordering."""
    joined = "".join(f"{name}:{hashes[name]}\n" for name in HASHED_FILES)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def file_hashes(directory: Path | None = None) -> dict[str, str]:
    directory = directory or spec_dir()
    return {name: sha256_file(directory / name) for name in HASHED_FILES}


def verify_spec(directory: Path | None = None) -> list[str]:
    """Problems with the spec on disk, as human sentences. Empty is good.

    Checks the three hashed files against `manifest.json`, the digest
    against its parts, and — the one that catches a hand-edited prompt —
    that composing `prompt.md` with `glossary.json` still reproduces
    `composed.txt`.
    """
    directory = directory or spec_dir()
    problems: list[str] = []
    try:
        manifest = load_manifest(directory)
        recorded = manifest.get("files", {})
        actual = file_hashes(directory)
    except (SpecError, OSError) as exc:
        return [str(exc)]
    for name in HASHED_FILES:
        if recorded.get(name) != actual[name]:
            problems.append(
                f"{name} does not match manifest.json "
                f"(recorded {str(recorded.get(name))[:12]}…, actual {actual[name][:12]}…)"
            )
    digest = compute_digest(actual)
    if manifest.get("digest") != digest:
        problems.append(
            f"manifest.json digest is {manifest.get('digest')!r}, files digest to {digest!r}"
        )
    try:
        composed = build_system_prompt(
            load_glossary(directory=directory), prompt=load_prompt(directory)
        )
        if composed != load_composed(directory):
            problems.append(
                "composed.txt is stale: prompt.md + glossary.json no longer compose to it"
            )
    except SpecError as exc:
        problems.append(str(exc))
    return problems


def spec_digest(directory: Path | None = None) -> str:
    """The digest recorded in the manifest — safe to report over health.
    It identifies the prompt without disclosing a word of it."""
    return str(load_manifest(directory).get("digest", ""))


def spec_id(directory: Path | None = None) -> str:
    """`caret-cleanup/1 <digest>` — what health reports."""
    digest = spec_digest(directory)
    return f"{SPEC_NAME} {digest}" if digest else SPEC_NAME


# ------------------------------------------------------------ env wiring


def glossary_from_env(env: dict[str, str] | None = None) -> list[dict]:
    """The glossary this deployment cleans up with.

    `CARET_CLEANUP_GLOSSARY=off` disables it entirely;
    `CARET_CLEANUP_GLOSSARY_PATH` replaces the default public vocabulary
    with the deployment's own (its product names, its people, its
    repositories). Replaces rather than extends: a deployment that wants
    both copies the defaults into its file, so what the model sees is
    always exactly one reviewable list.
    """
    env = os.environ if env is None else env
    if env.get("CARET_CLEANUP_GLOSSARY", "on").strip().lower() == "off":
        return []
    return load_glossary(env.get("CARET_CLEANUP_GLOSSARY_PATH", "").strip() or None)


def framing_from_env(env: dict[str, str] | None = None) -> str:
    """The composed system prompt for this deployment."""
    return build_system_prompt(glossary_from_env(env))


#: The default framing: the canonical prompt with the default glossary.
#: Equal to `composed.txt` by construction, which the test suite asserts.
POLISH_FRAMING = build_system_prompt(load_glossary())


__all__ = [
    "GLOSSARY_SECTION_HEADER",
    "POLISH_FRAMING",
    "SPEC_ID",
    "SPEC_NAME",
    "SPEC_VERSION",
    "SpecError",
    "TRANSCRIPT_CLOSE_TAG",
    "TRANSCRIPT_OPEN_TAG",
    "build_polish_prompt",
    "build_system_prompt",
    "compute_digest",
    "file_hashes",
    "framing_from_env",
    "glossary_from_env",
    "load_composed",
    "load_glossary",
    "load_manifest",
    "load_prompt",
    "render_glossary_entry",
    "render_glossary_section",
    "spec_dir",
    "spec_digest",
    "spec_id",
    "verify_spec",
    "wrap_transcript",
]
