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

    def test_redirect_stub_must_target_its_matching_archive_page(self):
        html = '<meta http-equiv="refresh" content="0; url=/legacy/live-dictation/">'
        findings = docs_check.check_stub_target(html, "connect/index.html")
        self.assertEqual(len(findings), 1)
        self.assertIn("/legacy/connect/", findings[0])

    def test_retired_url_is_flagged_on_active_pages(self):
        html = '<a href="/your-agent/">old guide</a>'
        findings = docs_check.check_retired_references(html, "index.html")
        self.assertEqual(len(findings), 1)

    def test_legacy_paths_are_not_retired(self):
        html = '<a href="/legacy/your-agent/">archived guide</a>'
        self.assertEqual(docs_check.check_retired_references(html, "index.html"), [])


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
        self.assertEqual(docs_check.main(), 0)


if __name__ == "__main__":
    unittest.main()
