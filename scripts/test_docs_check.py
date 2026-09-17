"""Tests for the docs structure guard. Standard library only."""
from __future__ import annotations

import sys
import tempfile
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
            docs_check.resolve("/spec/cleanup/v1/prompt.md", "cleanup/index.html"),
            "spec/cleanup/v1/prompt.md",
        )

    def test_root_link(self):
        self.assertEqual(docs_check.resolve("/", "reference/index.html"), "index.html")


class CheckTests(unittest.TestCase):
    def test_broken_link_is_flagged(self):
        html = '<a href="/nowhere/">x</a>'
        findings = docs_check.check_links(html, "index.html", {"index.html"})
        self.assertEqual(len(findings), 1)
        self.assertIn("/nowhere/", findings[0])

    def test_retired_paths_are_recognized(self):
        for path in ("hosted/index.html", "migration/index.html", "legacy/index.html",
                     "your-agent/index.html", "live-dictation/index.html",
                     "connect/hermes/index.html", "openapi.yaml",
                     "agent-prompts/connect-caret.md",
                     "integrations/hermes/caret-connect/SKILL.md"):
            with self.subTest(path=path):
                self.assertTrue(docs_check.is_retired(path))
        for path in docs_check.ACTIVE_PAGES.values():
            with self.subTest(path=path):
                self.assertFalse(docs_check.is_retired(path))

    def test_link_to_a_retired_url_is_flagged(self):
        for link in ("/hosted/", "/migration/", "/legacy/", "/your-agent/",
                     "/legacy/your-agent/", "/openapi.yaml",
                     "/agent-prompts/connect-caret.md",
                     "/integrations/hermes/caret-connect/SKILL.md"):
            with self.subTest(link=link):
                html = f'<a href="{link}">old</a>'
                findings = docs_check.check_retired_references(html, "index.html")
                self.assertEqual(len(findings), 1)

    def test_hosted_api_and_retirement_text_are_flagged(self):
        for needle in ("api.typewithcaret.com", "/v2/dictionary", "/v4/dictate",
                       "ARCHIVED", "<!-- redirect-stub -->", "retirement notice"):
            with self.subTest(needle=needle):
                findings = docs_check.check_forbidden_text(f"<p>{needle}</p>", "index.html")
                self.assertEqual(len(findings), 1)
        self.assertEqual(docs_check.check_forbidden_text("<p>caret/v4</p>", "index.html"), [])

    def test_assembled_site_rejects_retired_paths_and_hosted_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "legacy").mkdir()
            (site / "legacy" / "index.html").write_text("<p>retired</p>")
            (site / "hosted").mkdir()
            (site / "hosted" / "index.html").write_text("<p>hosted</p>")
            (site / "protocol").mkdir()
            (site / "protocol" / "index.html").write_text(
                '<p>ARCHIVED banner</p><a href="/migration/">old</a>'
            )
            (site / "reference").mkdir()
            (site / "reference" / "index.html").write_text("<p>fine</p>")
            findings = docs_check.check_site(site)
        self.assertEqual(len(findings), 4)
        self.assertTrue(any("legacy/index.html" in f for f in findings))
        self.assertTrue(any("hosted/index.html" in f for f in findings))
        self.assertTrue(any("ARCHIVED" in f for f in findings))
        self.assertTrue(any("/migration/" in f for f in findings))

    def test_assembled_site_checks_published_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "agent").mkdir()
            (site / "agent" / "instructions.md").write_text(
                "Do not use api.typewithcaret.com."
            )
            findings = docs_check.check_site(site)
        self.assertEqual(len(findings), 1)
        self.assertIn("agent/instructions.md", findings[0])
        self.assertIn("api.typewithcaret.com", findings[0])

    def test_published_markdown_cannot_link_to_retired_urls(self):
        markdown = "See the [old guide](/legacy/)."
        findings = docs_check.check_retired_references(
            markdown, "agent/instructions.md"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("/legacy/", findings[0])

    def test_assembled_site_rejects_broken_published_markdown_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "agent").mkdir()
            (site / "agent" / "instructions.md").write_text(
                "See the [missing guide](/not-published/)."
            )
            findings = docs_check.check_site(site)
        self.assertEqual(len(findings), 1)
        self.assertIn("broken internal link", findings[0])
        self.assertIn("/not-published/", findings[0])


class SharedShellTests(unittest.TestCase):
    """Every page carries the same header and sidebar."""

    def _html(self, rel):
        return (docs_check.DOCS / rel).read_text(encoding="utf-8")

    def test_every_page_marks_itself_current_and_matches_the_overview(self):
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
        self.assertNotIn("Hosted", sidebar)
        self.assertNotIn("Migration", sidebar)

    def test_wrong_current_link_is_flagged(self):
        html = self._html("reference/index.html")
        canonical = docs_check.sidebar_of(html).replace(' aria-current="page"', "")
        findings = docs_check.check_shell(html, "reference/index.html", "/protocol/", canonical)
        self.assertTrue(any("current" in f for f in findings))

    def test_sidebar_with_retired_link_is_flagged(self):
        html = self._html("index.html").replace('<a href="/protocol/">', '<a href="/hosted/">', 1)
        canonical = docs_check.sidebar_of(html).replace(' aria-current="page"', "")
        findings = docs_check.check_shell(html, "index.html", "/", canonical)
        self.assertTrue(any("non-current" in f for f in findings))

    def test_header_links_back_to_the_main_site(self):
        for rel in docs_check.ACTIVE_PAGES.values():
            header = docs_check.HEADER.search(self._html(rel)).group(0)
            self.assertIn('href="https://typewithcaret.com"', header)
            self.assertIn('aria-expanded="false"', header)


