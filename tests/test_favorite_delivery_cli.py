from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boss_hire.boss_access import BossLiveAccessDenied, account_key_for
from boss_hire.favorite_delivery import FavoriteDeliveryLedger
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.favorite_workflow import favorite_workflow_paths, load_active_favorite_workflow_session
from boss_hire.supply_inventory import CandidateInventory
from scripts import run_single_job_live as run_single_job_live_module
from scripts.run_single_job_live import favorite_command, parse_args
from tests.test_favorite_delivery import favorite_sync_receipt, published_batch


class FavoriteDeliveryCliTests(unittest.TestCase):
    def write_batch(self, root: Path) -> Path:
        path = root / "candidate_shortlist.json"
        path.write_text(json.dumps(published_batch(), ensure_ascii=False), encoding="utf-8")
        return path

    def configure_account(self, root: Path, *, auth_dir: Path | None = None) -> tuple[Path, Path]:
        fixed_auth_dir = auth_dir or root / "auth"
        account_state_dir = root / "account-state"
        auth_patch = patch.object(run_single_job_live_module, "FIXED_AUTH_DIR", fixed_auth_dir)
        state_patch = patch.object(
            run_single_job_live_module,
            "favorite_account_state_dir",
            return_value=account_state_dir,
        )
        auth_patch.start()
        state_patch.start()
        self.addCleanup(auth_patch.stop)
        self.addCleanup(state_patch.stop)
        return fixed_auth_dir, account_state_dir

    def write_sync_receipt(
        self,
        root: Path,
        *,
        auth_dir: Path,
        board_date: str = "2026-09-03",
        complete: bool = True,
    ) -> Path:
        path = root / "favorite_sync_receipt.json"
        path.write_text(
            json.dumps(
                favorite_sync_receipt(
                    published_batch(),
                    account_key=account_key_for(auth_dir),
                    board_date=board_date,
                    complete=complete,
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def fixed_now() -> datetime:
        return datetime(2026, 9, 3, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_favorite_command_requires_sync_receipt_argument(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "favorite",
                    "--batch",
                    "candidate_shortlist.json",
                    "--work-dir",
                    "deliveries",
                ]
            )

    def test_plain_favorite_uses_the_business_workflow_without_technical_arguments(self) -> None:
        args = parse_args(["favorite"])

        self.assertTrue(args.favorite_workflow)

    def test_plain_favorite_requires_sync_confirmation_before_auth_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = CandidateInventory()
            inventory.record_source_card(
                "candidate-1",
                "search",
                {
                    "encryptGeekId": "geek-1",
                    "encryptJobId": "job-open",
                    "securityId": "security-1",
                    "name": "候选人 1",
                },
            )
            inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})
            inventory.record_evaluation(
                "job-open",
                "candidate-1",
                {
                    "schema_version": 2,
                    "contract": "continuous_ranking",
                    "candidate_id": "candidate-1",
                    "rubric_version": "rubric-v1",
                    "total_score": 91,
                    "evidence_coverage": 90,
                    "dimension_scores": [],
                    "evidence": [],
                    "gaps": [],
                    "risks": [],
                    "follow_up_questions": [],
                    "summary": "候选人 1 摘要",
                },
            )
            inventory_path = root / "candidate_inventory.json"
            inventory.save(inventory_path)
            paths = SimpleNamespace(inventory_path=inventory_path)
            run = SimpleNamespace(
                state={
                    "status": "scoring_complete",
                    "job_id": "job-open",
                    "job_title": "平台招商负责人",
                    "rubric_version": "rubric-v1",
                }
            )
            outputs: list[str] = []
            args = parse_args(["favorite"])
            with patch.object(run_single_job_live_module, "workflow_paths", return_value=paths):
                with patch("boss_hire.workflow_run.load_active_workflow_run", return_value=run):
                    with patch.object(
                        run_single_job_live_module,
                        "favorite_account_state_dir",
                        return_value=root / "account",
                    ):
                        with patch(
                            "boss_hire.boss_access.account_key_for",
                            return_value="0123456789abcdef",
                        ):
                            with patch.object(
                                run_single_job_live_module,
                                "sync_auth_from_chrome",
                                side_effect=AssertionError("plain favorite must stay local"),
                            ):
                                with self.assertRaisesRegex(BossLiveAccessDenied, "确认"):
                                    favorite_command(
                                        args,
                                        input_fn=lambda _prompt: "确认收藏1人",
                                        output_fn=outputs.append,
                                        now=self.fixed_now,
                                    )

            rendered = "\n".join(outputs)
            self.assertIn("本地有效评分池：1 人", rendered)
            self.assertNotIn("1. 候选人 1", rendered)
            self.assertNotIn("batch", rendered)

    def test_plain_favorite_runs_only_sync_after_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = CandidateInventory()
            for index in range(1, 7):
                candidate_id = f"candidate-{index}"
                inventory.record_source_card(
                    candidate_id,
                    "search",
                    {"encryptGeekId": f"geek-{index}", "encryptJobId": "job-open", "securityId": f"security-{index}", "name": f"候选人 {index}"},
                )
                inventory.ensure_resume(candidate_id, lambda: {"work_experience": []})
                inventory.record_evaluation(
                    "job-open",
                    candidate_id,
                    {"schema_version": 2, "contract": "continuous_ranking", "candidate_id": candidate_id, "rubric_version": "rubric-v1", "total_score": 100 - index, "evidence_coverage": 90, "dimension_scores": [], "evidence": [], "gaps": [], "risks": [], "follow_up_questions": [], "summary": "摘要"},
                )
            inventory_path = root / "candidate_inventory.json"
            inventory.save(inventory_path)
            paths = SimpleNamespace(inventory_path=inventory_path)
            run = SimpleNamespace(state={"status": "scoring_complete", "job_id": "job-open", "job_title": "平台招商负责人", "rubric_version": "rubric-v1"})
            args = parse_args(["favorite"])

            def run_sync(legacy_args, *, now):
                plan = json.loads(Path(legacy_args.plan).read_text(encoding="utf-8"))
                FavoriteRegistry(root / "account", account_key="0123456789abcdef").record_candidates(
                    ["geek-1"], source="list_sync", receipt_id="sync-1"
                )
                receipt_path = Path(legacy_args.work_dir) / "run" / "favorite_sync_receipt.json"
                receipt_path.parent.mkdir(parents=True)
                receipt_path.write_text(json.dumps({"schema_version": 1, "contract": "boss_favorite_sync_receipt", "plan_id": plan["plan_id"], "account_key": plan["account_key"], "board_date": plan["board_date"], "purpose": "favorite_delivery", "batch_id": plan["batch_id"], "batch_digest": plan["batch_digest"], "completed_at": "2026-09-03T10:00:00+08:00", "status": "end_reached", "complete": True, "pages_read": 1, "max_pages": 40, "first_page_ids": ["geek-1"], "checkpoint_advanced": True}), encoding="utf-8")
                return 0

            with patch.object(run_single_job_live_module, "workflow_paths", return_value=paths):
                with patch("boss_hire.workflow_run.load_active_workflow_run", return_value=run):
                    with patch.object(run_single_job_live_module, "favorite_account_state_dir", return_value=root / "account"):
                        with patch("boss_hire.boss_access.account_key_for", return_value="0123456789abcdef"):
                            with patch.object(run_single_job_live_module, "account_key_for", return_value="0123456789abcdef"):
                                with patch("boss_hire.single_job_run_plan.preflight_boss_access", return_value=SimpleNamespace(account_key="0123456789abcdef")):
                                    with patch.object(run_single_job_live_module, "sync_auth_from_chrome", return_value={"session_fingerprint": "session-a"}):
                                        with patch.object(run_single_job_live_module, "LiveAuthorizationStore") as store:
                                            store.return_value.issue.return_value = SimpleNamespace(authorization_id="sync-auth")
                                            first_outputs: list[str] = []
                                            with patch.object(run_single_job_live_module, "run_command", side_effect=run_sync) as execute:
                                                result = favorite_command(args, input_fn=lambda _prompt: "确认生成未收藏Top5", output_fn=first_outputs.append, now=self.fixed_now)

                                        def run_delivery(legacy_args, *, input_fn, output_fn, now):
                                            self.assertEqual(input_fn("确认收藏："), "确认5人")
                                            self.assertIsInstance(legacy_args.batch, Path)
                                            self.assertIsInstance(legacy_args.sync_receipt, Path)
                                            self.assertIsInstance(legacy_args.work_dir, Path)
                                            batch = json.loads(Path(legacy_args.batch).read_text(encoding="utf-8"))
                                            receipt_path = Path(legacy_args.work_dir) / "runs" / "one" / "favorite_delivery_receipt.json"
                                            receipt_path.parent.mkdir(parents=True)
                                            self.assertEqual(legacy_args.select, "2,3,4,5,6")
                                            receipt_path.write_text(json.dumps({"schema_version": 1, "contract": "boss_favorite_delivery_receipt", "plan_id": "favorite-one", "batch_id": batch["batch_id"], "selected_count": 5, "confirmed_count": 5, "already_confirmed_count": 0, "failed_count": 0, "unknown_count": 0, "not_selected_count": 1, "results": [{"candidate_id": f"candidate-{rank}", "rank": rank, "status": "favorite_confirmed"} for rank in range(6, 1, -1)]}), encoding="utf-8")
                                            return 0

                                        outputs: list[str] = []
                                        with patch.object(run_single_job_live_module, "_favorite_command_with_advanced_arguments", side_effect=run_delivery) as deliver:
                                            responses = iter(["", "确认收藏5人"])
                                            with patch.object(run_single_job_live_module, "_local_now", return_value=self.fixed_now()):
                                                delivery_result = favorite_command(
                                                    args,
                                                    input_fn=lambda _prompt: next(responses),
                                                    output_fn=outputs.append,
                                                    now=None,
                                                )

            self.assertEqual(result, 0)
            issued_plan = store.return_value.issue.call_args.kwargs["plan"]
            self.assertTrue(issued_plan["execution_policy"]["fixed_auth_namespace"])
            self.assertTrue(issued_plan["execution_policy"]["fixed_guard_namespace"])
            self.assertEqual(issued_plan["execution_policy"]["authorization_mode"], "one_time")
            execute.assert_called_once()
            first_rendered = "\n".join(first_outputs)
            self.assertNotIn("1. 候选人 1", first_rendered)
            self.assertIn("1. 候选人 2", first_rendered)
            self.assertIn("5. 候选人 6", first_rendered)
            self.assertEqual(delivery_result, 0)
            deliver.assert_called_once()
            rendered = "\n".join(outputs)
            self.assertIn("新增收藏：5", rendered)
            self.assertIn("已收藏而跳过：0", rendered)
            self.assertIn("未选择：0", rendered)
            self.assertIn("本次收藏已完成", rendered)
            self.assertEqual(
                load_active_favorite_workflow_session(
                    favorite_workflow_paths(root / "account")
                ).state["status"],
                "completed",
            )

    def test_plain_favorite_display_numbers_map_to_frozen_pool_ranks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = published_batch()
            final_candidates = [batch["candidates"][2], batch["candidates"][4]]
            session = SimpleNamespace(
                state={
                    "candidate_snapshot": {
                        "candidates": batch["candidates"],
                    },
                    "final_selection": {
                        "actual_count": 2,
                        "candidates": final_candidates,
                    },
                },
                state_path=root / "session.json",
            )
            inputs = SimpleNamespace(
                batch_path=root / "pool.json",
                sync_receipt_path=root / "sync.json",
                work_dir=root / "delivery",
            )

            def run_delivery(legacy_args, *, input_fn, output_fn, now):
                self.assertEqual(legacy_args.select, "3")
                receipt_path = inputs.work_dir / "run" / "favorite_delivery_receipt.json"
                receipt_path.parent.mkdir(parents=True)
                receipt_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "contract": "boss_favorite_delivery_receipt",
                            "batch_id": batch["batch_id"],
                            "confirmed_count": 1,
                            "already_confirmed_count": 0,
                            "failed_count": 0,
                            "unknown_count": 0,
                            "not_selected_count": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                return 0

            outputs: list[str] = []
            with patch(
                "boss_hire.favorite_workflow_command.materialize_favorite_workflow_delivery_inputs",
                return_value=inputs,
            ):
                with patch(
                    "boss_hire.favorite_workflow_command.record_favorite_workflow_delivery",
                    return_value=SimpleNamespace(state={"status": "completed"}),
                ):
                    with patch.object(
                        run_single_job_live_module,
                        "_favorite_command_with_advanced_arguments",
                        side_effect=run_delivery,
                    ):
                        responses = iter(["1", "确认收藏1人"])
                        result = run_single_job_live_module._normal_favorite_delivery_command(
                            session,
                            input_fn=lambda _prompt: next(responses),
                            output_fn=outputs.append,
                            now=self.fixed_now,
                        )

            self.assertEqual(result, 0)
            rendered = "\n".join(outputs)
            self.assertIn("1.", rendered)
            self.assertIn("候选人 3", rendered)
            self.assertNotIn("候选人 1", rendered)

    def test_plain_favorite_result_explains_zero_new_writes(self) -> None:
        outputs: list[str] = []

        run_single_job_live_module._render_normal_favorite_result(
            {
                "selected_count": 1,
                "confirmed_count": 0,
                "already_confirmed_count": 1,
                "failed_count": 0,
                "unknown_count": 0,
            },
            final_count=1,
            output_fn=outputs.append,
        )

        rendered = "\n".join(outputs)
        self.assertIn("新增收藏：0", rendered)
        self.assertIn("已收藏而跳过：1", rendered)
        self.assertIn("失败：0", rendered)
        self.assertIn("结果不明：0", rendered)
        self.assertIn("本次收藏已完成", rendered)

    def test_preview_is_one_selection_and_writes_exact_local_plan_without_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir, _account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            outputs: list[str] = []
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(root / "deliveries"),
                ]
            )

            with patch(
                "scripts.run_single_job_live.sync_auth_from_chrome",
                side_effect=AssertionError("preview must not sync auth"),
            ):
                exit_code = favorite_command(
                    args,
                    input_fn=lambda _prompt: "1,2,4",
                    output_fn=outputs.append,
                    now=self.fixed_now,
                )

            self.assertEqual(exit_code, 0)
            plans = list((root / "deliveries" / "previews").glob("*/favorite_delivery_plan.json"))
            self.assertEqual(len(plans), 1)
            plan = json.loads(plans[0].read_text(encoding="utf-8"))
            self.assertEqual([row["rank"] for row in plan["candidates"]], [4, 2, 1])
            self.assertEqual(plan["not_selected_count"], 2)
            rendered = "\n".join(outputs)
            self.assertIn("请选择要收藏的人", rendered)
            self.assertIn("本地预览", rendered)

    def test_invalid_selection_fails_before_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir, _account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(root / "deliveries"),
                    "--select",
                    "1,1",
                    "--live",
                ]
            )

            with patch(
                "scripts.run_single_job_live.sync_auth_from_chrome",
                side_effect=AssertionError("invalid selection must not sync auth"),
            ):
                with self.assertRaises(ValueError):
                    favorite_command(
                        args,
                        input_fn=lambda _prompt: "确认1人",
                        output_fn=lambda _message: None,
                        now=self.fixed_now,
                    )

    def test_wrong_confirmation_fails_before_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir, _account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(root / "deliveries"),
                    "--select",
                    "1,2,4",
                    "--live",
                ]
            )

            with patch(
                "scripts.run_single_job_live.sync_auth_from_chrome",
                side_effect=AssertionError("wrong confirmation must not sync auth"),
            ):
                with self.assertRaisesRegex(BossLiveAccessDenied, "确认文本"):
                    favorite_command(
                        args,
                        input_fn=lambda _prompt: "确认2人",
                        output_fn=lambda _message: None,
                        now=self.fixed_now,
                    )

    def test_incomplete_sync_receipt_fails_before_auth_or_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir, _account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(
                root,
                auth_dir=auth_dir,
                complete=False,
            )
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(root / "deliveries"),
                    "--select",
                    "1",
                    "--live",
                ]
            )

            with patch.object(
                run_single_job_live_module,
                "sync_auth_from_chrome",
                side_effect=AssertionError("incomplete sync must fail before auth"),
            ):
                with self.assertRaisesRegex(ValueError, "complete"):
                    favorite_command(
                        args,
                        input_fn=lambda _prompt: "确认1人",
                        output_fn=lambda _message: None,
                        now=self.fixed_now,
                    )

    def test_live_command_freezes_plan_issues_and_consumes_auth_then_executes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir = root / "auth"
            guard_dir = root / "guard"
            work_dir = root / "deliveries"
            self.configure_account(root, auth_dir=auth_dir)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(work_dir),
                    "--select",
                    "1,2,4",
                    "--retry-definite-failures",
                    "--live",
                ]
            )
            issued = SimpleNamespace(authorization_id="favorite-auth-1")
            consumed = SimpleNamespace(authorization_id="favorite-auth-1")
            receipt_path = work_dir / "runs" / "receipt.json"
            execution_result = {
                "receipt": {
                    "confirmed_count": 3,
                    "already_confirmed_count": 0,
                    "failed_count": 0,
                    "unknown_count": 0,
                    "not_selected_count": 2,
                },
                "receipt_path": receipt_path,
            }
            outputs: list[str] = []
            with patch.object(run_single_job_live_module, "FIXED_GUARD_DIR", guard_dir):
                with patch.object(
                    run_single_job_live_module,
                    "sync_auth_from_chrome",
                    return_value={"session_fingerprint": "session-a"},
                ) as sync:
                    with patch.object(
                        run_single_job_live_module,
                        "LiveAuthorizationStore",
                    ) as store_class:
                        store_class.return_value.issue.return_value = issued
                        store_class.return_value.consume.return_value = consumed
                        with patch.object(
                            run_single_job_live_module,
                            "_execute_favorite_live_plan",
                            return_value=execution_result,
                        ) as execute:
                            result = favorite_command(
                                args,
                                input_fn=lambda _prompt: "确认3人",
                                output_fn=outputs.append,
                                now=self.fixed_now,
                            )

            self.assertEqual(result, 0)
            sync.assert_called_once_with(auth_dir)
            issued_plan = store_class.return_value.issue.call_args.kwargs["plan"]
            self.assertEqual(issued_plan["board_date"], "2026-09-03")
            self.assertTrue(issued_plan["retry_definite_failures"])
            self.assertEqual(issued_plan["operation_manifest_kind"], "favorite_delivery")
            self.assertEqual(len(issued_plan["operation_manifest"]), 6)
            self.assertEqual(
                store_class.return_value.consume.call_args.kwargs["plan"],
                issued_plan,
            )
            execute.assert_called_once()
            self.assertEqual(execute.call_args.kwargs["registry"].root, root / "account-state")
            self.assertEqual(
                execute.call_args.kwargs["ledger"].path,
                root / "account-state" / "delivery_ledger.json",
            )
            frozen = list((work_dir / "plans").glob("*/favorite_delivery_plan.json"))
            self.assertEqual(len(frozen), 1)
            self.assertEqual(json.loads(frozen[0].read_text(encoding="utf-8")), issued_plan)
            self.assertIn("已确认收藏：3", "\n".join(outputs))

    def test_live_batch_change_after_confirmation_fails_before_auth_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_dir, _account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            batch_path = self.write_batch(root)
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(batch_path),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(root / "deliveries"),
                    "--select",
                    "1",
                    "--live",
                ]
            )

            def confirm(_prompt: str) -> str:
                changed = json.loads(batch_path.read_text(encoding="utf-8"))
                changed["candidates"][0]["summary"] = "确认后被修改"
                batch_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
                return "确认1人"

            with patch.object(
                run_single_job_live_module,
                "sync_auth_from_chrome",
                side_effect=AssertionError("changed batch must fail before auth sync"),
            ):
                with self.assertRaisesRegex(BossLiveAccessDenied, "发生变化"):
                    favorite_command(
                        args,
                        input_fn=confirm,
                        output_fn=lambda _message: None,
                        now=self.fixed_now,
                    )

    def test_all_selected_candidates_already_confirmed_need_no_auth_or_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_dir = root / "deliveries"
            auth_dir, account_state = self.configure_account(root)
            sync_receipt = self.write_sync_receipt(root, auth_dir=auth_dir)
            ledger = FavoriteDeliveryLedger(work_dir / "favorite_delivery_state.json")
            ledger.set_status(
                "candidate-1",
                "favorite_confirmed",
                batch_id="older-batch",
                operation_key="favorite:older-batch:candidate-1:write",
            )
            ledger.save()
            args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(self.write_batch(root)),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(work_dir),
                    "--select",
                    "1",
                    "--live",
                ]
            )

            outputs: list[str] = []
            with patch.object(
                run_single_job_live_module,
                "sync_auth_from_chrome",
                side_effect=AssertionError("idempotent skip must not sync auth"),
            ):
                result = favorite_command(
                    args,
                    input_fn=lambda _prompt: "确认1人",
                    output_fn=outputs.append,
                    now=self.fixed_now,
                )

            self.assertEqual(result, 0)
            receipts = list((work_dir / "runs").glob("*/favorite_delivery_receipt.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["already_confirmed_count"], 1)
            self.assertEqual(receipt["confirmed_count"], 0)
            self.assertTrue((account_state / "delivery_ledger.json").is_file())
            self.assertTrue((work_dir / "favorite_delivery_state.json").is_file())
            self.assertIn("源文件保留", "\n".join(outputs))

            other_work_dir = root / "other-deliveries"
            other_args = parse_args(
                [
                    "favorite",
                    "--batch",
                    str(root / "candidate_shortlist.json"),
                    "--sync-receipt",
                    str(sync_receipt),
                    "--work-dir",
                    str(other_work_dir),
                    "--select",
                    "1",
                ]
            )
            favorite_command(
                other_args,
                input_fn=lambda _prompt: "1",
                output_fn=lambda _message: None,
                now=self.fixed_now,
            )
            second_plan_path = next(
                (other_work_dir / "previews").glob("*/favorite_delivery_plan.json")
            )
            second_plan = json.loads(second_plan_path.read_text(encoding="utf-8"))
            self.assertEqual(second_plan["candidates"][0]["action"], "already_confirmed")
            self.assertFalse((other_work_dir / "favorite_delivery_state.json").exists())


if __name__ == "__main__":
    unittest.main()
