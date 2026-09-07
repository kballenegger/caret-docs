"""The `caret-cleanup/1` spec, loaded and composed.

Cleanup is the one place a Caret backend puts the user's own words in
front of a language model and asks for them back. The wording is not
written here: it lives in the repository's canonical `spec/cleanup/v1/`
as data, and this module reads it, verifies it against its manifest, and
composes the system prompt exactly the way `composed.txt` records.

That last part is the point. The Go implementation composes the same two
files with the same rules and asserts the same equality, so a change to
the prompt that only one language notices fails a test rather than
quietly shipping two different products.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SPEC_NAME = "caret-cleanup/1"

TRANSCRIPT_OPEN_TAG = "<transcript>"
TRANSCRIPT_CLOSE_TAG = "</transcript>"

#: Byte-identical to the Go implementation's constant. Changing one
#: without the other breaks composed.txt in exactly one language.
GLOSSARY_SECTION_HEADER = (
    "Glossary. Canonical spellings for terms this speaker uses. Apply an "
    "entry only when the surrounding words clearly refer to that term. "
    "Never replace an ordinary word that merely sounds like one, and never "
    "insert a term the speaker did not say."
)

#: The files manifest.json covers, in digest order.
HASHED_FILES = ("prompt.md", "glossary.json", "composed.txt")

VOCABULARY_CONTEXT = "Supplied by the client for this dictation"


class SpecError(Exception):
    """The spec is missing, unreadable, or does not match its manifest.

    Raised at startup, never mid-dictation: a backend either has a
    verified prompt before it accepts an operation, or it says so on
    /health and returns dictation unpolished.
    """


@dataclass
class GlossaryEntry:
    canonical: str
    common_misrecognitions: list[str] = field(default_factory=list)
    context: str = ""


@dataclass
class CleanupSpec:
    directory: Path
    prompt: str
    glossary: list[GlossaryEntry]
    composed: str
    digest: str

    @property
    def spec_id(self) -> str:
        """`caret-cleanup/1 <digest>` — what /health reports. It names the
        prompt without disclosing a word of it."""
        return f"{SPEC_NAME} {self.digest}" if self.digest else SPEC_NAME

    def system_prompt(self, vocabulary: list[str] | None = None) -> str:
        """The composed system prompt, with the client's per-operation
        vocabulary folded into the glossary.

        §7 requires the vocabulary to reach the cleanup glossary, not
        just the recognizer: a name heard correctly and then "corrected"
        by the polish pass is the same bug, one step later.
        """
        entries = list(self.glossary)
        if vocabulary:
            entries.extend(_vocabulary_entries(entries, vocabulary))
        section = render_glossary_section(entries)
        return f"{self.prompt}\n\n{section}" if section else self.prompt

    def polish_prompt(self, transcript: str, framing: str | None = None) -> str:
        """The whole cleanup request as one string, for a text-in text-out
        adapter with no system-message slot. A provider with a real system
        role should send the framing and `wrap_transcript` separately —
        same two halves, same order."""
        return f"{framing or self.system_prompt()}\n\n{wrap_transcript(transcript)}"


def wrap_transcript(text: str) -> str:
    """The inert-data envelope. The transcript is passed through
    unmodified: never escaped, never trimmed, never truncated. Meaning
    preservation outranks tidiness, even for a transcript that contains
    the closing tag."""
    return f"{TRANSCRIPT_OPEN_TAG}\n{text}\n{TRANSCRIPT_CLOSE_TAG}"


def render_glossary_entry(entry: GlossaryEntry) -> str:
    line = f"- {entry.canonical}"
    context = (entry.context or "").strip()
    if context:
        line += f" — {context}"
        if not line.endswith("."):
            line += "."
    misheard = [m.strip() for m in (entry.common_misrecognitions or []) if m and m.strip()]
    if misheard:
        line += " Sometimes mis-heard as: " + ", ".join(misheard) + "."
    return line


def render_glossary_section(entries: list[GlossaryEntry]) -> str:
    """The glossary appendix, or the empty string when there is nothing
    to say — an empty glossary is invisible, not a dangling header."""
    if not entries:
        return ""
    lines = [GLOSSARY_SECTION_HEADER, ""]
    lines.extend(render_glossary_entry(entry) for entry in entries)
    return "\n".join(lines)


def _vocabulary_entries(existing: list[GlossaryEntry], vocabulary: list[str]) -> list[GlossaryEntry]:
    have = {entry.canonical.lower() for entry in existing}
    out = []
    for term in vocabulary:
        key = term.lower()
        if key in have:
            continue
        have.add(key)
        out.append(GlossaryEntry(canonical=term, context=VOCABULARY_CONTEXT))
    return out


def compute_digest(hashes: dict[str, str]) -> str:
    """A short name for "these exact spec files". The order is fixed by
    HASHED_FILES so the digest never depends on dict ordering."""
    joined = "".join(f"{name}:{hashes[name]}\n" for name in HASHED_FILES)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def find_spec_dir() -> Path:
    """Where the canonical spec lives.

    `CARET_CLEANUP_SPEC_DIR` points a deployment at its own vendored
    copy. Otherwise walk up from this file and the working directory,
    which finds it when running from anywhere inside the repository.
    """
    override = os.environ.get("CARET_CLEANUP_SPEC_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    roots = [Path(__file__).resolve().parent, Path.cwd().resolve()]
    for root in roots:
        for directory in [root, *root.parents][:9]:
            candidate = directory / "spec" / "cleanup" / "v1"
            if (candidate / "manifest.json").is_file():
                return candidate
    raise SpecError(
        "cleanup spec not found: set CARET_CLEANUP_SPEC_DIR to a spec/cleanup/v1 directory"
    )


def load_spec(directory: str | Path | None = None) -> CleanupSpec:
    """Load and verify the spec. Every failure is a `SpecError` with a
    sentence an operator can act on."""
    directory = Path(directory) if directory else find_spec_dir()
    try:
        raw = {name: (directory / name).read_bytes() for name in HASHED_FILES}
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"cannot read the cleanup spec from {directory}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"manifest.json is not valid JSON: {exc}") from exc

    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}
    recorded = manifest.get("files", {})
    for name in HASHED_FILES:
        if recorded.get(name) != hashes[name]:
            raise SpecError(f"{name} does not match manifest.json")
    digest = compute_digest(hashes)
    if manifest.get("digest") != digest:
        raise SpecError(
            f"manifest.json digest is {manifest.get('digest')!r}, the files digest to {digest!r}"
        )

    glossary = _parse_glossary(raw["glossary.json"], directory)
    spec = CleanupSpec(
        directory=directory,
        prompt=raw["prompt.md"].decode("utf-8").strip(),
        glossary=glossary,
        composed=raw["composed.txt"].decode("utf-8").rstrip("\n"),
        digest=digest,
    )
    # The check that catches a hand-edited prompt, and the one that keeps
    # two languages honest about each other.
    if spec.system_prompt() != spec.composed:
        raise SpecError("composed.txt is stale: prompt.md and glossary.json no longer compose to it")
    return spec


def _parse_glossary(data: bytes, directory: Path) -> list[GlossaryEntry]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecError(f"{directory / 'glossary.json'} is not valid JSON: {exc}") from exc
    entries = parsed.get("entries") if isinstance(parsed, dict) else parsed
    if not isinstance(entries, list):
        raise SpecError("glossary.json: expected an object with an 'entries' list")
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SpecError("glossary.json: every entry must be an object")
        canonical = str(entry.get("canonical", "")).strip()
        if not canonical:
            raise SpecError("glossary.json: every entry needs a 'canonical' term")
        misheard = entry.get("common_misrecognitions") or []
        if not isinstance(misheard, list):
            raise SpecError(f"glossary.json: 'common_misrecognitions' must be a list ({canonical})")
        out.append(GlossaryEntry(
            canonical=canonical,
            common_misrecognitions=[str(m).strip() for m in misheard if str(m).strip()],
            context=str(entry.get("context", "")).strip(),
        ))
    return out
