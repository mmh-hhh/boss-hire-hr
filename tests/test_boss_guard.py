from __future__ import annotations

import tempfile
import unittest
import sqlite3
from multiprocessing import get_context
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boss_hire.boss_access import BossAccessPolicy, account_key_for
from boss_hire.boss_guard import (
    BossCircuitOpen,
    BossGuardBusy,
    BossGuardError,
    BossRequestGuard,
)
from boss_hire.single_job_run_plan import build_favorite_delivery_operation_manifest
from scripts import run_single_job_live as live_launcher


def _hold_guard(root: str, ready: object, release: object) -> None:
    policy = BossAccessPolicy(
        list_interval_seconds=6,
        detail_interval_seconds=15,
    )
    now = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with BossRequestGuard(
        root=Path(root),
        account_key="account-a",
        run_id="child-run",
        operation_manifest=[
            {
                "operation_key": "child:unused",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            }
        ],
        policy=policy,
        now=lambda: now,
    ):
        ready.set()
        release.wait(5)


class BossRequestGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="boss_guard_")
        self.root = Path(self.temporary.name) / "guard"
        self.now = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.current = self.now
        self.policy = BossAccessPolicy(
            list_interval_seconds=6,
            detail_interval_seconds=15,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def guard(
        self,
        run_id: str,
        *,
        operation_manifest: list[dict[str, str]] | None = None,
    ) -> BossRequestGuard:
        def sleep(seconds: float) -> None:
            self.current += timedelta(seconds=seconds)

        manifest = operation_manifest or [
            {
                "operation_key": f"{run_id}:search:{index}",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            }
            for index in range(3)
        ]
        return BossRequestGuard(
            root=self.root,
            account_key="account-a",
            run_id=run_id,
            operation_manifest=manifest,
            policy=self.policy,
            now=lambda: self.current,
            sleep=sleep,
        )

    @staticmethod
    def reserve(
        guard: BossRequestGuard,
        operation_key: str,
        *,
        endpoint_name: str = "search_geeks",
        request_class: str = "list",
    ) -> int:
        return guard.reserve(
            endpoint_name,
            operation_key=operation_key,
            request_class=request_class,
            method="GET",
            endpoint_name=endpoint_name,
        )

    def test_operation_manifest_rejects_unplanned_duplicate_and_mismatched_requests(self) -> None:
        manifest = [
            {
                "operation_key": "source:recommendation:page:1",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "recommend_geeks",
            },
            {
                "operation_key": "source:search:title_match:page:1",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            },
        ]
        with self.guard(
            "manifest-run",
            operation_manifest=manifest,
        ) as guard:
            guard.reserve(
                "recommend_geeks",
                operation_key="source:recommendation:page:1",
                request_class="list",
                method="GET",
                endpoint_name="recommend_geeks",
            )
            with self.assertRaisesRegex(BossGuardError, "already reserved"):
                guard.reserve(
                    "recommend_geeks",
                    operation_key="source:recommendation:page:1",
                    request_class="list",
                    method="GET",
                    endpoint_name="recommend_geeks",
                )
            with self.assertRaisesRegex(BossGuardError, "not authorized"):
                guard.reserve(
                    "search_geeks",
                    operation_key="source:search:unplanned:page:1",
                    request_class="list",
                    method="GET",
                    endpoint_name="search_geeks",
                )
            with self.assertRaisesRegex(BossGuardError, "does not match"):
                guard.reserve(
                    "view_geek",
                    operation_key="source:search:title_match:page:1",
                    request_class="detail",
                    method="GET",
                    endpoint_name="view_geek",
                )
            self.assertEqual(guard.request_count_for_run(), 1)

    def test_same_account_allows_only_one_live_process(self) -> None:
        context = get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(target=_hold_guard, args=(str(self.root), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(BossGuardBusy):
                self.guard("parent-run").acquire()
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_request_ledger_persists_without_becoming_a_daily_quota(self) -> None:
        first_manifest = [
            {
                "operation_key": f"run-1:search:{index}",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            }
            for index in range(2)
        ]
        with self.guard("run-1", operation_manifest=first_manifest) as first:
            for item in first_manifest:
                self.reserve(first, item["operation_key"])

        second_manifest = [
            {
                "operation_key": f"run-2:search:{index}",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            }
            for index in range(7)
        ]
        with self.guard("run-2", operation_manifest=second_manifest) as second:
            for item in second_manifest:
                self.reserve(second, item["operation_key"])

        with sqlite3.connect(self.root / "guard.sqlite3") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM request_attempts WHERE account_key = ?",
                ("account-a",),
            ).fetchone()[0]
        self.assertEqual(count, 9)

    def test_many_manifest_operations_are_not_blocked_by_former_numeric_limits(self) -> None:
        manifest = [
            {
                "operation_key": f"large:search:{index}",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "search_geeks",
            }
            for index in range(47)
        ]
        with self.guard("large-run", operation_manifest=manifest) as guard:
            for item in manifest:
                self.reserve(guard, item["operation_key"])
            self.assertEqual(guard.request_count_for_run(), 47)

    def test_multiple_runs_same_day_are_recorded_without_daily_run_ceiling(self) -> None:
        for index in range(12):
            with self.guard(f"run-{index}"):
                pass

    def test_open_circuit_blocks_future_runs_until_manual_clear(self) -> None:
        with self.guard("run-1") as first:
            first.open_circuit("code_36")

        blocked = self.guard("run-2")
        with self.assertRaisesRegex(BossCircuitOpen, "code_36"):
            blocked.acquire()

        BossRequestGuard.clear_circuit(
            root=self.root,
            account_key="account-a",
            note="manual account review",
            now=lambda: self.now,
        )
        with self.guard("run-3"):
            pass

    def test_guard_files_are_private(self) -> None:
        with self.guard("run-1"):
            pass

        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "account-a.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "guard.sqlite3").stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.root / "guard.sqlite3") as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(live_runs)").fetchall()
            }
        self.assertIn("operation_count", columns)

    def test_clear_circuit_command_requires_manual_note_and_reopens_account(self) -> None:
        auth_dir = self.root / "auth"
        account_key = account_key_for(auth_dir)
        with BossRequestGuard(
            root=self.root,
            account_key=account_key,
            run_id="risk-run",
            operation_manifest=[
                {
                    "operation_key": "risk:unused",
                    "request_class": "list",
                    "method": "GET",
                    "endpoint_name": "search_geeks",
                }
            ],
            policy=self.policy,
            now=lambda: self.now,
        ) as guard:
            guard.open_circuit("code_32")

        args = live_launcher.parse_args(
            ["clear-circuit", "--note", "account reviewed in official page"]
        )
        with patch.object(live_launcher, "FIXED_AUTH_DIR", auth_dir):
            with patch.object(live_launcher, "FIXED_GUARD_DIR", self.root):
                self.assertEqual(args.handler(args), 0)
        with BossRequestGuard(
            root=self.root,
            account_key=account_key,
            run_id="after-review",
            operation_manifest=[
                {
                    "operation_key": "after:unused",
                    "request_class": "list",
                    "method": "GET",
                    "endpoint_name": "search_geeks",
                }
            ],
            policy=self.policy,
            now=lambda: self.now,
        ):
            pass

    def test_request_classes_share_global_interval_with_longer_detail_delay(self) -> None:
        current = self.now
        sleeps: list[float] = []

        def now() -> datetime:
            return current

        def sleep(seconds: float) -> None:
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        with BossRequestGuard(
            root=self.root,
            account_key="interval-account",
            run_id="interval-run",
            operation_manifest=[
                {"operation_key": "interval:search", "request_class": "list", "method": "GET", "endpoint_name": "search_geeks"},
                {"operation_key": "interval:recommend", "request_class": "list", "method": "GET", "endpoint_name": "recommend_geeks"},
                {"operation_key": "interval:detail", "request_class": "detail", "method": "GET", "endpoint_name": "view_geek"},
            ],
            policy=self.policy,
            now=now,
            sleep=sleep,
        ) as guard:
            self.reserve(guard, "interval:search")
            self.reserve(guard, "interval:recommend", endpoint_name="recommend_geeks")
            self.reserve(guard, "interval:detail", endpoint_name="view_geek", request_class="detail")

        self.assertEqual(sleeps, [6.0, 15.0])

    def test_only_fixed_favorite_write_is_allowed_and_uses_detail_pacing(self) -> None:
        current = self.now
        sleeps: list[float] = []

        def now() -> datetime:
            return current

        def sleep(seconds: float) -> None:
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        manifest = [
            {
                "operation_key": "favorite:batch-a:candidate-a:write",
                "request_class": "write",
                "method": "POST",
                "endpoint_name": "favorite_candidate",
            },
            {
                "operation_key": "favorite:batch-a:candidate-a:verify",
                "request_class": "detail",
                "method": "GET",
                "endpoint_name": "favorite_status",
            },
        ]
        with BossRequestGuard(
            root=self.root,
            account_key="favorite-account",
            run_id="favorite-run",
            operation_manifest=manifest,
            policy=self.policy,
            now=now,
            sleep=sleep,
        ) as guard:
            guard.reserve(
                "favorite_candidate",
                operation_key=manifest[0]["operation_key"],
                request_class="write",
                method="POST",
                endpoint_name="favorite_candidate",
            )
            guard.reserve(
                "favorite_status",
                operation_key=manifest[1]["operation_key"],
                request_class="detail",
                method="GET",
                endpoint_name="favorite_status",
            )

        self.assertEqual(sleeps, [15.0])

        for endpoint_name in ("send_message", "cancel_favorite"):
            with self.subTest(endpoint_name=endpoint_name), self.assertRaises(ValueError):
                BossRequestGuard(
                    root=self.root,
                    account_key="invalid-account",
                    run_id=f"invalid-{endpoint_name}",
                    operation_manifest=[
                        {
                            "operation_key": f"invalid:{endpoint_name}",
                            "request_class": "write",
                            "method": "POST",
                            "endpoint_name": endpoint_name,
                        }
                    ],
                    policy=self.policy,
                )

    def test_recovery_batch_gets_new_operation_keys_without_reusing_old_attempt(self) -> None:
        candidate = {
            "candidate_id": "candidate-a",
            "rank": 1,
            "action": "favorite",
            "encrypt_geek_id": "geek-a",
            "encrypt_job_id": "job-a",
            "security_id": "security-a",
        }
        old_manifest = build_favorite_delivery_operation_manifest(
            batch_id="shortlist-old",
            candidates=[candidate],
        )
        recovery_manifest = build_favorite_delivery_operation_manifest(
            batch_id="shortlist-recovery",
            candidates=[candidate],
        )

        self.assertNotEqual(
            [row["operation_key"] for row in old_manifest],
            [row["operation_key"] for row in recovery_manifest],
        )
        with self.guard("old-favorite", operation_manifest=old_manifest) as guard:
            first = old_manifest[0]
            guard.reserve(
                "favorite_candidate",
                operation_key=first["operation_key"],
                request_class="write",
                method="POST",
                endpoint_name="favorite_candidate",
            )
        with self.guard("recovery-favorite", operation_manifest=recovery_manifest) as guard:
            first = recovery_manifest[0]
            guard.reserve(
                "favorite_candidate",
                operation_key=first["operation_key"],
                request_class="write",
                method="POST",
                endpoint_name="favorite_candidate",
            )

    def test_non_write_operations_still_reject_post(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use GET"):
            BossRequestGuard(
                root=self.root,
                account_key="invalid-account",
                run_id="invalid-post",
                operation_manifest=[
                    {
                        "operation_key": "invalid:detail-post",
                        "request_class": "detail",
                        "method": "POST",
                        "endpoint_name": "view_geek",
                    }
                ],
                policy=self.policy,
            )

    def test_balanced_policy_adds_one_random_delay_per_pending_request(self) -> None:
        current = self.now
        sleeps: list[float] = []
        random_ranges: list[tuple[float, float]] = []

        def now() -> datetime:
            return current

        def sleep(seconds: float) -> None:
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        def random_uniform(lower: float, upper: float) -> float:
            random_ranges.append((lower, upper))
            return (lower + upper) / 2

        with BossRequestGuard(
            root=self.root,
            account_key="jitter-account",
            run_id="jitter-run",
            operation_manifest=[
                {"operation_key": "jitter:search", "request_class": "list", "method": "GET", "endpoint_name": "search_geeks"},
                {"operation_key": "jitter:recommend", "request_class": "list", "method": "GET", "endpoint_name": "recommend_geeks"},
                {"operation_key": "jitter:detail", "request_class": "detail", "method": "GET", "endpoint_name": "view_geek"},
            ],
            policy=BossAccessPolicy.balanced(),
            now=now,
            sleep=sleep,
            random_uniform=random_uniform,
        ) as guard:
            self.reserve(guard, "jitter:search")
            self.reserve(guard, "jitter:recommend", endpoint_name="recommend_geeks")
            self.reserve(guard, "jitter:detail", endpoint_name="view_geek", request_class="detail")

        self.assertEqual(random_ranges, [(1.0, 4.0), (1.0, 4.0), (2.0, 10.0)])
        self.assertEqual(sleeps, [8.5, 21.0])

    def test_consecutive_list_requests_do_not_share_one_fixed_interval(self) -> None:
        current = self.now
        sleeps: list[float] = []
        jitter_values = iter((1.0, 1.5, 3.0))

        def now() -> datetime:
            return current

        def sleep(seconds: float) -> None:
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        with BossRequestGuard(
            root=self.root,
            account_key="varying-jitter-account",
            run_id="varying-jitter-run",
            operation_manifest=[
                {"operation_key": f"varying:{index}", "request_class": "list", "method": "GET", "endpoint_name": "search_geeks"}
                for index in range(3)
            ],
            policy=BossAccessPolicy.balanced(),
            now=now,
            sleep=sleep,
            random_uniform=lambda _lower, _upper: next(jitter_values),
        ) as guard:
            for index in range(3):
                self.reserve(guard, f"varying:{index}")

        self.assertEqual(sleeps, [7.5, 9.0])

    def test_favorite_list_pages_use_existing_serial_list_jitter(self) -> None:
        current = self.now
        sleeps: list[float] = []
        random_ranges: list[tuple[float, float]] = []

        def now() -> datetime:
            return current

        def sleep(seconds: float) -> None:
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        def random_uniform(lower: float, upper: float) -> float:
            random_ranges.append((lower, upper))
            return 2.0

        manifest = [
            {
                "operation_key": f"favorite-sync:plan-a:page:{page}",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "favorite_list",
            }
            for page in range(1, 4)
        ]
        with BossRequestGuard(
            root=self.root,
            account_key="favorite-sync-account",
            run_id="favorite-sync-run",
            operation_manifest=manifest,
            policy=BossAccessPolicy.balanced(),
            now=now,
            sleep=sleep,
            random_uniform=random_uniform,
        ) as guard:
            for item in manifest:
                guard.reserve(
                    "favorite_list",
                    operation_key=item["operation_key"],
                    request_class="list",
                    method="GET",
                    endpoint_name="favorite_list",
                )

        self.assertEqual(random_ranges, [(1.0, 4.0)] * 3)
        self.assertEqual(sleeps, [8.0, 8.0])

    def test_guard_rejects_favorite_sync_and_write_in_one_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "favorite list.*write"):
            BossRequestGuard(
                root=self.root,
                account_key="mixed-account",
                run_id="mixed-run",
                operation_manifest=[
                    {
                        "operation_key": "favorite-sync:plan-a:page:1",
                        "request_class": "list",
                        "method": "GET",
                        "endpoint_name": "favorite_list",
                    },
                    {
                        "operation_key": "favorite:batch-a:candidate-a:write",
                        "request_class": "write",
                        "method": "POST",
                        "endpoint_name": "favorite_candidate",
                    },
                ],
                policy=self.policy,
            )

if __name__ == "__main__":
    unittest.main()
