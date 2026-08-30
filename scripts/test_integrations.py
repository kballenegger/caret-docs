"""Hermetic validation of the archived packaged integrations.

The integrations moved to legacy/ in the V4 cutover; archived means
frozen, not broken, so their manifests must still parse and stay
internally consistent, and skills must keep well-formed frontmatter.
Nothing here executes Claude Code or Hermes.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO_ROOT / "legacy" / "claude-plugin" / "marketplace.json"
CLAUDE_PLUGIN_DIR = REPO_ROOT / "legacy" / "integrations" / "claude-code" / "caret-connect"
HERMES_SKILL = REPO_ROOT / "legacy" / "integrations" / "hermes" / "caret-connect" / "SKILL.md"


def read_frontmatter(path: Path) -> dict:
    lines = path.read_text().splitlines()
    assert lines[0] == "---", f"{path} must start with YAML frontmatter"
    fields = {}
    for line in lines[1:]:
        if line == "---":
            return fields
        if line.startswith((" ", "\t")):  # folded continuation line
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    raise AssertionError(f"{path} frontmatter never closes")


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MARKETPLACE.read_text())

    def test_required_fields(self):
        self.assertEqual(self.manifest["name"], "caret-docs")
        self.assertIn("name", self.manifest["owner"])
        self.assertTrue(self.manifest["plugins"])

    def test_every_plugin_source_exists_and_is_a_plugin(self):
        for plugin in self.manifest["plugins"]:
            source = (MARKETPLACE.parent / plugin["source"]).resolve()
            self.assertTrue(source.is_dir(), f"missing plugin source {plugin['source']}")
            self.assertTrue((source / ".claude-plugin" / "plugin.json").is_file())


class ClaudePluginTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (CLAUDE_PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text()
        )

    def test_required_fields(self):
        self.assertEqual(self.manifest["name"], "caret-connect")
        self.assertIn("version", self.manifest)
        self.assertIn("description", self.manifest)

    def test_declared_skills_resolve_to_skill_files(self):
        for entry in self.manifest.get("skills", []):
            root = (CLAUDE_PLUGIN_DIR / entry).resolve()
            found = list(root.rglob("SKILL.md")) if root.is_dir() else []
            self.assertTrue(found, f"no SKILL.md under declared skills path {entry}")

    def test_marketplace_and_plugin_names_agree(self):
        marketplace = json.loads(MARKETPLACE.read_text())
        names = {p["name"] for p in marketplace["plugins"]}
        self.assertIn(self.manifest["name"], names)


class SkillTests(unittest.TestCase):
    SKILLS = (CLAUDE_PLUGIN_DIR / "SKILL.md", HERMES_SKILL)

    def test_frontmatter_has_name_and_trigger_description(self):
        for path in self.SKILLS:
            fields = read_frontmatter(path)
            self.assertEqual(fields.get("name"), "caret-connect", path)
            self.assertIn("Caret", fields.get("description", ""), path)

    def test_skills_deploy_the_one_reference_backend_not_a_fork(self):
        for path in self.SKILLS:
            body = path.read_text()
            self.assertIn("github.com/kballenegger/caret-docs", body, path)
            self.assertIn("caret_backend", body, path)
            self.assertIn("--check", body, path)

    def test_skills_never_suggest_permission_bypasses(self):
        for path in self.SKILLS:
            body = path.read_text()
            self.assertNotIn("--dangerously", body, path)
            self.assertNotIn("danger-full-access", body, path)


if __name__ == "__main__":
    unittest.main()
