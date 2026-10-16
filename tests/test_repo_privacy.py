"""Privacy guard regressions; runnable without Django or third-party packages."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.check_repo_privacy import content_issues, private_path


class PrivacyRulesTests(unittest.TestCase):
    def test_blocks_private_artifacts_but_keeps_application_code(self):
        for path in [
            ".env",
            ".env.production",
            "docs/АУДИТ-follow-up.md",
            "docs/audit.md",
            ".playwright-mcp/page.yml",
            "clients.xlsx",
            "db.sqlite3-wal",
            "backups/2026.dump",
            "media/photo.jpg",
            "secrets/age_identity.txt",
            "CLAUDE.md",
            "exports/clients.json",
        ]:
            with self.subTest(path=path):
                self.assertTrue(private_path(path))
        for path in [
            ".env.example",
            "README.md",
            "docs/catalog_import_example.csv",
            "apps/sales/management/commands/audit_stale_totals.py",
            "apps/reports/export.py",
            "static/pos/js/sw-manifest.json",
        ]:
            with self.subTest(path=path):
                self.assertFalse(private_path(path))

    def test_emails_allow_placeholders_and_noreply(self):
        self.assertTrue(content_issues(b"person@" + b"gmail.com"))
        self.assertFalse(content_issues(b"person@example.com"))
        self.assertFalse(content_issues(b"138603985+nurd11n@users.noreply.github.com"))
        self.assertFalse(content_issues(b"test@example.test"))

    def test_credentials_are_detected_without_returning_values(self):
        token = b"ghp_" + b"X" * 36
        self.assertEqual(content_issues(token), ["GitHub token"])
        secret = b"local-private-" + b"value-123456"
        self.assertEqual(content_issues(secret, [secret]), ["local credential value"])
        self.assertFalse(content_issues(b"AGE-SECRET-KEY-1..."))

    def test_staged_content_and_old_history_cannot_hide_behind_worktree(self):
        script = Path(__file__).resolve().parents[1] / "scripts/check_repo_privacy.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

            def check(*args):
                return subprocess.run(
                    [sys.executable, str(script), *args], cwd=root, capture_output=True
                )

            git("init", "-q")
            git("config", "user.name", "Test Contributor")
            git("config", "user.email", "138603985+nurd11n@users.noreply.github.com")
            (root / "code.txt").write_bytes(b"person@" + b"gmail.com")
            git("add", "code.txt")
            (root / "code.txt").write_text("safe worktree content")
            result = check("--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"person@", result.stdout)
            git("commit", "-qm", "Initial version")
            git("add", "code.txt")
            git("commit", "-qm", "Remove private content")
            self.assertEqual(check().returncode, 0)
            self.assertNotEqual(check("--history").returncode, 0)


if __name__ == "__main__":
    unittest.main()
