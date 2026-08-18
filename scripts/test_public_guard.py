"""Tests for the public-repo guard. Standard library only."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import public_guard


class ScanTextTests(unittest.TestCase):
    def assert_flags(self, text: str) -> None:
        self.assertTrue(
            public_guard.scan_text(text, "x"), f"expected a finding in {text!r}"
        )

    def assert_clean(self, text: str) -> None:
        self.assertEqual(public_guard.scan_text(text, "x"), [])

    def test_flags_private_machine_username(self):
        self.assert_flags("path is /Users/" + "kenneth" + "-bot/somewhere")

    def test_flags_any_macos_home_path(self):
        self.assert_flags("copied from /Users" + "/alice/project/file.py")

    def test_flags_any_linux_home_path(self):
        self.assert_flags("see /home" + "/deploy/app for details")

    def test_flags_private_identifiers_case_insensitively(self):
        for ident in public_guard.PRIVATE_IDENTIFIERS:
            self.assert_flags(f"mentions {ident.upper()} here")

    def test_flags_private_backend_checkout_coupling(self):
        self.assert_flags("cp ~/car" + "et/bridge/file.py .")
        self.assert_flags('SRC="${CARET_' + 'REPO}/bridge"')

    def test_flags_credential_shaped_strings(self):
        self.assert_flags("key = sk-ant-" + "a1b2c3d4e5f6g7h8")
        self.assert_flags("aws AKIA" + "ABCDEFGHIJKLMNOP")
        self.assert_flags("gh ghp_" + "abcdefghij0123456789")
        self.assert_flags("-----BEGIN RSA " + "PRIVATE KEY-----")

    def test_clean_public_content_passes(self):
        self.assert_clean("curl -fsSLO https://docs.typewithcaret.com/x.py")
        self.assert_clean("export CARET_AGENT=claude-code")
        self.assert_clean("generate with secrets.token_urlsafe(32)")
        self.assert_clean("hermes chat -q '{prompt}' -Q")
        self.assert_clean("stored client-side in the iOS Keychain")
        self.assert_clean("~/caret-docs is fine: it is this repository")

    def test_reports_file_and_line(self):
        findings = public_guard.scan_text("ok\nsee /Users" + "/alice/x\n", "docs/a.md")
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].startswith("docs/a.md:2:"))


class RepoScanTests(unittest.TestCase):
    def test_tracked_files_are_discovered(self):
        files = public_guard.tracked_files()
        names = {p.name for p in files}
        self.assertIn("openapi.yaml", names)
        self.assertIn("adapters.py", names)

    def test_the_repository_itself_is_clean(self):
        findings = []
        for path in public_guard.tracked_files():
            findings.extend(public_guard.scan_file(path))
        self.assertEqual(findings, [])

    def test_guard_does_not_match_its_own_definitions(self):
        guard_path = Path(public_guard.__file__)
        self.assertEqual(public_guard.scan_file(guard_path), [])


if __name__ == "__main__":
    unittest.main()
