from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from boss_hire.favorite_workflow import (
    create_favorite_workflow_session,
    favorite_workflow_paths,
    load_active_favorite_workflow_session,
    update_favorite_workflow_session,
    build_favorite_candidate_snapshot,
    build_favorite_final_selection,
)
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.state_store import content_hash
from boss_hire.favorite_workflow_command import (
    build_favorite_workflow_sync_plan,
    confirm_favorite_sync,
    expire_favorite_workflow_session,
    materialize_favorite_workflow_delivery_inputs,
    prepare_favorite_workflow,
    record_favorite_workflow_delivery,
    record_favorite_workflow_sync,
)
from boss_hire.supply_inventory import CandidateInventory
from tests.test_supply_inventory import evaluation


class FavoriteCandidateSnapshotTests(unittest.TestCase):
    def make_inventory(self, count: int = 7) -> CandidateInventory:
        inventory = CandidateInventory()
        for index in range(1, count + 1):
            candidate_id = f"candidate-{index}"
            inventory.record_source_card(
                candidate_id,
                "search",
                {
                    "encryptGeekId": f"geek-{index}",
                    "encryptJobId": "job-open",
                    "securityId": f"security-{index}",
                    "name": f"候选人 {index}",
                    "current_title": "平台招商",
                },
            )
            inventory.ensure_resume(candidate_id, lambda: {"work_experience": []})
            inventory.record_evaluation(
                "job-open",
                candidate_id,
                evaluation(candidate_id, 100 - index, 90 - index),
            )
        return inventory

    def test_builds_top_five_from_all_valid_scores_without_mutating_delivered(self) -> None:
        inventory = self.make_inventory()
        inventory.mark_delivered("legacy-publish", "job-open", ["candidate-1", "candidate-2"])
        delivered_before = inventory.to_dict()["delivered"]

        snapshot = build_favorite_candidate_snapshot(
            inventory=inventory,
            job_id="job-open",
            job_title="平台招商负责人",
            rubric_version="rubric-v1",
            favorite_candidate_ids={"geek-3"},
            favorite_statuses={"candidate-4": "favorite_unknown"},
            created_at="2026-09-08T10:00:00+08:00",
        )

        self.assertEqual(snapshot["contract"], "boss_favorite_candidate_snapshot")
        self.assertEqual(snapshot["requested_count"], 5)
        self.assertEqual(snapshot["actual_count"], 5)
        self.assertEqual(
            [row["candidate_id"] for row in snapshot["candidates"]],
            ["candidate-1", "candidate-2", "candidate-5", "candidate-6", "candidate-7"],
        )
        self.assertEqual(snapshot["registry_excluded_candidate_ids"], ["candidate-3"])
        self.assertEqual(snapshot["ledger_excluded_candidate_ids"], ["candidate-4"])
        self.assertEqual(inventory.to_dict()["delivered"], delivered_before)

    def test_reports_shortage_without_fetching_or_backfilling(self) -> None:
        snapshot = build_favorite_candidate_snapshot(
            inventory=self.make_inventory(count=3),
            job_id="job-open",
            job_title="平台招商负责人",
            rubric_version="rubric-v1",
            favorite_candidate_ids=(),
            favorite_statuses={},
            created_at="2026-09-08T10:00:00+08:00",
        )

        self.assertEqual(snapshot["actual_count"], 3)
        self.assertEqual(snapshot["shortage_count"], 2)
        self.assertEqual(snapshot["shortage_reason"], "evaluated_inventory_exhausted")

    def test_freezes_the_full_ranked_pool_before_final_selection(self) -> None:
        snapshot = build_favorite_candidate_snapshot(
            inventory=self.make_inventory(count=10),
            job_id="job-open",
            job_title="平台招商负责人",
            rubric_version="rubric-v1",
            favorite_candidate_ids=(),
            favorite_statuses={},
            created_at="2026-09-08T10:00:00+08:00",
        )

        self.assertEqual(snapshot["actual_count"], 10)
        self.assertEqual([row["rank"] for row in snapshot["candidates"]], list(range(1, 11)))

    def test_final_selection_is_an_exact_original_rank_subset(self) -> None:
        snapshot = build_favorite_candidate_snapshot(
            inventory=self.make_inventory(count=10),
            job_id="job-open",
            job_title="平台招商负责人",
            rubric_version="rubric-v1",
            favorite_candidate_ids=(),
            favorite_statuses={},
            created_at="2026-09-08T10:00:00+08:00",
        )

        selection = build_favorite_final_selection(
            snapshot,
            favorite_candidate_ids={f"geek-{index}" for index in range(1, 6)},
            favorite_statuses={},
            selected_at="2026-09-08T10:01:00+08:00",
        )

        self.assertEqual([row["candidate_id"] for row in selection["candidates"]], [f"candidate-{index}" for index in range(6, 11)])
        self.assertEqual([row["rank"] for row in selection["candidates"]], [6, 7, 8, 9, 10])
        self.assertEqual(selection["registry_excluded_candidate_ids"], [f"candidate-{index}" for index in range(1, 6)])

    def test_ignores_scores_for_another_rubric(self) -> None:
        inventory = self.make_inventory(count=1)
        inventory.record_evaluation(
            "job-open",
            "candidate-1",
            {**evaluation("candidate-1", 99, 99), "rubric_version": "rubric-v2"},
        )

        snapshot = build_favorite_candidate_snapshot(
            inventory=inventory,
            job_id="job-open",
            job_title="平台招商负责人",
            rubric_version="rubric-v1",
            favorite_candidate_ids=(),
            favorite_statuses={},
            created_at="2026-09-08T10:00:00+08:00",
        )

        self.assertEqual(snapshot["actual_count"], 0)
        self.assertEqual(snapshot["candidates"], [])


