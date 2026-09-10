from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.secure_local_boss_data import secure_and_inventory


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class SecureLocalBossDataTests(unittest.TestCase):
    def test_secures_tree_skips_symlinks_and_writes_anonymous_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="boss_hire_inventory_") as temporary:
            base = Path(temporary)
            root = base / "data" / "local"
            auth = root / "boss_agent_cli_auth"
            resumes = root / "run" / "resumes"
            auth.mkdir(parents=True)
            resumes.mkdir(parents=True)
            token = auth / "token.json"
            resume = resumes / "candidate-secret.json"
            token.write_text("{}", encoding="utf-8")
            resume.write_text("{}", encoding="utf-8")
            auth.chmod(0o755)
            resumes.chmod(0o755)
            token.chmod(0o644)
            resume.chmod(0o644)

            outside = base / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            outside.chmod(0o644)
            link = root / "outside-link"
            os.symlink(outside, link)

            report_json = root / "security_inventory.json"
            report_markdown = root / "security_inventory.md"
            inventory = secure_and_inventory(root, report_json, report_markdown)

            self.assertEqual(mode(root), 0o700)
            self.assertEqual(mode(auth), 0o700)
            self.assertEqual(mode(resumes), 0o700)
            self.assertEqual(mode(token), 0o600)
            self.assertEqual(mode(resume), 0o600)
            self.assertEqual(mode(outside), 0o644)
            self.assertTrue(link.is_symlink())
            self.assertEqual(inventory["deletions_performed"], 0)
            self.assertGreaterEqual(inventory["permission_changes"]["symlinks_skipped"], 1)
            self.assertGreaterEqual(inventory["categories"]["auth"]["files"], 1)
            self.assertGreaterEqual(inventory["categories"]["resume"]["files"], 1)
            self.assertNotIn("candidate-secret", report_json.read_text(encoding="utf-8"))
            self.assertFalse(inventory["contains_file_names"])
            self.assertEqual(mode(report_json), 0o600)
            self.assertEqual(mode(report_markdown), 0o600)
            self.assertEqual(json.loads(report_json.read_text(encoding="utf-8"))["deletions_performed"], 0)


if __name__ == "__main__":
    unittest.main()
