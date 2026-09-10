from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from boss_hire.boss_access import (
    BossAccessPolicy,
    BossLiveAccessDenied,
    preflight_boss_access,
)


class BossAccessPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 28, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.policy = BossAccessPolicy.balanced()

    def test_default_mode_is_frozen_without_confirmation(self) -> None:
        result = preflight_boss_access(
            live=False,
            confirm_live=None,
            auth_dir=Path("data/local/boss_agent_cli_auth"),
            operation_count=999,
            now=self.now,
            policy=self.policy,
        )

        self.assertEqual(result.mode, "frozen")
        self.assertFalse(result.network_allowed)
        self.assertEqual(result.operation_count, 0)

    def test_live_requires_shanghai_calendar_date(self) -> None:
        for confirmation in (None, "2026-08-27", "2026-08-29"):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(BossLiveAccessDenied, "--confirm-live 2026-08-28"):
                    preflight_boss_access(
                        live=True,
                        confirm_live=confirmation,
                        auth_dir=Path("data/local/boss_agent_cli_auth"),
                        operation_count=20,
                        now=self.now,
                        policy=self.policy,
                    )

    def test_live_date_uses_asia_shanghai_not_utc(self) -> None:
        utc_now = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)

        result = preflight_boss_access(
            live=True,
            confirm_live="2026-08-28",
            auth_dir=Path("data/local/boss_agent_cli_auth"),
            operation_count=20,
            now=utc_now,
            policy=self.policy,
        )

        self.assertTrue(result.network_allowed)
        self.assertEqual(result.confirmed_local_date, "2026-08-28")

    def test_live_accepts_large_exact_operation_manifest_without_numeric_ceiling(self) -> None:
        result = preflight_boss_access(
            live=True,
            confirm_live="2026-08-28",
            auth_dir=Path("data/local/boss_agent_cli_auth"),
            operation_count=137,
            now=self.now,
            policy=self.policy,
        )

        self.assertEqual(result.operation_count, 137)

    def test_live_receipt_uses_auth_path_digest_not_raw_path(self) -> None:
        auth_dir = Path("data/local/private-account-name")
        result = preflight_boss_access(
            live=True,
            confirm_live="2026-08-28",
            auth_dir=auth_dir,
            operation_count=137,
            now=self.now,
            policy=self.policy,
        )

        self.assertEqual(len(result.account_key), 16)
        self.assertNotIn("private-account-name", result.account_key)
        self.assertNotIn(str(auth_dir), result.summary())
        self.assertIn("operations=137", result.summary())
        self.assertNotIn("/40", result.summary())

    def test_balanced_policy_keeps_minimum_intervals_and_adds_random_jitter(self) -> None:
        policy = BossAccessPolicy.balanced()

        self.assertEqual(policy.list_interval_seconds, 6.0)
        self.assertEqual(policy.detail_interval_seconds, 15.0)
        self.assertEqual(policy.list_jitter_seconds, (1.0, 4.0))
        self.assertEqual(policy.detail_jitter_seconds, (2.0, 10.0))

    def test_policy_rejects_invalid_jitter_ranges(self) -> None:
        for list_jitter, detail_jitter in (
            ((-1.0, 2.0), (0.0, 0.0)),
            ((3.0, 1.0), (0.0, 0.0)),
            ((0.0, 0.0), (-1.0, 2.0)),
            ((0.0, 0.0), (3.0, 1.0)),
        ):
            with self.subTest(list_jitter=list_jitter, detail_jitter=detail_jitter):
                with self.assertRaisesRegex(ValueError, "jitter"):
                    BossAccessPolicy(
                        list_interval_seconds=6,
                        detail_interval_seconds=15,
                        list_jitter_seconds=list_jitter,
                        detail_jitter_seconds=detail_jitter,
                    )


if __name__ == "__main__":
    unittest.main()