if __name__ == "__main__":
    unittest.main()


class FavoriteWorkflowSessionTests(unittest.TestCase):
    def session_state(
        self,
        *,
        status: str = "draft",
        session_id: str = "favorite-session-12345678",
    ) -> dict[str, object]:
        snapshot = {
            "schema_version": 1,
            "contract": "boss_favorite_candidate_snapshot",
            "snapshot_id": session_id.replace("session", "snapshot", 1),
            "job_id": "job-open",
            "rubric_version": "rubric-v1",
            "candidates": [],
        }
        return {
            "schema_version": 1,
            "contract": "favorite_workflow_session",
            "session_id": session_id,
            "account_key": "0123456789abcdef",
            "board_date": "2026-09-08",
            "job_id": "job-open",
            "job_title": "平台招商负责人",
            "rubric_version": "rubric-v1",
            "status": status,
            "created_at": "2026-09-08T10:00:00+08:00",
            "updated_at": "2026-09-08T10:00:00+08:00",
            "candidate_snapshot": snapshot,
        }

    def test_creates_private_active_session_and_reloads_digest_checked_state(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = favorite_workflow_paths(Path(temporary) / "favorites")
            created = create_favorite_workflow_session(paths, self.session_state())
            loaded = load_active_favorite_workflow_session(paths)

            self.assertEqual(created.state, loaded.state)
            self.assertEqual(created.state["status"], "draft")
            self.assertTrue(created.state_path.is_file())
            self.assertTrue(created.snapshot_path.is_file())
            self.assertTrue(paths.active_session_path.is_file())

    def test_rejects_second_nonterminal_session_and_allows_replace_after_close(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = favorite_workflow_paths(Path(temporary) / "favorites")
            created = create_favorite_workflow_session(paths, self.session_state())

            with self.assertRaisesRegex(ValueError, "活动收藏会话"):
                create_favorite_workflow_session(
                    paths,
                    self.session_state(session_id="favorite-session-87654321"),
                )

            closed = update_favorite_workflow_session(
                created,
                {**created.state, "status": "closed_by_user", "updated_at": "2026-09-08T10:01:00+08:00"},
            )
            replacement = create_favorite_workflow_session(
                paths,
                self.session_state(session_id="favorite-session-87654321"),
            )

            self.assertEqual(closed.state["status"], "closed_by_user")
            self.assertEqual(replacement.state["session_id"], "favorite-session-87654321")

    def test_rejects_pointer_digest_drift(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = favorite_workflow_paths(Path(temporary) / "favorites")
            create_favorite_workflow_session(paths, self.session_state())
            pointer = paths.active_session_path.read_text(encoding="utf-8")
            paths.active_session_path.write_text(pointer.replace("favorite-session", "tampered-session", 1), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_active_favorite_workflow_session(paths)

    def test_expires_prior_session_before_a_new_session_is_created(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = favorite_workflow_paths(Path(temporary) / "favorites")
            active = create_favorite_workflow_session(paths, self.session_state())

            expired = expire_favorite_workflow_session(
                active,
                updated_at="2026-09-09T10:00:00+08:00",
            )
            replacement = create_favorite_workflow_session(
                paths,
                self.session_state(session_id="favorite-session-87654321"),
            )

            self.assertEqual(expired.state["status"], "expired")
            self.assertEqual(replacement.state["session_id"], "favorite-session-87654321")

    def test_rejects_changes_to_a_frozen_final_selection(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = favorite_workflow_paths(Path(temporary) / "favorites")
            state = self.session_state(status="awaiting_delivery_confirmation")
            state["final_selection"] = {
                "schema_version": 1,
                "contract": "boss_favorite_final_selection",
                "snapshot_id": state["candidate_snapshot"]["snapshot_id"],
                "candidate_snapshot_digest": content_hash(state["candidate_snapshot"]),
                "selected_at": "2026-09-08T10:01:00+08:00",
                "requested_count": 5,
                "actual_count": 0,
                "shortage_count": 5,
                "shortage_reason": "ranked_pool_exhausted",
                "registry_excluded_candidate_ids": [],
                "ledger_excluded_candidate_ids": [],
                "candidates": [],
            }
            created = create_favorite_workflow_session(paths, state)

            with self.assertRaisesRegex(ValueError, "不能修改已冻结最终待收藏名单"):
                update_favorite_workflow_session(
                    created,
                    {**created.state, "final_selection": None, "updated_at": "2026-09-08T10:02:00+08:00"},
                )


class FavoriteWorkflowCommandTests(unittest.TestCase):
    def test_prepares_a_local_session_without_using_publish_history(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = FavoriteCandidateSnapshotTests().make_inventory()
            inventory.mark_delivered("old-publish", "job-open", ["candidate-1"])
            session = prepare_favorite_workflow(
                inventory=inventory,
                account_state_dir=root / "account",
                account_key="0123456789abcdef",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )

            self.assertEqual(session.state["status"], "awaiting_sync_confirmation")
            self.assertEqual(
                [row["candidate_id"] for row in session.state["candidate_snapshot"]["candidates"]],
                [f"candidate-{index}" for index in range(1, 8)],
            )

    def test_returns_existing_active_session_without_rebuilding_candidates(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = FavoriteCandidateSnapshotTests().make_inventory()
            first = prepare_favorite_workflow(
                inventory=inventory,
                account_state_dir=root / "account",
                account_key="0123456789abcdef",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            inventory.record_evaluation("job-open", "candidate-7", evaluation("candidate-7", 999, 99))

            restored = prepare_favorite_workflow(
                inventory=inventory,
                account_state_dir=root / "account",
                account_key="0123456789abcdef",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:01:00+08:00",
            )

            self.assertEqual(restored.state, first.state)

    def test_sync_plan_initializes_then_uses_checkpoint_incrementally(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = FavoriteCandidateSnapshotTests().make_inventory()
            auth_dir = root / "auth"
            account_key = "0123456789abcdef"
            session = prepare_favorite_workflow(
                inventory=inventory,
                account_state_dir=root / "account",
                account_key=account_key,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                initial = build_favorite_workflow_sync_plan(session, auth_dir=auth_dir)
            FavoriteRegistry(root / "account", account_key=account_key).save_complete_checkpoint(
                anchor_group=["geek-1"],
                receipt_id="sync-1",
                sync_status="end_reached",
                completed_at="2026-09-08T10:01:00+08:00",
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                incremental = build_favorite_workflow_sync_plan(session, auth_dir=auth_dir)

            self.assertEqual(initial["mode"], "initialize")
            self.assertEqual(incremental["mode"], "incremental")
            self.assertEqual(initial["purpose"], "favorite_delivery")
            self.assertEqual(initial["favorite_session_id"], session.state["session_id"])
            self.assertEqual(initial["max_pages"], 40)

    def test_sync_confirmation_requires_exact_business_text(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = prepare_favorite_workflow(
                inventory=FavoriteCandidateSnapshotTests().make_inventory(),
                account_state_dir=root / "account",
                account_key="0123456789abcdef",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )

            with self.assertRaisesRegex(Exception, "确认"):
                confirm_favorite_sync(session, input_fn=lambda _prompt: "确认收藏5人")
            confirm_favorite_sync(session, input_fn=lambda _prompt: "确认生成未收藏Top5")

    def test_completed_sync_moves_session_to_delivery_without_another_sync(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            account_key = "0123456789abcdef"
            session = prepare_favorite_workflow(
                inventory=FavoriteCandidateSnapshotTests().make_inventory(),
                account_state_dir=root / "account",
                account_key=account_key,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                plan = build_favorite_workflow_sync_plan(session, auth_dir=root / "auth")
            receipt = {
                "schema_version": 1,
                "contract": "boss_favorite_sync_receipt",
                "plan_id": plan["plan_id"],
                "account_key": account_key,
                "board_date": "2026-09-08",
                "purpose": "favorite_delivery",
                "batch_id": plan["batch_id"],
                "batch_digest": plan["batch_digest"],
                "completed_at": "2026-09-08T10:01:00+08:00",
                "status": "end_reached",
                "complete": True,
                "pages_read": 1,
                "max_pages": 40,
                "first_page_ids": ["geek-1"],
                "checkpoint_advanced": True,
            }

            completed = record_favorite_workflow_sync(
                session,
                plan=plan,
                receipt=receipt,
                updated_at="2026-09-08T10:01:00+08:00",
            )

            self.assertEqual(completed.state["status"], "awaiting_delivery_confirmation")
            with self.assertRaisesRegex(ValueError, "不能核对"):
                build_favorite_workflow_sync_plan(completed, auth_dir=root / "auth")
            inputs = materialize_favorite_workflow_delivery_inputs(completed)
            self.assertTrue(inputs.batch_path.is_file())
            self.assertTrue(inputs.sync_receipt_path.is_file())
            finished = record_favorite_workflow_delivery(
                completed,
                receipt={
                    "schema_version": 1,
                    "contract": "boss_favorite_delivery_receipt",
                    "plan_id": "favorite-test",
                    "batch_id": plan["batch_id"],
                    "selected_count": 1,
                    "confirmed_count": 1,
                    "already_confirmed_count": 0,
                    "failed_count": 0,
                    "unknown_count": 0,
                    "results": [
                        {
                            "candidate_id": "candidate-1",
                            "rank": 1,
                            "status": "favorite_confirmed",
                        }
                    ],
                },
                updated_at="2026-09-08T10:02:00+08:00",
            )
            self.assertEqual(finished.state["status"], "completed")

            with self.assertRaisesRegex(ValueError, "精确子集"):
                record_favorite_workflow_delivery(
                    completed,
                    receipt={
                        "schema_version": 1,
                        "contract": "boss_favorite_delivery_receipt",
                        "plan_id": "favorite-forged",
                        "batch_id": plan["batch_id"],
                        "selected_count": 1,
                        "confirmed_count": 1,
                        "already_confirmed_count": 0,
                        "failed_count": 0,
                        "unknown_count": 0,
                        "results": [
                            {
                                "candidate_id": "candidate-6",
                                "rank": 6,
                                "status": "favorite_confirmed",
                            }
                        ],
                    },
                    updated_at="2026-09-08T10:02:00+08:00",
                )

    def test_completed_sync_selects_next_five_from_the_frozen_full_ranking_pool(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            account_key = "0123456789abcdef"
            session = prepare_favorite_workflow(
                inventory=FavoriteCandidateSnapshotTests().make_inventory(count=10),
                account_state_dir=root / "account",
                account_key=account_key,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            self.assertEqual(
                [row["candidate_id"] for row in session.state["candidate_snapshot"]["candidates"]],
                [f"candidate-{index}" for index in range(1, 11)],
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                plan = build_favorite_workflow_sync_plan(session, auth_dir=root / "auth")
            FavoriteRegistry(root / "account", account_key=account_key).record_candidates(
                [f"geek-{index}" for index in range(1, 6)],
                source="list_sync",
                receipt_id="sync-1",
                observed_at="2026-09-08T10:01:00+08:00",
            )
            completed = record_favorite_workflow_sync(
                session,
                plan=plan,
                receipt={
                    "schema_version": 1,
                    "contract": "boss_favorite_sync_receipt",
                    "plan_id": plan["plan_id"],
                    "account_key": account_key,
                    "board_date": "2026-09-08",
                    "purpose": "favorite_delivery",
                    "batch_id": plan["batch_id"],
                    "batch_digest": plan["batch_digest"],
                    "completed_at": "2026-09-08T10:01:00+08:00",
                    "status": "end_reached",
                    "complete": True,
                    "pages_read": 1,
                    "max_pages": 40,
                    "first_page_ids": ["geek-1"],
                    "checkpoint_advanced": True,
                },
                updated_at="2026-09-08T10:01:00+08:00",
            )

            self.assertEqual(completed.state["status"], "awaiting_delivery_confirmation")
            self.assertEqual(
                [row["candidate_id"] for row in completed.state["final_selection"]["candidates"]],
                [f"candidate-{index}" for index in range(6, 11)],
            )
            self.assertEqual(
                [row["rank"] for row in completed.state["final_selection"]["candidates"]],
                [6, 7, 8, 9, 10],
            )

    def test_completed_sync_finishes_when_the_whole_frozen_pool_is_already_favorited(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            account_key = "0123456789abcdef"
            session = prepare_favorite_workflow(
                inventory=FavoriteCandidateSnapshotTests().make_inventory(count=5),
                account_state_dir=root / "account",
                account_key=account_key,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                plan = build_favorite_workflow_sync_plan(session, auth_dir=root / "auth")
            FavoriteRegistry(root / "account", account_key=account_key).record_candidates(
                [f"geek-{index}" for index in range(1, 6)],
                source="list_sync",
                receipt_id="sync-1",
            )
            completed = record_favorite_workflow_sync(
                session,
                plan=plan,
                receipt={
                    "schema_version": 1,
                    "contract": "boss_favorite_sync_receipt",
                    "plan_id": plan["plan_id"],
                    "account_key": account_key,
                    "board_date": "2026-09-08",
                    "purpose": "favorite_delivery",
                    "batch_id": plan["batch_id"],
                    "batch_digest": plan["batch_digest"],
                    "completed_at": "2026-09-08T10:01:00+08:00",
                    "status": "end_reached",
                    "complete": True,
                    "pages_read": 1,
                    "max_pages": 40,
                    "first_page_ids": ["geek-1"],
                    "checkpoint_advanced": True,
                },
                updated_at="2026-09-08T10:01:00+08:00",
            )

            self.assertEqual(completed.state["status"], "completed")
            self.assertEqual(completed.state["final_selection"]["actual_count"], 0)
            with self.assertRaisesRegex(ValueError, "不能执行收藏"):
                materialize_favorite_workflow_delivery_inputs(completed)

    def test_incomplete_sync_never_creates_a_final_selection(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            account_key = "0123456789abcdef"
            session = prepare_favorite_workflow(
                inventory=FavoriteCandidateSnapshotTests().make_inventory(),
                account_state_dir=root / "account",
                account_key=account_key,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                board_date="2026-09-08",
                created_at="2026-09-08T10:00:00+08:00",
            )
            with patch(
                "boss_hire.single_job_run_plan.preflight_boss_access",
                return_value=SimpleNamespace(account_key=account_key),
            ):
                plan = build_favorite_workflow_sync_plan(session, auth_dir=root / "auth")

            incomplete = record_favorite_workflow_sync(
                session,
                plan=plan,
                receipt={
                    "schema_version": 1,
                    "contract": "boss_favorite_sync_receipt",
                    "plan_id": plan["plan_id"],
                    "account_key": account_key,
                    "board_date": "2026-09-08",
                    "purpose": "favorite_delivery",
                    "batch_id": plan["batch_id"],
                    "batch_digest": plan["batch_digest"],
                    "completed_at": "2026-09-08T10:01:00+08:00",
                    "status": "sync_incomplete",
                    "complete": False,
                    "pages_read": 40,
                    "max_pages": 40,
                    "first_page_ids": ["geek-1"],
                    "checkpoint_advanced": False,
                },
                updated_at="2026-09-08T10:01:00+08:00",
            )

            self.assertEqual(incomplete.state["status"], "blocked_incomplete_sync")
            self.assertNotIn("final_selection", incomplete.state)
