#!/usr/bin/env python3
"""Public-repo guard: fail if anything private leaks into this repository.

This repository is public and must stay self-contained. The guard scans
every git-tracked file — and the assembled `_site/` output when present —
for content that would disclose or depend on private infrastructure:

  * private machine paths (absolute home directories, `~`-relative repo
    paths outside this repository)
  * private project and infrastructure identifiers
  * credential material (provider API keys, private key blocks, tokens)
  * build/source coupling to repositories other than this one

Run it directly (`python3 scripts/public_guard.py`) or via `make test`,
which runs it first. Exit 0 means clean; exit 1 lists every finding with
file and line.

The forbidden identifier strings below are assembled from fragments so the
guard does not trip over its own definitions — the assembled values never
appear literally in this file or anywhere else in the repository.

Allowlist policy: none. There is deliberately no per-file allowlist; a
legitimate future need for one should be added as a narrow, commented
exception here, in code review, not silently.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Assembled from fragments so this file cannot match its own rules.
_J = "".join
PRIVATE_IDENTIFIERS = (
    _J(("kenneth", "-", "bot")),        # the private machine's username
    _J(("k", "law")),                    # private agent/workspace naming
    _J(("min", "ions")),                 # private worker infrastructure
    _J(("uto", "pian")),                 # private org naming
)
# Note: the repo owner's public GitHub handle (github.com/kballenegger) is
# deliberately NOT forbidden — it is the public home of this repository.

# Paths that would couple this repo to a private machine or repository.
PRIVATE_PATH_PATTERNS = (
    re.compile(r"/Users/[A-Za-z0-9._-]+"),          # any macOS home path
    re.compile(r"/home/[A-Za-z0-9._-]+"),           # any Linux home path
    re.compile("~/" + _J(("car", "et")) + r"(?![\w-])"),  # the private backend checkout (`~/caret-docs`, this repo, stays fine)
    re.compile(r"\$\{?CARET_REPO\}?"),               # build coupling to it
)

# Credential material. These match real secrets, not documentation about
# secrets ("generate a key with secrets.token_urlsafe" is fine).
SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}"),  # JWTs
)

# Redaction-fragile secret assignments. A shell line that assigns a
# command substitution to a key-shaped variable — CARET_API_KEYS, then an
# equals sign, then a `$(…)` that generates the key inline (spelled apart
# here so this file does not trip its own rule) — is correct shell
# but publishes badly: naive secret scrubbers — in CI log
# masks, chat integrations, agent harnesses that read these pages — match
# `…KEY(S)=` and replace everything up to the first space, which turns the
# line into `export CARET_API_KEYS=*** -c 'import secrets;…')"`. That is
# not executable, and a reader who pastes it gets a shell error instead of
# a key. Keep the generator on its own line and the assigned value a
# single whitespace-free token, so the worst a scrubber can do is mask a
# placeholder that was already meant to be replaced.
FRAGILE_SECRET_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]*(?:KEY|KEYS|SECRET|TOKEN|PASSWORD)\s*=\s*[\"']?(?:\$\(|`)"
)

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pyc"}


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def site_files() -> list[Path]:
    site = REPO_ROOT / "_site"
    if not site.is_dir():
        return []
    return [p for p in site.rglob("*") if p.is_file()]


def scan_text(text: str, origin: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for ident in PRIVATE_IDENTIFIERS:
            if ident in lowered:
                findings.append(
                    f"{origin}:{lineno}: private identifier {ident!r}"
                )
        for pattern in PRIVATE_PATH_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    f"{origin}:{lineno}: private path/coupling {match.group(0)!r}"
                )
        for pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(
                    f"{origin}:{lineno}: credential-shaped string {match.group(0)[:12]!r}…"
                )
        match = FRAGILE_SECRET_ASSIGNMENT.search(line)
        if match:
            findings.append(
                f"{origin}:{lineno}: redaction-fragile secret assignment "
                f"{match.group(0)!r} — put the generator on its own line and "
                f"assign a single whitespace-free token"
            )
    return findings


def scan_file(path: Path) -> list[str]:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: nothing text-shaped to leak
    return scan_text(text, str(path.relative_to(REPO_ROOT)))


def main() -> int:
    targets = tracked_files() + site_files()
    findings: list[str] = []
    for path in targets:
        findings.extend(scan_file(path))
    if findings:
        print("public_guard: PRIVATE CONTENT DETECTED", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            f"public_guard: {len(findings)} finding(s) across "
            f"{len(targets)} scanned file(s)",
            file=sys.stderr,
        )
        return 1
    print(f"public_guard: ok — {len(targets)} files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
