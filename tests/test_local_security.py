from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from boss_hire.local_security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    apply_private_umask,
    atomic_write_json,
    atomic_write_text,
)
from boss_hire.state_store import StateStore, jd_hash


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class LocalSecurityTests(unittest.TestCase):
    def test_private_umask_applies_to_plain_new_files_and_directories(self) -> None:
        previous = os.umask(0)
        try:
            apply_private_umask()
            with tempfile.TemporaryDirectory(prefix="boss_hire_permissions_") as temporary:
                root = Path(temporary)
                directory = root / "plain"
                directory.mkdir()
                path = directory / "plain.txt"
                path.write_text("secret", encoding="utf-8")
                self.assertEqual(mode(directory), PRIVATE_DIRECTORY_MODE)
                self.assertEqual(mode(path), PRIVATE_FILE_MODE)
        finally:
            os.umask(previous)

    def test_atomic_writers_force_private_modes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="boss_hire_atomic_") as temporary:
            root = Path(temporary)
            text_path = root / "nested" / "report.md"
            json_path = root / "nested" / "state.json"
            atomic_write_text(text_path, "private")
            atomic_write_json(json_path, {"value": 1})

            self.assertEqual(mode(text_path.parent), PRIVATE_DIRECTORY_MODE)
            self.assertEqual(mode(text_path), PRIVATE_FILE_MODE)
            self.assertEqual(mode(json_path), PRIVATE_FILE_MODE)
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), {"value": 1})
            self.assertFalse(text_path.with_suffix(".md.tmp").exists())
            self.assertFalse(json_path.with_suffix(".json.tmp").exists())

    def test_state_store_uses_private_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="boss_hire_state_permissions_") as temporary:
            path = Path(temporary) / "local" / "state.json"
            state = StateStore(path)
            state.record_job("job", jd_hash("JD"))
            state.save()

            self.assertEqual(mode(path.parent), PRIVATE_DIRECTORY_MODE)
            self.assertEqual(mode(path), PRIVATE_FILE_MODE)


if __name__ == "__main__":
    unittest.main()
