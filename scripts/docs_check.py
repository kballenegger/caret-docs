#!/usr/bin/env python3
"""Docs structure guard: the V4 path is the only active path.

Deterministic, standard library only. Enforced by `make test` and
`make site`, after the public-repo guard. Four invariants:

  1. The required active pages exist (overview, protocol, reference
     design, migration, cleanup, imagine references, legacy index).
  2. Every internal link in every docs page resolves to a file that the
     assembled site will actually contain. The expected site layout is
     mirrored from the Makefile's `site` target in EXTRA_SITE_FILES.
  3. Archived pages under docs/legacy/ carry the ARCHIVED banner, and
     the redirect stubs left at retired URLs point into /legacy/.
  4. Active pages never link back to a retired URL, and the root README
     never links to a root path that moved into legacy/.

Exit 0 means the docs tree is publishable; exit 1 lists every finding.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

REQUIRED_PAGES = (
    "index.html",
    "protocol/index.html",
    "reference/index.html",
    "migration/index.html",
    "cleanup/index.html",
    "imagine-references/index.html",
    "legacy/index.html",
)

# Files `make site` copies into _site/ from outside docs/. Site-absolute
# paths, no leading slash. Mirror the Makefile when changing either.
EXTRA_SITE_FILES = tuple(
    ["spec/cleanup/v1/" + p.name for p in sorted((REPO_ROOT / "spec/cleanup/v1").glob("*"))]
    + ["legacy/openapi.yaml", "openapi.yaml"]
    + [
        prefix + p.name
        for p in sorted((REPO_ROOT / "legacy/agent-prompts").glob("*.md"))
        for prefix in ("legacy/agent-prompts/", "agent-prompts/")
    ]
    + [
        "legacy/integrations/hermes/caret-connect/SKILL.md",
        "integrations/hermes/caret-connect/SKILL.md",
    ]
)

# Retired page URLs: only redirect stubs may live there, and no active
# page may link to them.
RETIRED_PREFIXES = ("/connect/", "/your-agent/", "/live-dictation/", "/agent-prompts/")
RETIRED_EXACT = ("/openapi.yaml",)

STUB_MARKER = "<!-- redirect-stub -->"
ARCHIVE_MARKER = "ARCHIVED"

HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"'#]+)(?:#[^"']*)?["']""")

# README links that would resurrect a moved root path.
README_FORBIDDEN = (
    "](reference-backend",
    "](integrations",
    "](agent-prompts",
    "](openapi.yaml",
    "](.claude-plugin",
)


def site_paths() -> set[str]:
    """Every site-absolute path (no leading slash) the built site contains."""
    paths = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file()}
    paths.update(EXTRA_SITE_FILES)
    return paths


def resolve(link: str, page_rel: str) -> str | None:
    """Normalize an internal link to a site path; None if external."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", link):  # https:, mailto:, …
        return None
    if link.startswith("/"):
        target = link[1:]
    else:
        base = str(Path(page_rel).parent)
        target = str((Path("" if base == "." else base) / link))
    target = str(Path(target)) if target else ""
    if target in ("", "."):
        target = "index.html"
    elif link.endswith("/"):
        target += "/index.html"
    return target


def check_links(html: str, page_rel: str, known: set[str]) -> list[str]:
    findings = []
    for link in HREF.findall(html):
        target = resolve(link, page_rel)
        if target is None:
            continue
        if target not in known and target + "/index.html" not in known:
            findings.append(f"docs/{page_rel}: broken internal link {link!r}")
    return findings


def check_retired_references(html: str, page_rel: str) -> list[str]:
    findings = []
    for link in HREF.findall(html):
        if link in RETIRED_EXACT or any(link.startswith(p) for p in RETIRED_PREFIXES):
            findings.append(
                f"docs/{page_rel}: active page links to retired URL {link!r}"
            )
    return findings


def check_stub_target(html: str, page_rel: str) -> list[str]:
    """Ensure a retired URL redirects to its corresponding archive page."""
    expected = "/legacy/" + page_rel.removesuffix("/index.html") + "/"
    targets = re.findall(r"url=(/legacy/[^\"'\s>]+)", html)
    if expected not in targets:
        return [f"docs/{page_rel}: redirect stub does not target {expected}"]
    return []


def main() -> int:
    findings: list[str] = []
    known = site_paths()

    for rel in REQUIRED_PAGES:
        if not (DOCS / rel).is_file():
            findings.append(f"missing required active page docs/{rel}")

    for page in sorted(DOCS.rglob("*.html")):
        rel = str(page.relative_to(DOCS))
        html = page.read_text(encoding="utf-8")
        is_stub = STUB_MARKER in html
        is_legacy = rel.startswith("legacy/")

        findings.extend(check_links(html, rel, known))

        if is_stub:
            findings.extend(check_stub_target(html, rel))
        elif is_legacy:
            if rel != "legacy/index.html" and ARCHIVE_MARKER not in html:
                findings.append(f"docs/{rel}: archived page missing {ARCHIVE_MARKER} banner")
        else:
            findings.extend(check_retired_references(html, rel))

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for fragment in README_FORBIDDEN:
        if fragment in readme:
            findings.append(f"README.md: links to moved root path via {fragment!r}")
    if "docs/protocol/" not in readme:
        findings.append("README.md: does not point the reader at docs/protocol/")

    if findings:
        print("docs_check: DOCS STRUCTURE VIOLATIONS", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(f"docs_check: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    pages = len(list(DOCS.rglob("*.html")))
    print(f"docs_check: ok — {pages} pages, all links resolve, archive marked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
