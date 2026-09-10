from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from threading import Barrier, Thread

from boss_hire.favorite_delivery import (
    FavoriteDeliveryLedger,
    build_favorite_delivery_plan,
    build_synced_favorite_delivery_plan,
    migrate_favorite_delivery_ledger,
)
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.state_store import content_hash


def published_batch(count: int = 5) -> dict[str, object]:
    candidates = []
    for rank in range(1, count + 1):
        candidates.append(
            {
                "candidate_id": f"candidate-{rank}",
                "rank": rank,
                "score": 100 - rank,
                "summary": f"候选人 {rank} 的评分摘要",
                "display": {"name": f"候选人 {rank}", "current_title": "平台招商"},
                "boss_identifiers": {
                    "encryptGeekId": f"geek-{rank}",
                    "securityId": f"security-{rank}",
                    "encryptJobId": "job-open",
                },
            }
        )
    return {
        "schema_version": 1,
        "contract": "candidate_shortlist_batch",
        "batch_id": "shortlist-demo",
        "published_at": "2026-09-03T10:00:00+08:00",
        "job_id": "job-open",
        "job_title": "平台招商负责人",
        "rubric_version": "rubric-v1",
        "requested_count": 5,
        "actual_count": count,
        "shortage_count": max(0, 5 - count),
        "shortage_reason": None,
        "candidates": candidates,
    }


def favorite_sync_receipt(
    batch: dict[str, object],
    *,
    account_key: str = "0123456789abcdef",
    board_date: str = "2026-09-03",
    complete: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract": "boss_favorite_sync_receipt",
        "plan_id": "favorite-sync-demo",
        "account_key": account_key,
        "board_date": board_date,
        "purpose": "favorite_delivery",
        "batch_id": batch["batch_id"],
        "batch_digest": content_hash(batch),
        "completed_at": f"{board_date}T09:30:00+08:00",
        "status": "end_reached" if complete else "sync_incomplete",
        "complete": complete,
        "pages_read": 2,
        "max_pages": 40,
        "first_page_ids": ["geek-1", "geek-2"],
        "checkpoint_advanced": complete,
    }


