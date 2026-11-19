"""Tests for the site extras: Markdown twins, page dates, head metadata."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_site_extras as extras

WHEN = datetime(2026, 11, 10, 6, 23, 45, tzinfo=timezone.utc)

PAGE = """<!doctype html>
<html lang="en">
<head>
<title>Test page &mdash; a title</title>
<meta name="description" content="What the page is about.">
</head>
<body>
<header class="site"><a href="/">Caret Docs</a></header>
<aside class="sidebar"><a href="/protocol/">Protocol</a></aside>
<main class="wrap" id="content">
  <p class="kicker">Kicker</p>
  <h1>The headline.</h1>
  <p class="lead">
    Read the <a href="/protocol/#7">protocol</a> or
    <a href="https://example.com/x">elsewhere</a>, then
    <a href="../agent/instructions.md">the instructions</a>.
  </p>
  <h2>A table</h2>
  <table>
    <tr><th>Route</th><th>Result</th></tr>
    <tr><td><code>/dictate</code> · required</td><td>a | b</td></tr>
  </table>
  <div class="card">
    <h3>Card<span class="pill">contract</span></h3>
    <pre><code>{"event": "partial",
  "text": "a &lt;b&gt; ``` c"}</code></pre>
  </div>
  <ol><li>One <strong>bold</strong></li><li>Two *stars*</li></ol>
</main>
<footer class="site">Footer text</footer>
</body>
</html>
"""


class TwinTests(unittest.TestCase):
    def setUp(self):
        self.md = extras.twin_markdown(PAGE, "https://docs.typewithcaret.com/cleanup/", WHEN)

    def test_frame(self):
        lines = self.md.splitlines()
        self.assertEqual(lines[0], "# Test page — a title")
        self.assertEqual(lines[2], "Updated: 2026-11-10")
        self.assertEqual(lines[-1], "Canonical URL: https://docs.typewithcaret.com/cleanup/")
        self.assertEqual(self.md.count("\n# "), 0, "the page title is the only h1")

    def test_main_only(self):
        self.assertNotIn("Footer text", self.md)
        self.assertNotIn("Caret Docs", self.md)
        self.assertIn("**The headline.**", self.md)

    def test_links_are_absolute(self):
        self.assertIn("[protocol](https://docs.typewithcaret.com/protocol/#7)", self.md)
        self.assertIn("[elsewhere](https://example.com/x)", self.md)
        self.assertIn("[the instructions](https://docs.typewithcaret.com/agent/instructions.md)", self.md)

    def test_table(self):
        self.assertIn(
            "| Route | Result |\n| --- | --- |\n| `/dictate` · required | a \\| b |",
            self.md,
        )

    def test_code_block_is_verbatim_with_a_longer_fence(self):
        self.assertIn(
            '````\n{"event": "partial",\n  "text": "a <b> ``` c"}\n````', self.md
        )

    def test_headings_lists_and_escaping(self):
        self.assertIn("### Card (contract)", self.md)
        self.assertIn("1. One **bold**\n2. Two \\*stars\\*", self.md)


class DateTests(unittest.TestCase):
    def test_git_offset_is_normalised_to_utc(self):
        when = extras.parse_date("2026-11-10T14:23:45+08:00")
        self.assertEqual(extras.iso_utc(when), "2026-11-10T06:23:45Z")

    def test_utc_conversion_can_change_the_day(self):
        when = extras.parse_date("2026-11-01T02:00:00+08:00")
        self.assertEqual(extras.day(when), "2026-10-31")
        self.assertEqual(extras.human(when), "October 31, 2026")

    def test_updated_line(self):
        self.assertEqual(
            extras.updated_line(WHEN),
            '<p class="updated">Updated <time datetime="2026-11-10T06:23:45Z">November 10, 2026</time></p>',
        )

    def test_git_date_is_the_last_commit_touching_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args, date=None):
                env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": tmp}
                if date:
                    env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                    cwd=repo, env=env, check=True, capture_output=True,
                )

            git("init", "-q")
            (repo / "page.html").write_text("one")
            git("add", "page.html")
            git("commit", "-qm", "one", date="2026-01-02T03:04:05+02:00")
            (repo / "other.html").write_text("other")
            git("add", "other.html")
            git("commit", "-qm", "other", date="2026-03-01T00:00:00+00:00")
            self.assertEqual(extras.iso_utc(extras.git_date(repo / "page.html")), "2026-01-02T01:04:05Z")
            self.assertEqual(extras.iso_utc(extras.git_date(repo / "other.html")), "2026-03-01T00:00:00Z")

    def test_undated_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(git + ["init", "-q"], cwd=tmp, check=True)
            (Path(tmp) / "old.html").write_text("x")
            subprocess.run(git + ["add", "old.html"], cwd=tmp, check=True)
            subprocess.run(git + ["commit", "-qm", "old"], cwd=tmp, check=True)
            (Path(tmp) / "new.html").write_text("x")
            with self.assertRaises(SystemExit):
                extras.git_date(Path(tmp) / "new.html")

    def test_one_date_everywhere(self):
        page = extras.decorate_html(PAGE, "/cleanup/", WHEN)
        stamp = "2026-11-10T06:23:45Z"
        self.assertIn(f'<time datetime="{stamp}">', page)
        ld = json.loads(re.search(r'ld\+json">\n(.*?)\n</script>', page, re.S).group(1))
        self.assertEqual(ld["@graph"][0]["dateModified"], stamp)
        self.assertIn(f"<lastmod>{stamp}</lastmod>", extras.sitemap({"/cleanup/": WHEN}))
        llms = extras.llms_txt("# Head\n", [("/cleanup/", "Desc.", WHEN)], WHEN)
        self.assertIn("(https://docs.typewithcaret.com/cleanup/index.md): Updated 2026-11-10. Desc.", llms)


class HeadTests(unittest.TestCase):
    def test_head_links_and_json_ld(self):
        page = extras.decorate_html(PAGE, "/cleanup/", WHEN)
        self.assertIn('<link rel="canonical" href="https://docs.typewithcaret.com/cleanup/">', page)
        self.assertIn(
            '<link rel="alternate" type="text/markdown" href="https://docs.typewithcaret.com/cleanup/index.md">',
            page,
        )
        ld = json.loads(re.search(r'ld\+json">\n(.*?)\n</script>', page, re.S).group(1))
        article = ld["@graph"][0]
        self.assertEqual(article["@type"], "TechArticle")
        self.assertEqual(article["headline"], "Test page — a title")
        self.assertEqual(article["description"], "What the page is about.")
        self.assertEqual(article["about"]["url"], "https://typewithcaret.com/")
        self.assertEqual(len(ld["@graph"]), 1)
        self.assertLess(page.index("</h1>"), page.index('class="updated"'))

    def test_home_page_is_also_a_website(self):
        page = extras.decorate_html(PAGE, "/", WHEN)
        self.assertIn('href="https://docs.typewithcaret.com/index.md"', page)
        ld = json.loads(re.search(r'ld\+json">\n(.*?)\n</script>', page, re.S).group(1))
        self.assertEqual([n["@type"] for n in ld["@graph"]], ["TechArticle", "WebSite"])


if __name__ == "__main__":
    unittest.main()
