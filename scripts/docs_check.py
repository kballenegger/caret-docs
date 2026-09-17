#!/usr/bin/env python3
"""Docs structure guard: the current caret/v4 pages are the only published pages.

Deterministic, standard library only. Enforced by `make test` and
`make site`, after the public-repo guard. The invariants:

  1. The required pages exist (overview, agent instructions, protocol,
     reference, cleanup, imagine references) and no other HTML page is
     present. The only non-HTML page content is the agent instructions
     Markdown, published verbatim at /agent/instructions.md.
  2. Every internal link in every page resolves to a file that the
     assembled site will actually contain. The expected site layout is
     mirrored from the Makefile's `site` target in EXTRA_SITE_FILES.
  3. Every page carries the same shared shell: the header with the link
     back to typewithcaret.com and the mobile menu button, and one
     sidebar whose only difference between pages is which link is
     marked current.
  4. Nothing retired is published or referenced. No file lives at a
     pre-V4 URL (connect/, your-agent/, live-dictation/, legacy/,
     migration/, agent-prompts/, integrations/, openapi.yaml), no page
     documents the hosted service as a developer API (hosted/,
     api.typewithcaret.com), and no page carries an archive or
     retirement banner. The root README follows the same rules.

With `--site DIR`, the same rules are applied to the assembled output,
so a stray copy cannot reach the deploy.

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
    "/agent/": "agent/index.html",
    "/protocol/": "protocol/index.html",
    "/reference/": "reference/index.html",
    "/cleanup/": "cleanup/index.html",
    "/imagine-references/": "imagine-references/index.html",
}

# Non-HTML files under docs/ that are published on purpose.
PUBLISHED_FILES = ("agent/instructions.md",)

REQUIRED_PAGES = tuple(ACTIVE_PAGES.values()) + PUBLISHED_FILES

# Files `make site` copies into _site/ from outside docs/. Site-absolute
# paths, no leading slash. Mirror the Makefile when changing either.
EXTRA_SITE_FILES = tuple(
    "spec/cleanup/v1/" + p.name for p in sorted((REPO_ROOT / "spec/cleanup/v1").glob("*"))
)

# Retired URL space (site paths, no leading slash). Nothing may exist
# there and nothing may link there; the old URLs answer 404.
RETIRED_PREFIXES = (
    "hosted/",
    "connect/",
    "your-agent/",
    "live-dictation/",
    "migration/",
    "legacy/",
    "agent-prompts/",
    "integrations/",
)
RETIRED_EXACT = ("openapi.yaml",)

# Text that must not appear in any published page: the hosted service
# presented as a developer API, and the archive / retirement callouts.
FORBIDDEN_TEXT = (
    "api.typewithcaret.com",
    "/v2/dictionary",
    "/v4/dictate",
    "ARCHIVED",
    "<!-- redirect-stub -->",
    "<!-- retired-notice -->",
    "caret-docs: retired",
    "retirement notice",
    "migration guide",
)

MAIN_SITE = "https://typewithcaret.com"

HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"'#]+)(?:#[^"']*)?["']""")
MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
SIDEBAR = re.compile(r'<aside class="sidebar" id="site-nav">.*?</aside>', re.S)
HEADER = re.compile(r'<header class="site">.*?</header>', re.S)
CURRENT = re.compile(r'<a href="([^"]+)" aria-current="page">')

# README links that would resurrect a retired path.
README_FORBIDDEN = (
    "](reference-backend",
    "](integrations",
    "](agent-prompts",
    "](openapi.yaml",
    "](.claude-plugin",
    "](docs/hosted",
    "](docs/migration",
    "](docs/legacy",
    "docs.typewithcaret.com/hosted/",
    "docs.typewithcaret.com/migration/",
    "docs.typewithcaret.com/legacy/",
    "api.typewithcaret.com",
)


def site_paths() -> set[str]:
    """Every site-absolute path (no leading slash) the built site contains."""
    paths = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file()}
    paths.update(EXTRA_SITE_FILES)
    return paths


