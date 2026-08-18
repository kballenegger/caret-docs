#!/usr/bin/env python3
"""Regenerate the derived files in `spec/cleanup/v1/`.

Two of the four spec files are written by hand — `prompt.md` and
`glossary.json`. The other two are derived from them and must never be
hand-edited:

    composed.txt    prompt.md composed with the default glossary
    manifest.json   sha256 of the three, plus the short spec digest

Edit a source file, run this, commit all of it together. The digest
changing is the point: it is the visible, greppable signal that the
cleanup prompt moved, and every consumer of the spec pins it.

    python3 scripts/build_cleanup_spec.py           rewrite the derived files
    python3 scripts/build_cleanup_spec.py --check   fail if they are stale

`--check` is what the test suite runs, so a hand-edited prompt with a
stale manifest cannot merge.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reference-backend"))

from caret_backend import cleanup  # noqa: E402


def build() -> tuple[str, str]:
    """Return the (composed prompt, manifest JSON) the sources imply."""
    directory = cleanup.DEFAULT_SPEC_DIR
    composed = cleanup.build_system_prompt(
        cleanup.load_glossary(directory=directory),
        prompt=cleanup.load_prompt(directory),
    )
    # The manifest hashes composed.txt, so it must be written first — or,
    # here, hashed from the bytes we are about to write.
    composed_bytes = (composed + "\n").encode("utf-8")
    hashes = {
        "prompt.md": cleanup.sha256_file(directory / "prompt.md"),
        "glossary.json": cleanup.sha256_file(directory / "glossary.json"),
        "composed.txt": cleanup.hashlib.sha256(composed_bytes).hexdigest(),
    }
    manifest = {
        "spec": cleanup.SPEC_ID,
        "version": cleanup.SPEC_VERSION,
        "name": cleanup.SPEC_NAME,
        "description": (
            "Canonical Caret transcript-cleanup prompt. prompt.md and "
            "glossary.json are the sources; composed.txt is prompt.md "
            "composed with the default glossary and is what a consumer "
            "asserts its own composition against. digest is sha256 over "
            "'<name>:<sha256>\\n' for each file in order, truncated to 16 "
            "hex characters."
        ),
        "files": hashes,
        "digest": cleanup.compute_digest(hashes),
    }
    return composed + "\n", json.dumps(manifest, indent=2) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv[1:]
    directory = cleanup.DEFAULT_SPEC_DIR
    composed, manifest = build()
    targets = {directory / "composed.txt": composed, directory / "manifest.json": manifest}

    if check:
        stale = [
            path.name
            for path, want in targets.items()
            if not path.exists() or path.read_text(encoding="utf-8") != want
        ]
        if stale:
            print(
                "spec/cleanup/v1 is stale: "
                + ", ".join(sorted(stale))
                + " — run `python3 scripts/build_cleanup_spec.py`",
                file=sys.stderr,
            )
            return 1
        print(f"cleanup spec ok — {cleanup.spec_id(directory)}")
        return 0

    for path, want in targets.items():
        path.write_text(want, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(f"cleanup spec — {cleanup.spec_id(directory)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
