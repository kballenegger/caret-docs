#!/usr/bin/env python3
"""Make the assembled site readable by agents and search engines.

Runs inside `make site`, after docs/ and the spec are copied into _site/
and before the guards. Standard library only. For every page in
docs_check.ACTIVE_PAGES it:

  * dates the page from data: the last git commit that touched its source
    file (`git log -1 --format=%cI -- <file>`), normalised to UTC. That
    one date is used everywhere the page is described;
  * writes a Markdown twin at <dir>/index.md (index.md for the home page)
    from the page's <main> content, with absolute links;
  * adds a visible "Updated" line under the page's h1, and to <head> a
    canonical link, a text/markdown alternate pointing at the twin, and
    JSON-LD (TechArticle; the home page also a WebSite);

then regenerates sitemap.xml and llms.txt (docs/llms.txt is the
hand-written head; the page list is appended here) and writes
.lastmod.tsv: one `<site path>\\t<ISO-8601 UTC>` line per published HTML
page, Markdown file, llms.txt, sitemap.xml and robots.txt. A deploy step
sets file mtimes from it so Last-Modified matches. It is a dotfile, which
the web server does not serve.

    python3 scripts/build_site_extras.py _site
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docs_check  # noqa: E402

REPO_ROOT = docs_check.REPO_ROOT
DOCS = docs_check.DOCS
SITE_URL = "https://docs.typewithcaret.com"
PRODUCT_URL = "https://typewithcaret.com/"
PUBLISHER = "Utopian Ventures, LLC"

# Names used in llms.txt, in sidebar order.
PAGE_NAMES = {
    "/": "Overview",
    "/agent/": "Agent instructions",
    "/protocol/": "Protocol",
    "/reference/": "Reference implementations",
    "/cleanup/": "Cleanup",
    "/imagine-references/": "Image references",
}

MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


# ---------------------------------------------------------------- dates


def git_date(path: Path) -> datetime:
    """The UTC date of the last commit that touched `path`."""
    out = subprocess.run(
        ["git", "log", "-1", "--format=%cI", "--", path.name],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not out:
        raise SystemExit(f"build_site_extras: {path} has no git history to date it from")
    return parse_date(out)


def parse_date(value: str) -> datetime:
    """Parse git's strict ISO-8601 output and convert it to UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


def human(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    return f"{MONTHS[when.month - 1]} {when.day}, {when.year}"


def updated_line(when: datetime) -> str:
    return (
        f'<p class="updated">Updated <time datetime="{iso_utc(when)}">'
        f"{human(when)}</time></p>"
    )


# ---------------------------------------------------------------- HTML tree

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children: list = []  # Node or str

    def classes(self) -> set:
        return set(self.attrs.get("class", "").split())

    def find(self, tag: str) -> "Node | None":
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    return child
                found = child.find(tag)
                if found is not None:
                    return found
        return None

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(text: str) -> Node:
    builder = TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


# ---------------------------------------------------------------- Markdown

BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "ul", "ol", "table",
         "div", "section", "article", "blockquote", "main", "aside", "hr"}
ESCAPE = re.compile(r"([\\`*\[\]<])")


def escape(text: str) -> str:
    return ESCAPE.sub(r"\\\1", text)


