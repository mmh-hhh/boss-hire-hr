from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionContractTests(unittest.TestCase):
    def test_public_snapshot_contains_only_supported_entrypoints(self) -> None:
        manifest_path = ROOT / "DISTRIBUTION_MANIFEST.json"
        if not manifest_path.is_file():
            self.skipTest("internal source tree")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["license_policy"], "no-license-file-owner-decision")
        self.assertFalse((ROOT / "LICENSE").exists())
        self.assertFalse((ROOT / "experiments").exists())
        self.assertFalse((ROOT / "plans").exists())
        self.assertEqual(
            {path.name for path in (ROOT / "scripts").glob("*.py")},
            {
                "run_offline_tests.py",
                "run_single_job_live.py",
                "search_filters.py",
                "secure_local_boss_data.py",
                "setup_hr.py",
            },
        )
        for old_module in (
            "daily_board.py",
            "daily_supply_schedule.py",
            "legacy_recruiter_favorites.py",
            "llm_experiment.py",
            "offline_daily_replay.py",
            "precise_supply.py",
            "screener.py",
            "supply_audit.py",
            "supply_calibration.py",
            "supply_history.py",
            "supply_mvp.py",
            "workbench.py",
            "workbench_events.py",
        ):
            self.assertFalse((ROOT / "boss_hire" / old_module).exists(), old_module)

    def test_public_readme_is_agent_first_and_points_to_the_main_flow(self) -> None:
        manifest_path = ROOT / "DISTRIBUTION_MANIFEST.json"
        if not manifest_path.is_file():
            self.skipTest("internal source tree")

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("直接交给 coding agent 的提示词", readme)
        self.assertIn("mmh-hhh/boss-hire-hr", readme)
        self.assertIn("start --config smoke", readme)
        self.assertIn("本仓库有意不包含 `LICENSE`", readme)


if __name__ == "__main__":
    unittest.main()