class FavoriteDeliveryPlanTests(unittest.TestCase):
    def test_selects_exact_subset_low_to_high_rank_for_recent_favorite_order(self) -> None:
        batch = published_batch()
        before = copy.deepcopy(batch)

        plan = build_favorite_delivery_plan(batch, selected_ranks=[1, 2, 4])

        self.assertEqual(batch, before)
        self.assertEqual(plan["contract"], "boss_favorite_delivery_plan")
        self.assertEqual(plan["batch_id"], "shortlist-demo")
        self.assertEqual(plan["batch_digest"], content_hash(batch))
        self.assertEqual(plan["published_count"], 5)
        self.assertEqual(plan["selected_count"], 3)
        self.assertEqual(plan["not_selected_count"], 2)
        self.assertEqual([row["rank"] for row in plan["candidates"]], [4, 2, 1])
        self.assertEqual(
            [row["candidate_id"] for row in plan["not_selected_candidates"]],
            ["candidate-3", "candidate-5"],
        )

    def test_rejects_empty_duplicate_or_out_of_range_selection(self) -> None:
        batch = published_batch()
        cases = ([], [1, 1], [0], [6])
        for selected_ranks in cases:
            with self.subTest(selected_ranks=selected_ranks), self.assertRaises(ValueError):
                build_favorite_delivery_plan(batch, selected_ranks=selected_ranks)

    def test_rejects_wrong_contract_tampering_and_missing_boss_identifiers(self) -> None:
        batch = published_batch()
        with self.assertRaisesRegex(ValueError, "candidate_shortlist_batch"):
            build_favorite_delivery_plan(
                {**batch, "contract": "other"},
                selected_ranks=[1],
            )
        with self.assertRaisesRegex(ValueError, "摘要"):
            build_favorite_delivery_plan(
                batch,
                selected_ranks=[1],
                expected_batch_digest="wrong",
            )
        missing = copy.deepcopy(batch)
        missing["candidates"][0]["boss_identifiers"].pop("securityId")  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "securityId"):
            build_favorite_delivery_plan(missing, selected_ranks=[1])

    def test_global_favorite_status_skips_confirmed_and_blocks_unknown(self) -> None:
        batch = published_batch()
        confirmed = build_favorite_delivery_plan(
            batch,
            selected_ranks=[1, 2],
            favorite_statuses={"candidate-1": "favorite_confirmed"},
        )
        self.assertEqual(confirmed["candidates"][0]["action"], "favorite")
        self.assertEqual(confirmed["candidates"][1]["action"], "already_confirmed")

        with self.assertRaisesRegex(ValueError, "favorite_unknown"):
            build_favorite_delivery_plan(
                batch,
                selected_ranks=[1],
                favorite_statuses={"candidate-1": "favorite_unknown"},
            )

    def test_recovery_only_reopens_definite_failures(self) -> None:
        batch = published_batch()
        with self.assertRaisesRegex(ValueError, "favorite_failed"):
            build_favorite_delivery_plan(
                batch,
                selected_ranks=[1],
                favorite_statuses={"candidate-1": "favorite_failed"},
            )

        plan = build_favorite_delivery_plan(
            batch,
            selected_ranks=[1],
            favorite_statuses={"candidate-1": "favorite_failed"},
            retry_definite_failures=True,
        )

        self.assertTrue(plan["retry_definite_failures"])
        self.assertEqual(plan["candidates"][0]["action"], "favorite")
        self.assertEqual(plan["candidates"][0]["favorite_status"], "favorite_failed")
        for blocked_status in ("favorite_unknown", "write_reserved"):
            with self.subTest(status=blocked_status), self.assertRaisesRegex(
                ValueError, blocked_status
            ):
                build_favorite_delivery_plan(
                    batch,
                    selected_ranks=[1],
                    favorite_statuses={"candidate-1": blocked_status},
                    retry_definite_failures=True,
                )

    def test_synced_plan_binds_complete_receipt_and_skips_registry_ids(self) -> None:
        batch = published_batch()
        receipt = favorite_sync_receipt(batch)

        plan = build_synced_favorite_delivery_plan(
            batch,
            selected_ranks=[1, 2],
            sync_receipt=receipt,
            account_key="0123456789abcdef",
            board_date="2026-09-03",
            favorite_candidate_ids={"geek-1"},
        )

        self.assertEqual(plan["account_key"], "0123456789abcdef")
        self.assertEqual(plan["board_date"], "2026-09-03")
        self.assertEqual(plan["favorite_sync_receipt_id"], "favorite-sync-demo")
        self.assertEqual(plan["favorite_sync_receipt_digest"], content_hash(receipt))
        self.assertEqual(plan["actionable_count"], 1)
        self.assertEqual(plan["already_confirmed_count"], 1)
        self.assertEqual(
            [(row["encrypt_geek_id"], row["action"]) for row in plan["candidates"]],
            [("geek-2", "favorite"), ("geek-1", "already_confirmed")],
        )

    def test_synced_plan_rejects_missing_or_mismatched_receipt(self) -> None:
        batch = published_batch()
        complete = favorite_sync_receipt(batch)
        cases: list[tuple[str, object]] = [
            ("missing", None),
            ("incomplete", favorite_sync_receipt(batch, complete=False)),
            (
                "cross_day",
                favorite_sync_receipt(batch, board_date="2026-09-02"),
            ),
            (
                "cross_account",
                favorite_sync_receipt(batch, account_key="fedcba9876543210"),
            ),
            ("wrong_batch_id", {**complete, "batch_id": "shortlist-other"}),
            ("wrong_batch_digest", {**complete, "batch_digest": "0" * 64}),
        ]

        for label, receipt in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                build_synced_favorite_delivery_plan(
                    batch,
                    selected_ranks=[1],
                    sync_receipt=receipt,  # type: ignore[arg-type]
                    account_key="0123456789abcdef",
                    board_date="2026-09-03",
                    favorite_candidate_ids=set(),
                )


