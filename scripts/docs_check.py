#!/usr/bin/env python3
"""Docs structure guard: the V4 path is the only published path.

Deterministic, standard library only. Enforced by `make test` and
`make site`, after the public-repo guard. The invariants:

  1. The required active pages exist (overview, hosted API, protocol,
     reference, migration, cleanup, imagine references, and the
     retirement notice at /legacy/).
  2. Every internal link in every docs page resolves to a file that the
     assembled site will actually contain. The expected site layout is
     mirrored from the Makefile's `site` target in EXTRA_SITE_FILES.
  3. Every active page carries the same shared shell: the header with
     the link back to typewithcaret.com, and one sidebar whose only
     difference between pages is which link is marked current. The
     sidebar never lists retired material.
  4. Retired pre-V4 URLs exist only as redirect stubs pointing at the
     retirement notice, or as short retirement-notice files. No archived
     page, runbook, schema or prompt is published.
  5. Active pages never link to a retired URL (the notice itself is
     fine), and the root README never links to a root path that moved
     into legacy/.

With `--site DIR`, the same retired-path rules are applied to the
assembled output, so a stray copy cannot reach the deploy.

Exit 0 means the docs tree is publishable; exit 1 lists every finding.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

# Pages that share the sidebar. Site URL -> file under docs/.
ACTIVE_PAGES = {
    "/": "index.html",
    "/hosted/": "hosted/index.html",
    "/protocol/": "protocol/index.html",
    "/reference/": "reference/index.html",
    "/migration/": "migration/index.html",
    "/cleanup/": "cleanup/index.html",
    "/imagine-references/": "imagine-references/index.html",
}

NOTICE_PAGE = "legacy/index.html"

REQUIRED_PAGES = tuple(ACTIVE_PAGES.values()) + (NOTICE_PAGE,)

# Files `make site` copies into _site/ from outside docs/. Site-absolute
# paths, no leading slash. Mirror the Makefile when changing either.
EXTRA_SITE_FILES = tuple(
    "spec/cleanup/v1/" + p.name for p in sorted((REPO_ROOT / "spec/cleanup/v1").glob("*"))
)

# Retired URL space (site paths, no leading slash). Only redirect stubs
# and retirement notices may live there, and no active page may link
# there. The notice page itself is the one exception.
RETIRED_PREFIXES = (
    "connect/",
    "your-agent/",
    "live-dictation/",
    "agent-prompts/",
    "integrations/",
    "legacy/",
)
RETIRED_EXACT = ("openapi.yaml",)

STUB_MARKER = "<!-- redirect-stub -->"
STUB_TARGET = "/legacy/"
NOTICE_MARKER = "caret-docs: retired"
NOTICE_PAGE_MARKER = "<!-- retired-notice -->"
ARCHIVE_BANNER = "ARCHIVED"

MAIN_SITE = "https://typewithcaret.com"

HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"'#]+)(?:#[^"']*)?["']""")
SIDEBAR = re.compile(r'<aside class="sidebar" id="site-nav">.*?</aside>', re.S)
HEADER = re.compile(r'<header class="site">.*?</header>', re.S)
CURRENT = re.compile(r'<a href="([^"]+)" aria-current="page">')

# README links that would resurrect a moved root path.
README_FORBIDDEN = (
    "](reference-backend",
    "](integrations",
    "](agent-prompts",
    "](openapi.yaml",
    "](.claude-plugin",
    "](docs/legacy/connect",
    "](docs/legacy/your-agent",
    "](docs/legacy/live-dictation",
)


def site_paths() -> set[str]:
    """Every site-absolute path (no leading slash) the built site contains."""
    paths = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file()}
    paths.update(EXTRA_SITE_FILES)
    return paths


def is_retired(site_path: str) -> bool:
    """True for a site path inside the retired URL space (notice excluded)."""
    if site_path == NOTICE_PAGE:
        return False
    return site_path in RETIRED_EXACT or any(site_path.startswith(p) for p in RETIRED_PREFIXES)


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
        target = resolve(link, page_rel)
        if target is not None and is_retired(target):
            findings.append(
                f"docs/{page_rel}: active page links to retired URL {link!r}"
            )
    return findings


def check_stub_target(html: str, page_rel: str) -> list[str]:
    """Ensure a retired URL redirects to the retirement notice."""
    targets = re.findall(r"url=(/[^\"'\s>]*)", html)
    if targets != [STUB_TARGET]:
        return [f"docs/{page_rel}: redirect stub must target {STUB_TARGET}, found {targets}"]
    return []


def check_retired_file(text: str, rel: str, label: str = "docs") -> list[str]:
    """A file at a retired path must be a stub or a retirement notice."""
    if rel.endswith(".html"):
        if STUB_MARKER not in text:
            return [f"{label}/{rel}: retired URL holds a page instead of a redirect stub"]
        return check_stub_target(text, rel)
    if NOTICE_MARKER not in text:
        return [f"{label}/{rel}: retired path holds content instead of a retirement notice"]
    return []