def code_span(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


class Renderer:
    def __init__(self, page_url: str):
        self.page_url = page_url

    def absolute(self, href: str) -> str:
        return urljoin(self.page_url, href)

    # inline ------------------------------------------------------------

    def inline(self, node: Node) -> str:
        parts = []
        for child in node.children:
            if isinstance(child, str):
                parts.append(escape(re.sub(r"\s+", " ", child)))
                continue
            tag = child.tag
            if tag == "code":
                parts.append(code_span(child.text()))
            elif tag in ("strong", "b"):
                inner = self.inline(child).strip()
                parts.append(f"**{inner}**" if inner else "")
            elif tag in ("em", "i"):
                inner = self.inline(child).strip()
                parts.append(f"*{inner}*" if inner else "")
            elif tag == "a":
                inner = self.inline(child).strip()
                href = child.attrs.get("href")
                parts.append(f"[{inner}]({self.absolute(href)})" if href else inner)
            elif tag == "br":
                parts.append(" ")
            elif tag == "img":
                alt = child.attrs.get("alt", "")
                if alt:
                    parts.append(f"![{escape(alt)}]({self.absolute(child.attrs.get('src', ''))})")
            elif tag == "span" and "pill" in child.classes():
                parts.append(f" ({self.inline(child).strip()})")
            else:
                parts.append(self.inline(child))
        return "".join(parts)

    def line(self, node: Node) -> str:
        return re.sub(r" {2,}", " ", self.inline(node)).strip()

    # blocks ------------------------------------------------------------

    def blocks(self, node: Node, depth: int = 0) -> list:
        out: list = []
        run = Node("#run")

        def flush():
            text = self.line(run)
            if text:
                out.append(text)
            run.children = []

        for child in node.children:
            if isinstance(child, Node) and child.tag in BLOCK:
                flush()
                out.extend(self.block(child, depth))
            else:
                run.children.append(child)
        flush()
        return out

    def block(self, node: Node, depth: int) -> list:
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.line(node)
            if tag == "h1":  # the twin's only h1 is the page title
                return [f"**{text}**"]
            return [f"{'#' * int(tag[1])} {text}"]
        if tag == "p":
            text = self.line(node)
            return [text] if text else []
        if tag == "pre":
            body = node.text().strip("\n")
            longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
            fence = "`" * max(3, longest + 1)
            return [f"{fence}\n{body}\n{fence}"]
        if tag in ("ul", "ol"):
            return ["\n".join(self.list_items(node, depth))]
        if tag == "table":
            return [self.table(node)]
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(node, depth))
            return ["\n".join("> " + l if l else ">" for l in inner.split("\n"))]
        if tag == "hr":
            return ["---"]
        return self.blocks(node, depth)

    def list_items(self, node: Node, depth: int) -> list:
        lines = []
        number = 0
        indent = "  " * depth
        for li in node.children:
            if not (isinstance(li, Node) and li.tag == "li"):
                continue
            number += 1
            marker = f"{number}." if node.tag == "ol" else "-"
            inline = Node("#li")
            nested = []
            for child in li.children:
                if isinstance(child, Node) and child.tag in ("ul", "ol"):
                    nested.append(child)
                elif isinstance(child, Node) and child.tag in BLOCK:
                    inline.children.extend([" "] + child.children + [" "])
                else:
                    inline.children.append(child)
            lines.append(f"{indent}{marker} {self.line(inline)}")
            for sub in nested:
                lines.extend(self.list_items(sub, depth + 1))
        return lines

    def table(self, node: Node) -> str:
        rows = []
        for tr in iter_tag(node, "tr"):
            cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("th", "td")]
            rows.append([self.line(c).replace("|", "\\|") for c in cells])
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)


def iter_tag(node: Node, tag: str):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                yield child
            else:
                yield from iter_tag(child, tag)


def page_info(text: str) -> dict:
    """Title, description and <main> node of an HTML page."""
    root = parse_html(text)
    title = root.find("title")
    main = root.find("main")
    description = ""
    for meta in iter_tag(root, "meta"):
        if meta.attrs.get("name") == "description":
            description = meta.attrs.get("content", "")
    if title is None or main is None or not description:
        raise SystemExit("build_site_extras: page lacks a <title>, <main> or meta description")
    return {"title": re.sub(r"\s+", " ", title.text()).strip(), "description": description, "main": main}


def twin_markdown(text: str, page_url: str, when: datetime) -> str:
    """The Markdown twin of an HTML page."""
    info = page_info(text)
    body = Renderer(page_url).blocks(info["main"])
    parts = [f"# {info['title']}", f"Updated: {day(when)}", *body, f"Canonical URL: {page_url}"]
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- HTML head


def twin_path(url: str) -> str:
    """Site path (no leading slash) of a page URL's Markdown twin."""
    return url.lstrip("/") + "index.md"


def json_ld(url: str, title: str, description: str, when: datetime) -> str:
    page_url = SITE_URL + url
    website = {"@type": "WebSite", "name": "Caret Docs", "url": SITE_URL + "/"}
    article = {
        "@type": "TechArticle",
        "headline": title,
        "description": description,
        "dateModified": iso_utc(when),
        "url": page_url,
        "inLanguage": "en",
        "publisher": {"@type": "Organization", "name": PUBLISHER},
        "isPartOf": website,
        "about": {"@type": "SoftwareApplication", "name": "Caret", "url": PRODUCT_URL},
    }
    graph = [article]
    if url == "/":
        graph.append({**website, "description": description, "publisher": article["publisher"]})
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1)
    return data.replace("</", "<\\/")