def is_retired(site_path: str) -> bool:
    """True for a site path inside the retired URL space."""
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


def check_links(
    html: str, page_rel: str, known: set[str], label: str = "docs"
) -> list[str]:
    findings = []
    links = HREF.findall(html) + MARKDOWN_LINK.findall(html)
    for link in links:
        target = resolve(link, page_rel)
        if target is None:
            continue
        if is_retired(target):
            continue
        if target not in known and target + "/index.html" not in known:
            findings.append(f"{label}/{page_rel}: broken internal link {link!r}")
    return findings


def check_retired_references(html: str, page_rel: str, label: str = "docs") -> list[str]:
    findings = []
    links = HREF.findall(html) + MARKDOWN_LINK.findall(html)
    for link in links:
        target = resolve(link, page_rel)
        if target is not None and is_retired(target):
            findings.append(f"{label}/{page_rel}: links to retired URL {link!r}")
    return findings


def check_forbidden_text(text: str, rel: str, label: str = "docs") -> list[str]:
    return [
        f"{label}/{rel}: contains retired or hosted-API text {needle!r}"
        for needle in FORBIDDEN_TEXT
        if needle in text
    ]


def sidebar_of(html: str) -> str | None:
    m = SIDEBAR.search(html)
    return m.group(0) if m else None


def check_shell(html: str, page_rel: str, url: str, canonical_sidebar: str) -> list[str]:
    """The page carries the shared header and sidebar."""
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
        if target is not None and target not in ACTIVE_PAGES.values():
            findings.append(f"docs/{page_rel}: sidebar lists a non-current page {link!r}")
    if 'src="/nav.js"' not in html:
        findings.append(f"docs/{page_rel}: missing /nav.js")
    return findings


def check_site(site_dir: Path) -> list[str]:
    """Apply the retired-path and forbidden-text rules to an assembled site tree."""
    findings = []
    if not site_dir.is_dir():
        return [f"{site_dir}: not a directory"]
    label = str(site_dir)
    known = {str(path.relative_to(site_dir)) for path in site_dir.rglob("*") if path.is_file()}
    for path in sorted(site_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(site_dir))
        if is_retired(rel):
            findings.append(f"{label}/{rel}: retired path is present in the assembled site")
            continue
        if not rel.endswith((".html", ".md")):
            continue
        text = path.read_text(encoding="utf-8")
        findings.extend(check_links(text, rel, known, label=label))
        findings.extend(check_forbidden_text(text, rel, label=label))
        findings.extend(check_retired_references(text, rel, label=label))
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
            findings.append(f"missing required page docs/{rel}")

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
        if is_retired(rel):
            findings.append(f"docs/{rel}: retired path must not exist")
            continue
        if rel.startswith("assets/") or rel in ("styles.css", "nav.js"):
            continue
        if rel in PUBLISHED_FILES:
            text = page.read_text(encoding="utf-8")
            findings.extend(check_links(text, rel, known))
            findings.extend(check_forbidden_text(text, rel))
            findings.extend(check_retired_references(text, rel))
            continue
        if not rel.endswith(".html"):
            findings.append(f"docs/{rel}: unexpected file outside the published set")
            continue
        text = page.read_text(encoding="utf-8")
        findings.extend(check_links(text, rel, known))
        findings.extend(check_retired_references(text, rel))
        findings.extend(check_forbidden_text(text, rel))

        if rel in files_by_rel:
            if canonical_sidebar is None:
                findings.append(f"docs/{rel}: no canonical sidebar (overview lacks one)")
            else:
                findings.extend(check_shell(text, rel, files_by_rel[rel], canonical_sidebar))
        else:
            findings.append(f"docs/{rel}: unexpected page outside the published set")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for fragment in README_FORBIDDEN:
        if fragment in readme:
            findings.append(f"README.md: references a retired path via {fragment!r}")
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
    where = f", assembled site {site_dir} clean" if site_dir is not None else ""
    print(
        f"docs_check: ok — {pages} pages share the shell, nothing retired published, "
        f"all links resolve{where}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