def sidebar_of(html: str) -> str | None:
    m = SIDEBAR.search(html)
    return m.group(0) if m else None


def check_shell(html: str, page_rel: str, url: str, canonical_sidebar: str) -> list[str]:
    """The active page carries the shared header and sidebar."""
    findings = []
    header = HEADER.search(html)
    if not header:
        return [f"docs/{page_rel}: missing shared header"]
    if f'href="{MAIN_SITE}"' not in header.group(0):
        findings.append(f"docs/{page_rel}: header lacks the link back to {MAIN_SITE}")
    if 'aria-controls="site-nav"' not in header.group(0):
        findings.append(f"docs/{page_rel}: header lacks the mobile menu button")
    if '<a class="skip" href="#content">' not in html:
        findings.append(f"docs/{page_rel}: missing skip link")
    if 'id="content"' not in html:
        findings.append(f"docs/{page_rel}: missing #content landmark")

    sidebar = sidebar_of(html)
    if sidebar is None:
        return findings + [f"docs/{page_rel}: missing sidebar"]
    current = CURRENT.findall(sidebar)
    if current != [url]:
        findings.append(f"docs/{page_rel}: sidebar must mark exactly {url} current, found {current}")
    if sidebar.replace(' aria-current="page"', "") != canonical_sidebar:
        findings.append(f"docs/{page_rel}: sidebar differs from the shared sidebar")
    for link in HREF.findall(sidebar):
        target = resolve(link, page_rel)
        if target is not None and (is_retired(target) or target == NOTICE_PAGE):
            findings.append(f"docs/{page_rel}: sidebar lists retired material {link!r}")
    if 'src="/nav.js"' not in html:
        findings.append(f"docs/{page_rel}: missing /nav.js")
    return findings


def check_site(site_dir: Path) -> list[str]:
    """Apply the retired-path rules to an assembled site tree."""
    findings = []
    if not site_dir.is_dir():
        return [f"{site_dir}: not a directory"]
    for path in sorted(site_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(site_dir))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if is_retired(rel):
            findings.extend(check_retired_file(text, rel, label=str(site_dir)))
        elif rel.endswith(".html") and ARCHIVE_BANNER in text and rel != NOTICE_PAGE:
            findings.append(f"{site_dir}/{rel}: published page carries the archive banner")
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    site_dir: Path | None = None
    if argv[:1] == ["--site"] and len(argv) == 2:
        site_dir = Path(argv[1])
    elif argv:
        print("usage: docs_check.py [--site DIR]", file=sys.stderr)
        return 2

    findings: list[str] = []
    known = site_paths()

    for rel in REQUIRED_PAGES:
        if not (DOCS / rel).is_file():
            findings.append(f"missing required active page docs/{rel}")

    canonical_sidebar = None
    overview = DOCS / ACTIVE_PAGES["/"]
    if overview.is_file():
        sb = sidebar_of(overview.read_text(encoding="utf-8"))
        canonical_sidebar = sb.replace(' aria-current="page"', "") if sb else None

    files_by_rel = {ACTIVE_PAGES[u]: u for u in ACTIVE_PAGES}

    for page in sorted(DOCS.rglob("*")):
        if not page.is_file():
            continue
        rel = str(page.relative_to(DOCS))
        if rel.startswith("assets/") or rel in ("styles.css", "nav.js"):
            continue
        text = page.read_text(encoding="utf-8")

        if rel.endswith(".html"):
            findings.extend(check_links(text, rel, known))

        if is_retired(rel):
            findings.extend(check_retired_file(text, rel))
            continue

        if rel == NOTICE_PAGE:
            if NOTICE_PAGE_MARKER not in text:
                findings.append(f"docs/{rel}: retirement notice missing {NOTICE_PAGE_MARKER}")
            findings.extend(check_retired_references(text, rel))
            continue

        if rel in files_by_rel:
            findings.extend(check_retired_references(text, rel))
            if canonical_sidebar is None:
                findings.append(f"docs/{rel}: no canonical sidebar (overview lacks one)")
            else:
                findings.extend(check_shell(text, rel, files_by_rel[rel], canonical_sidebar))
        elif rel.endswith(".html"):
            findings.append(f"docs/{rel}: unexpected page outside the active set")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for fragment in README_FORBIDDEN:
        if fragment in readme:
            findings.append(f"README.md: links to moved root path via {fragment!r}")
    if "docs/protocol/" not in readme:
        findings.append("README.md: does not point the reader at docs/protocol/")

    if site_dir is not None:
        findings.extend(check_site(site_dir))

    if findings:
        print("docs_check: DOCS STRUCTURE VIOLATIONS", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(f"docs_check: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    pages = len(list(DOCS.rglob("*.html")))
    stubs = sum(1 for p in DOCS.rglob("*.html") if STUB_MARKER in p.read_text(encoding="utf-8"))
    where = f", assembled site {site_dir} clean" if site_dir is not None else ""
    print(
        f"docs_check: ok — {len(ACTIVE_PAGES)} active pages share the shell, "
        f"{stubs} redirect stubs, {pages} pages total, all links resolve{where}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