def decorate_html(text: str, url: str, when: datetime) -> str:
    """Add the head links, JSON-LD and the visible Updated line."""
    info = page_info(text)
    if 'rel="canonical"' in text or 'class="updated"' in text:
        raise SystemExit(f"build_site_extras: {url} is already decorated")
    head = (
        f'<link rel="canonical" href="{SITE_URL}{url}">\n'
        f'<link rel="alternate" type="text/markdown" href="{SITE_URL}/{twin_path(url)}">\n'
        f'<script type="application/ld+json">\n'
        f'{json_ld(url, info["title"], info["description"], when)}\n</script>\n'
    )
    if text.count("</head>") != 1 or text.count("</h1>") < 1:
        raise SystemExit(f"build_site_extras: {url} has no single </head> or no <h1>")
    text = text.replace("</head>", head + "</head>", 1)
    main_at = text.index("<main")
    h1_end = text.index("</h1>", main_at) + len("</h1>")
    return text[:h1_end] + "\n  " + updated_line(when) + text[h1_end:]


# ---------------------------------------------------------------- site files


def sitemap(dates: dict) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, when in dates.items():
        lines.append(f"  <url><loc>{SITE_URL}{url}</loc><lastmod>{iso_utc(when)}</lastmod></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def llms_txt(head: str, pages: list, instructions_date: datetime) -> str:
    """docs/llms.txt's hand-written head, then the generated page lists.

    `pages` is a list of (url, description, date) in sidebar order.
    """
    lines = [head.rstrip("\n"), "", "## Pages", ""]
    for url, description, when in pages:
        lines.append(
            f"- [{PAGE_NAMES[url]}]({SITE_URL}/{twin_path(url)}): "
            f"Updated {day(when)}. {description}"
        )
    lines.append(
        f"- [Agent setup instructions]({SITE_URL}/agent/instructions.md): "
        f"Updated {day(instructions_date)}. The plain-Markdown instructions a coding agent "
        "follows to set up the reference backend."
    )
    lines.append(f"- [Site map]({SITE_URL}/sitemap.xml)")
    lines += [
        "",
        "## Product pages",
        "",
        "- [Caret llms.txt](https://typewithcaret.com/llms.txt): the product site's guide for agents.",
        "- [Caret](https://typewithcaret.com/): the Caret product site.",
    ]
    return "\n".join(lines) + "\n"


def source_of(rel: str) -> Path:
    """The tracked file a published site path is copied from."""
    for candidate in (DOCS / rel, REPO_ROOT / rel):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"build_site_extras: no source file for {rel}")


def build(site: Path) -> None:
    dates = {}
    pages = []
    lastmod = {}
    for url, rel in docs_check.ACTIVE_PAGES.items():
        when = git_date(DOCS / rel)
        dates[url] = when
        source = (DOCS / rel).read_text(encoding="utf-8")
        info = page_info(source)
        pages.append((url, info["description"], when))
        (site / twin_path(url)).write_text(twin_markdown(source, SITE_URL + url, when), encoding="utf-8")
        (site / rel).write_text(decorate_html(source, url, when), encoding="utf-8")
        lastmod[rel] = when
        lastmod[twin_path(url)] = when

    for path in sorted(site.rglob("*.md")):
        rel = str(path.relative_to(site))
        if rel not in lastmod:
            lastmod[rel] = git_date(source_of(rel))

    newest = max(dates.values())
    head = (DOCS / "llms.txt").read_text(encoding="utf-8")
    (site / "llms.txt").write_text(llms_txt(head, pages, lastmod["agent/instructions.md"]), encoding="utf-8")
    (site / "sitemap.xml").write_text(sitemap(dates), encoding="utf-8")
    for rel in ("llms.txt", "sitemap.xml", "robots.txt"):
        if not (site / rel).is_file():
            raise SystemExit(f"build_site_extras: {site / rel} missing")
        lastmod[rel] = newest

    rows = "".join(f"{rel}\t{iso_utc(when)}\n" for rel, when in sorted(lastmod.items()))
    (site / ".lastmod.tsv").write_text(rows, encoding="utf-8")
    print(f"build_site_extras: {len(dates)} pages dated, twinned and described; {len(lastmod)} lastmod rows")


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: build_site_extras.py SITE_DIR", file=sys.stderr)
        return 2
    build(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