class NavigationAssetTests(unittest.TestCase):
    css = (docs_check.DOCS / "styles.css").read_text(encoding="utf-8")
    js = (docs_check.DOCS / "nav.js").read_text(encoding="utf-8")
    overview = (docs_check.DOCS / "index.html").read_text(encoding="utf-8")

    def test_mobile_menu_has_a_script_free_fallback_and_no_horizontal_overflow(self):
        self.assertIn("@media (max-width: 52rem)", self.css)
        self.assertIn(".js .sidebar { display: none; }", self.css)
        self.assertIn(".js .nav-open .sidebar { display: block; }", self.css)
        self.assertIn("table { display: block; overflow-x: auto; }", self.css)
        self.assertIn("minmax(0, 1fr)", self.css)

    def test_menu_script_covers_keyboard_and_focus_behavior(self):
        self.assertIn('aria-controls="site-nav"', self.overview)
        self.assertIn('aria-expanded', self.js)
        self.assertIn('event.key === "Escape"', self.js)
        self.assertIn("first.focus()", self.js)
        self.assertIn("button.focus()", self.js)
        self.assertIn('root.classList.add("js")', self.js)


class AgentInstructionsTests(unittest.TestCase):
    """The Markdown a person hands their coding agent."""

    md = (docs_check.DOCS / "agent/instructions.md").read_text(encoding="utf-8")
    landing = (docs_check.DOCS / "agent/index.html").read_text(encoding="utf-8")

    def test_published_and_linked_from_the_landing_page(self):
        self.assertIn("agent/instructions.md", docs_check.REQUIRED_PAGES)
        self.assertIn('href="/agent/instructions.md"', self.landing)
        self.assertIn("https://docs.typewithcaret.com/agent/instructions.md", self.md)

    def test_points_at_the_current_spec_and_reference(self):
        self.assertIn("https://docs.typewithcaret.com/protocol/", self.md)
        self.assertIn("github.com/kballenegger/caret-docs/tree/main/reference", self.md)
        for section in ("Ask before you build", "Loopback is not transcription",
                        "Protect secrets", "Verify before you say it works"):
            self.assertIn(section, self.md)

    def test_every_command_names_a_path_that_exists(self):
        for rel in ("reference/go/cmd/caret-v4-backend", "reference/go/cmd/caret-v4-conform",
                    "reference/python/caret_v4/__main__.py", "spec/cleanup/v1/manifest.json"):
            self.assertTrue((docs_check.REPO_ROOT / rel).exists(), rel)
        for flag in ("-keys", "-stt", "-agent", "-cleanup", "-tls-cert", "-cleanup-spec-dir"):
            self.assertIn(flag, self.md)
        for flag in ("--keys", "--stt", "--tls-cert", "--insecure-skip-verify"):
            self.assertIn(flag, self.md)

    def test_no_hosted_or_old_version_material(self):
        self.assertEqual(docs_check.check_forbidden_text(self.md, "agent/instructions.md"), [])
        for needle in ("caret/v2", "caret/v3", "/hosted/", "/migration/", "/legacy/"):
            self.assertNotIn(needle, self.md)
        self.assertIn("not a developer API", self.md)


class RepoTests(unittest.TestCase):
    def test_the_docs_tree_passes(self):
        self.assertEqual(docs_check.main([]), 0)

    def test_only_the_current_pages_exist_under_docs(self):
        pages = sorted(str(p.relative_to(docs_check.DOCS)) for p in docs_check.DOCS.rglob("*.html"))
        self.assertEqual(pages, sorted(docs_check.ACTIVE_PAGES.values()))

    def test_no_retired_path_exists_under_docs(self):
        for path in docs_check.DOCS.rglob("*"):
            rel = str(path.relative_to(docs_check.DOCS))
            self.assertFalse(docs_check.is_retired(rel), rel)

    def test_no_page_documents_the_hosted_service_or_old_versions(self):
        for rel in docs_check.ACTIVE_PAGES.values():
            text = (docs_check.DOCS / rel).read_text(encoding="utf-8")
            with self.subTest(page=rel):
                self.assertEqual(docs_check.check_forbidden_text(text, rel), [])
                self.assertNotIn("caret/v2", text)
                self.assertNotIn("caret/v3", text)
                self.assertNotIn('"contract"', text)

    def test_overview_does_not_present_hosted_dictation_as_a_developer_api(self):
        text = (docs_check.DOCS / "index.html").read_text(encoding="utf-8")
        self.assertIn("Not a developer API", text)
        self.assertNotIn("typewithcaret.com/signup", text)

    def test_readme_matches_the_published_set(self):
        readme = (docs_check.REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for fragment in docs_check.README_FORBIDDEN:
            self.assertNotIn(fragment, readme)


if __name__ == "__main__":
    unittest.main()