class FavoriteDeliveryLedgerTests(unittest.TestCase):
    def test_persists_candidate_global_status_without_consuming_unselected_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "favorite_delivery_state.json"
            ledger = FavoriteDeliveryLedger(path)

            ledger.set_status(
                "candidate-1",
                "favorite_confirmed",
                batch_id="shortlist-demo",
                operation_key="favorite:shortlist-demo:candidate-1",
            )
            ledger.save()

            restored = FavoriteDeliveryLedger(path)
            self.assertEqual(restored.status("candidate-1"), "favorite_confirmed")
            self.assertEqual(restored.status("candidate-3"), "not_requested")
            self.assertEqual(restored.status("candidate-5"), "not_requested")

    def test_write_reservation_is_global_and_persisted_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "favorite_delivery_state.json"
            first = FavoriteDeliveryLedger(path)
            second = FavoriteDeliveryLedger(path)

            first.reserve_write(
                "candidate-1",
                batch_id="shortlist-demo",
                operation_key="favorite:shortlist-demo:candidate-1:write",
                recorded_at="2026-09-03T10:00:00+08:00",
            )

            self.assertEqual(FavoriteDeliveryLedger(path).status("candidate-1"), "write_reserved")
            with self.assertRaisesRegex(RuntimeError, "already attempted"):
                second.reserve_write(
                    "candidate-1",
                    batch_id="shortlist-other",
                    operation_key="favorite:shortlist-other:candidate-1:write",
                )

    def test_explicit_recovery_replaces_only_failed_reservation_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "favorite_delivery_state.json"
            ledger = FavoriteDeliveryLedger(path)
            ledger.set_status(
                "candidate-1",
                "favorite_failed",
                batch_id="shortlist-old",
                operation_key="favorite:shortlist-old:candidate-1:write",
                recorded_at="2026-09-03T10:00:00+08:00",
            )
            ledger.save()

            with self.assertRaisesRegex(RuntimeError, "already attempted"):
                FavoriteDeliveryLedger(path).reserve_write(
                    "candidate-1",
                    batch_id="shortlist-new",
                    operation_key="favorite:shortlist-new:candidate-1:write",
                )

            FavoriteDeliveryLedger(path).reserve_write(
                "candidate-1",
                batch_id="shortlist-new",
                operation_key="favorite:shortlist-new:candidate-1:write",
                retry_definite_failures=True,
                recorded_at="2026-09-04T10:00:00+08:00",
            )

            record = FavoriteDeliveryLedger(path).record("candidate-1")
            self.assertEqual(record["status"], "write_reserved")
            self.assertEqual(record["batch_id"], "shortlist-new")
            self.assertEqual(len(record["attempt_history"]), 1)
            self.assertEqual(record["attempt_history"][0]["status"], "favorite_failed")
            self.assertEqual(record["attempt_history"][0]["batch_id"], "shortlist-old")

    def test_reserved_write_can_only_be_finalized_by_the_same_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "favorite_delivery_state.json"
            ledger = FavoriteDeliveryLedger(path)
            operation_key = "favorite:shortlist-demo:candidate-1:write"
            ledger.reserve_write(
                "candidate-1",
                batch_id="shortlist-demo",
                operation_key=operation_key,
            )

            with self.assertRaisesRegex(RuntimeError, "operation"):
                ledger.finalize_write(
                    "candidate-1",
                    "favorite_confirmed",
                    batch_id="shortlist-demo",
                    operation_key="favorite:shortlist-demo:candidate-1:other",
                )
            ledger.finalize_write(
                "candidate-1",
                "favorite_confirmed",
                batch_id="shortlist-demo",
                operation_key=operation_key,
            )
            self.assertEqual(FavoriteDeliveryLedger(path).status("candidate-1"), "favorite_confirmed")

    def test_concurrent_write_reservations_allow_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "favorite_delivery_state.json"
            barrier = Barrier(2)
            outcomes: list[str] = []

            def reserve(operation_key: str) -> None:
                barrier.wait()
                try:
                    FavoriteDeliveryLedger(path).reserve_write(
                        "candidate-1",
                        batch_id="shortlist-demo",
                        operation_key=operation_key,
                    )
                    outcomes.append("reserved")
                except RuntimeError:
                    outcomes.append("rejected")

            threads = [
                Thread(target=reserve, args=(f"favorite:shortlist-demo:candidate-1:{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertCountEqual(outcomes, ["reserved", "rejected"])

    def test_legacy_ledger_migration_preserves_blocking_states_and_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "legacy" / "favorite_delivery_state.json"
            target_path = root / "account" / "delivery_ledger.json"
            source = FavoriteDeliveryLedger(source_path)
            statuses = {
                "confirmed": "favorite_confirmed",
                "manual": "manual_verified",
                "unknown": "favorite_unknown",
                "failed": "favorite_failed",
                "reserved": "write_reserved",
            }
            for candidate_id, status in statuses.items():
                source.set_status(
                    candidate_id,
                    status,
                    batch_id="legacy-batch",
                    operation_key=f"legacy:{candidate_id}",
                    recorded_at="2026-09-03T10:00:00+08:00",
                )
            source.save()
            original_source = source_path.read_bytes()
            registry = FavoriteRegistry(root / "account", account_key="0123456789abcdef")

            receipt = migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=target_path,
                registry=registry,
                migration_id="migration-1",
                stable_candidate_ids={
                    "confirmed": "geek-confirmed",
                    "manual": "geek-manual",
                },
                migrated_at="2026-09-04T10:00:00+08:00",
            )

            target = FavoriteDeliveryLedger(target_path)
            for candidate_id, status in statuses.items():
                self.assertEqual(target.status(candidate_id), status)
            self.assertEqual(
                registry.known_candidate_ids(),
                {"geek-confirmed", "geek-manual"},
            )
            self.assertEqual(receipt["imported_count"], 5)
            self.assertEqual(receipt["blocked_preserved_count"], 3)
            self.assertFalse(receipt["source_deleted"])
            self.assertEqual(source_path.read_bytes(), original_source)

    def test_legacy_ledger_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "legacy.json"
            target_path = root / "account" / "delivery_ledger.json"
            source = FavoriteDeliveryLedger(source_path)
            source.set_status(
                "candidate-1",
                "favorite_confirmed",
                batch_id="legacy-batch",
                operation_key="legacy:candidate-1",
            )
            source.save()
            registry = FavoriteRegistry(root / "account", account_key="0123456789abcdef")

            first = migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=target_path,
                registry=registry,
                migration_id="migration-1",
                stable_candidate_ids={"candidate-1": "geek-1"},
                migrated_at="2026-09-04T10:00:00+08:00",
            )
            first_target = FavoriteDeliveryLedger(target_path).to_dict()
            first_registry = registry.snapshot()
            second = migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=target_path,
                registry=registry,
                migration_id="migration-1",
                stable_candidate_ids={"candidate-1": "geek-1"},
                migrated_at="2026-09-04T10:00:00+08:00",
            )

            self.assertEqual(first["imported_count"], 1)
            self.assertEqual(second["imported_count"], 0)
            self.assertEqual(FavoriteDeliveryLedger(target_path).to_dict(), first_target)
            self.assertEqual(registry.snapshot(), first_registry)

    def test_legacy_migration_recognizes_failed_attempt_preserved_by_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "legacy.json"
            target_path = root / "account" / "delivery_ledger.json"
            source = FavoriteDeliveryLedger(source_path)
            source.set_status(
                "candidate-1",
                "favorite_failed",
                batch_id="legacy-batch",
                operation_key="favorite:legacy-batch:candidate-1:write",
                recorded_at="2026-09-03T10:00:00+08:00",
            )
            source.save()
            registry = FavoriteRegistry(root / "account", account_key="0123456789abcdef")

            migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=target_path,
                registry=registry,
                migration_id="migration-1",
                migrated_at="2026-09-04T10:00:00+08:00",
            )
            target = FavoriteDeliveryLedger(target_path)
            recovery_operation = "favorite:recovery-batch:candidate-1:write"
            target.reserve_write(
                "candidate-1",
                batch_id="recovery-batch",
                operation_key=recovery_operation,
                retry_definite_failures=True,
                recorded_at="2026-09-04T11:00:00+08:00",
            )
            target.finalize_write(
                "candidate-1",
                "favorite_confirmed",
                batch_id="recovery-batch",
                operation_key=recovery_operation,
                recorded_at="2026-09-04T11:01:00+08:00",
            )

            receipt = migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=target_path,
                registry=registry,
                migration_id="migration-1",
                migrated_at="2026-09-05T10:00:00+08:00",
            )

            record = FavoriteDeliveryLedger(target_path).record("candidate-1")
            self.assertEqual(receipt["imported_count"], 0)
            self.assertEqual(record["status"], "favorite_confirmed")
            self.assertEqual(record["attempt_history"][0]["status"], "favorite_failed")

    def test_legacy_migration_never_registers_unmapped_local_candidate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "legacy.json"
            source = FavoriteDeliveryLedger(source_path)
            source.set_status(
                "local-candidate-id",
                "favorite_confirmed",
                batch_id="legacy-batch",
                operation_key="legacy:local-candidate-id",
            )
            source.save()
            registry = FavoriteRegistry(root / "account", account_key="0123456789abcdef")

            receipt = migrate_favorite_delivery_ledger(
                source_path=source_path,
                target_path=root / "account" / "delivery_ledger.json",
                registry=registry,
                migration_id="migration-unmapped",
                stable_candidate_ids={},
            )

            self.assertEqual(registry.known_candidate_ids(), set())
            self.assertEqual(receipt["unmapped_confirmed_count"], 1)
            self.assertEqual(
                FavoriteDeliveryLedger(root / "account" / "delivery_ledger.json").status(
                    "local-candidate-id"
                ),
                "favorite_confirmed",
            )

    def test_legacy_ledger_migration_conflict_fails_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "legacy.json"
            target_path = root / "account" / "delivery_ledger.json"
            source = FavoriteDeliveryLedger(source_path)
            source.set_status(
                "candidate-1",
                "favorite_confirmed",
                batch_id="legacy-batch",
                operation_key="legacy:candidate-1",
            )
            source.save()
            target = FavoriteDeliveryLedger(target_path)
            target.set_status(
                "candidate-1",
                "favorite_unknown",
                batch_id="new-batch",
                operation_key="new:candidate-1",
            )
            target.save()
            original_target = target_path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "conflict"):
                migrate_favorite_delivery_ledger(
                    source_path=source_path,
                    target_path=target_path,
                    registry=FavoriteRegistry(
                        root / "account", account_key="0123456789abcdef"
                    ),
                    migration_id="migration-1",
                )

            self.assertEqual(target_path.read_bytes(), original_target)


if __name__ == "__main__":
    unittest.main()
