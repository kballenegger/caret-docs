"""Tests for the docs structure guard. Standard library only."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docs_check


class ResolveTests(unittest.TestCase):
    def test_external_links_are_skipped(self):
        self.assertIsNone(docs_check.resolve("https://example.com/x", "index.html"))
        self.assertIsNone(docs_check.resolve("mailto:a@b.c", "index.html"))

    def test_absolute_directory_link(self):
        self.assertEqual(
            docs_check.resolve("/protocol/", "cleanup/index.html"),
            "protocol/index.html",
        )

    def test_absolute_file_link(self):
        self.assertEqual(
            docs_check.resolve("/legacy/openapi.yaml", "legacy/your-agent/index.html"),
            "legacy/openapi.yaml",
        )

    def test_root_link(self):
        self.assertEqual(docs_check.resolve("/", "migration/index.html"), "index.html")


class CheckTests(unittest.TestCase):
    def test_broken_link_is_flagged(self):
        html = '<a href="/nowhere/">x</a>'
        findings = docs_check.check_links(html, "index.html", {"index.html"})
        self.assertEqual(len(findings), 1)
        self.assertIn("/nowhere/", findings[0])

    def test_redirect_stub_must_target_the_retirement_notice(self):
        html = '<meta http-equiv="refresh" content="0; url=/legacy/connect/">'
        findings = docs_check.check_stub_target(html, "connect/index.html")
        self.assertEqual(len(findings), 1)
        self.assertIn("/legacy/", findings[0])
        ok = '<meta http-equiv="refresh" content="0; url=/legacy/">'
        self.assertEqual(docs_check.check_stub_target(ok, "connect/index.html"), [])

    def test_retired_url_is_flagged_on_active_pages(self):
        for link in ("/your-agent/", "/legacy/your-agent/", "/openapi.yaml",
                     "/agent-prompts/connect-caret.md",
                     "/integrations/hermes/caret-connect/SKILL.md"):
            with self.subTest(link=link):
                html = f'<a href="{link}">old</a>'
                findings = docs_check.check_retired_references(html, "index.html")
                self.assertEqual(len(findings), 1)

    def test_the_notice_itself_is_not_retired(self):
        html = '<a href="/legacy/">retired</a>'
        self.assertEqual(docs_check.check_retired_references(html, "index.html"), [])

    def test_retired_path_accepts_only_stubs_and_notices(self):
        page = "<!doctype html><!-- redirect-stub --><meta http-equiv=\"refresh\" content=\"0; url=/legacy/\">"
        self.assertEqual(docs_check.check_retired_file(page, "connect/index.html"), [])
        self.assertEqual(len(docs_check.check_retired_file("<h1>Runbook</h1>", "connect/index.html")), 1)
        self.assertEqual(docs_check.check_retired_file("# Retired\ncaret-docs: retired\n", "openapi.yaml"), [])
        self.assertEqual(len(docs_check.check_retired_file("openapi: 3.0.0\n", "openapi.yaml")), 1)

    def test_assembled_site_rejects_archived_content(self):
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as tmp:
            site = P(tmp)
            (site / "legacy" / "your-agent").mkdir(parents=True)
            (site / "legacy" / "your-agent" / "index.html").write_text("<p>ARCHIVED guide</p>")
            (site / "protocol").mkdir()
            (site / "protocol" / "index.html").write_text("<p>ARCHIVED banner on an active page</p>")
            findings = docs_check.check_site(site)
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("your-agent" in f for f in findings))
        self.assertTrue(any("protocol" in f for f in findings))


class SharedShellTests(unittest.TestCase):
    """Every active page carries the same header and sidebar."""

    def _html(self, rel):
        return (docs_check.DOCS / rel).read_text(encoding="utf-8")

    def test_every_active_page_marks_itself_current_and_matches_the_overview(self):
        canonical = docs_check.sidebar_of(self._html("index.html")).replace(' aria-current="page"', "")
        for url, rel in docs_check.ACTIVE_PAGES.items():
            with self.subTest(page=rel):
                html = self._html(rel)
                self.assertEqual(docs_check.check_shell(html, rel, url, canonical), [])
                self.assertEqual(html.count('aria-current="page"'), 1)

    def test_sidebar_lists_current_docs_only(self):
        sidebar = docs_check.sidebar_of(self._html("index.html"))
        links = docs_check.HREF.findall(sidebar)
        self.assertEqual(sorted(links), sorted(docs_check.ACTIVE_PAGES))

    def test_wrong_current_link_is_flagged(self):
        html = self._html("hosted/index.html")
        canonical = docs_check.sidebar_of(html).replace(' aria-current="page"', "")
        findings = docs_check.check_shell(html, "hosted/index.html", "/protocol/", canonical)
        self.assertTrue(any("current" in f for f in findings))

    def test_sidebar_with_retired_link_is_flagged(self):
        html = self._html("index.html").replace('<a href="/protocol/">', '<a href="/legacy/your-agent/">', 1)
        canonical = docs_check.sidebar_of(html).replace(' aria-current="page"', "")
        findings = docs_check.check_shell(html, "index.html", "/", canonical)
        self.assertTrue(any("retired" in f for f in findings))

    def test_header_links_back_to_the_main_site(self):
        for rel in docs_check.ACTIVE_PAGES.values():
            header = docs_check.HEADER.search(self._html(rel)).group(0)
            self.assertIn('href="https://typewithcaret.com"', header)
            self.assertIn('aria-expanded="false"', header)

    def test_notice_page_shares_the_shell_without_a_current_link(self):
        html = self._html(docs_check.NOTICE_PAGE)
        self.assertIn(docs_check.NOTICE_PAGE_MARKER, html)
        self.assertEqual(html.count('aria-current="page"'), 0)
        canonical = docs_check.sidebar_of(self._html("index.html")).replace(' aria-current="page"', "")
        self.assertEqual(docs_check.sidebar_of(html), canonical)


class HostedPageTests(unittest.TestCase):
    page = docs_check.DOCS / "hosted/index.html"

    def test_hosted_page_is_required_and_present(self):
        self.assertIn("hosted/index.html", docs_check.REQUIRED_PAGES)
        self.assertTrue(self.page.is_file())

    def test_hosted_page_names_both_contract_discriminators(self):
        html = self.page.read_text(encoding="utf-8")
        self.assertIn('"contract": "caret/v4"', html)
        self.assertIn('"protocol": "caret/v4"', html)
        self.assertIn("/v4/dictate", html)
        self.assertIn("/dictate", html)

    def test_hosted_page_documents_current_auth_and_fallback(self):
        html = " ".join(self.page.read_text(encoding="utf-8").split())
        self.assertIn("Authorization: Bearer", html)
        self.assertIn("No query parameter", html)
        self.assertIn("no WebSocket subprotocol", html)
        self.assertIn("no signed proof", html)
        self.assertIn("Unknown codes close with 4500", html)
        self.assertIn("524 288 bytes", html)


class RepoTests(unittest.TestCase):
    def test_the_docs_tree_passes(self):
        self.assertEqual(docs_check.main([]), 0)

    def test_no_archived_pages_remain_under_docs(self):
        for page in docs_check.DOCS.rglob("*.html"):
            rel = str(page.relative_to(docs_check.DOCS))
            text = page.read_text(encoding="utf-8")
            if docs_check.is_retired(rel):
                self.assertIn(docs_check.STUB_MARKER, text, rel)
            else:
                self.assertNotIn("ARCHIVED —", text, rel)

    def test_retired_prompt_and_schema_paths_are_notices(self):
        for rel in ("openapi.yaml", "legacy/openapi.yaml",
                    "agent-prompts/connect-caret.md",
                    "integrations/hermes/caret-connect/SKILL.md"):
            text = (docs_check.DOCS / rel).read_text(encoding="utf-8")
            self.assertIn(docs_check.NOTICE_MARKER, text, rel)
            self.assertLess(len(text.splitlines()), 15, rel)


if __name__ == "__main__":
    unittest.main()
