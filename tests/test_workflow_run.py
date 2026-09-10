from __future__ import annotations

import unittest
import json
import os
import stat
import tempfile
from pathlib import Path

from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory
from boss_hire.boss_access import account_key_for
from boss_hire.workflow_status import (
    build_workflow_status,
    freeze_detail_plan,
    prepare_detail_action,
    verify_frozen_detail_plan,
)
from boss_hire.workflow_run import (
    build_workflow_run_state,
    create_workflow_run,
    create_workflow_run_with_artifacts,
    ensure_workflow_directories,
    load_active_workflow_run,
    resolve_run_config,
    save_active_workflow_run,
    transition_workflow_run_state,
    validate_workflow_run_state,
    workflow_paths,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64


def run_state() -> dict[str, object]:
    return build_workflow_run_state(
        run_id="20260907-120000-search_top5-abcd1234",
        config_name="search_top5",
        config={
            "recommendation_source_enabled": 0,
            "top_priority_search_query_count": 5,
        },
        config_digest=DIGEST_A,
        board_date="2026-09-07",
        account_key="0123456789abcdef",
        job_id="job-open",
        source_jd_hash=DIGEST_B,
        search_plan_version="search-v3",
        search_plan_digest=DIGEST_C,
        rubric_version="rubric-v3",
        rubric_digest=DIGEST_D,
        created_at="2026-09-07T12:00:00+08:00",
    )


class WorkflowRunStateTests(unittest.TestCase):
    def test_builds_frozen_initial_state_with_source_next_action(self) -> None:
        state = run_state()

        self.assertEqual(state["status"], "awaiting_source_confirmation")
        self.assertEqual(state["next_action"], "source_collection")
        self.assertEqual(state["config"]["top_priority_search_query_count"], 5)
        self.assertEqual(state["artifacts"], {})

    def test_accepts_current_config_and_preserves_historical_states(self) -> None:
        historical = run_state()
        before = json.loads(json.dumps(historical))
        normalized_historical = validate_workflow_run_state(historical)
        self.assertEqual(normalized_historical["config"], before["config"])
        self.assertEqual(content_hash(normalized_historical), content_hash(before))

        current = run_state()
        current["config"] = {
            **current["config"],
            "second_page_search_query_count": 3,
        }
        normalized_current = validate_workflow_run_state(current)
        self.assertEqual(normalized_current["config"]["second_page_search_query_count"], 3)

        filtered = run_state()
        filtered["config"] = {
            **filtered["config"],
            "second_page_search_query_count": 0,
            "recent_view_filter": "exclude_14d",
        }
        normalized_filtered = validate_workflow_run_state(filtered)
        self.assertEqual(normalized_filtered["config"]["recent_view_filter"], "exclude_14d")

        for invalid in (-1, True, "1", 6):
            candidate = run_state()
            candidate["config"] = {
                **candidate["config"],
                "second_page_search_query_count": invalid,
            }
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_workflow_run_state(candidate)

        for invalid in ("", "exclude_30d", 1, True):
            candidate = run_state()
            candidate["config"] = {
                **candidate["config"],
                "second_page_search_query_count": 0,
                "recent_view_filter": invalid,
            }
            with self.subTest(recent_view_filter=invalid), self.assertRaises(ValueError):
                validate_workflow_run_state(candidate)

    def test_rejects_unknown_config_fields_and_unsafe_identity(self) -> None:
        invalid = run_state()
        invalid["config"] = {**invalid["config"], "work_dir": "/tmp/other"}
        with self.assertRaisesRegex(ValueError, "来源配置"):
            validate_workflow_run_state(invalid)

        invalid = run_state()
        invalid["run_id"] = "../other-run"
        with self.assertRaisesRegex(ValueError, "不安全"):
            validate_workflow_run_state(invalid)

    def test_rejects_status_next_action_mismatch_and_illegal_transition(self) -> None:
        invalid = run_state()
        invalid["next_action"] = "llm_scoring"
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_workflow_run_state(invalid)

        with self.assertRaisesRegex(ValueError, "非法运行状态转换"):
            transition_workflow_run_state(
                run_state(),
                status="scoring_complete",
                updated_at="2026-09-07T12:01:00+08:00",
            )

    def test_transition_records_only_relative_digest_bound_artifacts(self) -> None:
        state = transition_workflow_run_state(
            run_state(),
            status="source_complete",
            updated_at="2026-09-07T12:01:00+08:00",
            artifacts={
                "source_plan": {"path": "source_plan.json", "digest": DIGEST_A},
                "source_receipt": {"path": "source_receipt.json", "digest": DIGEST_B},
            },
        )

        self.assertEqual(state["next_action"], "candidate_details")
        self.assertEqual(set(state["artifacts"]), {"source_plan", "source_receipt"})
        with self.assertRaisesRegex(ValueError, "相对路径"):
            transition_workflow_run_state(
                run_state(),
                status="source_complete",
                updated_at="2026-09-07T12:01:00+08:00",
                artifacts={
                    "source_plan": {"path": "../source_plan.json", "digest": DIGEST_A}
                },
            )


class WorkflowPathTests(unittest.TestCase):
    def test_paths_are_fixed_under_project_root_and_private_when_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "project"
            paths = workflow_paths(project_root)

            stable_project_root = project_root.resolve()
            self.assertEqual(paths.config_root, stable_project_root / "data/local/run_configs")
            self.assertEqual(paths.work_root, stable_project_root / "data/local/single_job_runs")
            self.assertEqual(paths.inventory_path, paths.work_root / "candidate_inventory.json")
            ensure_workflow_directories(paths)
            self.assertEqual(stat.S_IMODE(paths.work_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(paths.runs_root.stat().st_mode), 0o700)

    def test_resolves_historical_and_page2_configs_by_safe_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run_configs"
            root.mkdir()
            (root / "search_top5.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 0,
                        "top_priority_search_query_count": 5,
                    }
                ),
                encoding="utf-8",
            )

            resolved = resolve_run_config("search_top5", config_root=root)
            with_suffix = resolve_run_config("search_top5.json", config_root=root)

            self.assertEqual(resolved.name, "search_top5")
            self.assertEqual(resolved.value["top_priority_search_query_count"], 5)
            self.assertEqual(resolved.value["second_page_search_query_count"], 0)
            self.assertEqual(resolved.value["recent_view_filter"], "include_all")
            self.assertEqual(resolved.digest, with_suffix.digest)
            self.assertEqual(len(resolved.digest), 64)

            (root / "search_top3_page2.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 0,
                        "top_priority_search_query_count": 3,
                        "second_page_search_query_count": 3,
                    }
                ),
                encoding="utf-8",
            )
            page2 = resolve_run_config("search_top3_page2", config_root=root)
            self.assertEqual(page2.value["second_page_search_query_count"], 3)

            (root / "search_top3_filtered.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 0,
                        "top_priority_search_query_count": 3,
                        "second_page_search_query_count": 0,
                        "recent_view_filter": "exclude_14d",
                    }
                ),
                encoding="utf-8",
            )
            filtered = resolve_run_config("search_top3_filtered", config_root=root)
            self.assertEqual(filtered.value["recent_view_filter"], "exclude_14d")

    def test_rejects_paths_missing_configs_unknown_fields_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run_configs"
            root.mkdir()
            (root / "invalid.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 1,
                        "top_priority_search_query_count": 1,
                        "work_dir": "/tmp/other",
                    }
                ),
                encoding="utf-8",
            )
            outside = Path(temporary) / "outside.json"
            outside.write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 1,
                        "top_priority_search_query_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            symlink = root / "linked.json"
            try:
                os.symlink(outside, symlink)
            except (OSError, NotImplementedError):
                symlink = None

            for name in ("../outside", "nested/config", "/absolute", "missing"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    resolve_run_config(name, config_root=root)
            with self.assertRaisesRegex(ValueError, "未知字段"):
                resolve_run_config("invalid", config_root=root)
            if symlink is not None:
                with self.assertRaisesRegex(ValueError, "符号链接"):
                    resolve_run_config("linked", config_root=root)


class WorkflowPersistenceTests(unittest.TestCase):
    def test_initial_artifacts_are_written_private_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            artifact = {"contract": "source-plan", "value": 1}
            state = run_state()
            state["artifacts"] = {
                "source_plan": {
                    "path": "source_plan.json",
                    "digest": content_hash(artifact),
                }
            }

            created = create_workflow_run_with_artifacts(
                paths,
                state,
                artifact_values={"source_plan": artifact},
            )

            artifact_path = created.state_path.parent / "source_plan.json"
            self.assertEqual(json.loads(artifact_path.read_text(encoding="utf-8")), artifact)
            self.assertEqual(stat.S_IMODE(artifact_path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, "产物与状态引用"):
                create_workflow_run_with_artifacts(
                    paths,
                    {**run_state(), "run_id": "other-run"},
                    artifact_values={"source_plan": artifact},
                )

    def test_only_one_nonterminal_run_is_active_and_terminal_allows_a_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            first = create_workflow_run(paths, run_state())

            second_state = run_state()
            second_state["run_id"] = "20260907-130000-search_top5-efgh5678"
            second_state["created_at"] = "2026-09-07T13:00:00+08:00"
            second_state["updated_at"] = "2026-09-07T13:00:00+08:00"
            with self.assertRaisesRegex(ValueError, "已有活动运行"):
                create_workflow_run(paths, second_state)

            closed = transition_workflow_run_state(
                first.state,
                status="closed",
                updated_at="2026-09-07T12:05:00+08:00",
            )
            save_active_workflow_run(paths, closed, expected_state_digest=first.state_digest)
            second = create_workflow_run(paths, second_state)

            self.assertEqual(load_active_workflow_run(paths).state["run_id"], second.state["run_id"])
            self.assertEqual(stat.S_IMODE(paths.active_run_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(second.state_path.stat().st_mode), 0o600)

    def test_active_pointer_state_and_artifact_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            created = create_workflow_run(paths, run_state())
            pointer = json.loads(paths.active_run_path.read_text(encoding="utf-8"))
            pointer["state_digest"] = DIGEST_B
            paths.active_run_path.write_text(json.dumps(pointer), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "摘要不一致"):
                load_active_workflow_run(paths)

            paths.active_run_path.write_text(
                json.dumps(
                    {
                        **pointer,
                        "state_digest": created.state_digest,
                    }
                ),
                encoding="utf-8",
            )
            artifact = {"contract": "source-plan", "value": 1}
            artifact_path = created.state_path.parent / "source_plan.json"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            source_complete = transition_workflow_run_state(
                created.state,
                status="source_complete",
                updated_at="2026-09-07T12:02:00+08:00",
                artifacts={
                    "source_plan": {
                        "path": "source_plan.json",
                        "digest": content_hash(artifact),
                    }
                },
            )
            saved = save_active_workflow_run(
                paths,
                source_complete,
                expected_state_digest=created.state_digest,
            )
            with self.assertRaisesRegex(ValueError, "状态已变化"):
                save_active_workflow_run(
                    paths,
                    saved.state,
                    expected_state_digest=created.state_digest,
                )

            artifact["value"] = 2
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "产物 source_plan 摘要不一致"):
                load_active_workflow_run(paths)


class WorkflowStatusTests(unittest.TestCase):
    def _source_complete_run(
        self,
        paths,
        *,
        with_pending: int,
        account_key: str = "0123456789abcdef",
    ) -> None:
        config = {
            "recommendation_source_enabled": 1,
            "top_priority_search_query_count": 0,
        }
        jd_digest = content_hash("jd-current")
        rubric = {"source_jd_hash": jd_digest, "version": "rubric-v1"}
        search_plan = {"source_jd_hash": jd_digest, "version": "search-v1"}
        state = build_workflow_run_state(
            run_id="20260907-120000-recommendation-abcd1234",
            config_name="recommendation",
            config=config,
            config_digest=content_hash(config),
            board_date="2026-09-07",
            account_key=account_key,
            job_id="job-open",
            source_jd_hash=jd_digest,
            search_plan_version="search-v1",
            search_plan_digest=content_hash(search_plan),
            rubric_version="rubric-v1",
            rubric_digest=content_hash(rubric),
            created_at="2026-09-07T12:00:00+08:00",
        )
        created = create_workflow_run(paths, state)
        source_complete = transition_workflow_run_state(
            created.state,
            status="source_complete",
            updated_at="2026-09-07T12:01:00+08:00",
        )
        save_active_workflow_run(paths, source_complete, expected_state_digest=created.state_digest)
        inventory = CandidateInventory()
        inventory.record_job_artifacts(
            "job-open",
            jd_digest,
            rubric=rubric,
            search_plan=search_plan,
        )
        for index in range(with_pending):
            inventory.record_source_card(
                f"candidate-{index + 1}",
                "recommendation",
                {
                    "encryptGeekId": f"boss-{index + 1}",
                    "encryptJobId": "job-open",
                    "securityId": f"security-{index + 1}",
                },
            )
        inventory.save(paths.inventory_path)

    def test_detail_action_defaults_to_all_and_can_select_a_smaller_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            self._source_complete_run(paths, with_pending=3)

            action = prepare_detail_action(
                paths,
                selection=2,
                updated_at="2026-09-07T12:02:00+08:00",
            )

        self.assertEqual(action["status"], "awaiting_confirmation")
        self.assertEqual(action["candidate_ids"], ["candidate-1", "candidate-2"])
        self.assertEqual(action["selection"]["remaining_pending_count"], 1)
        self.assertEqual(action["boss_requests"], 0)

    def test_detail_action_skips_cached_resumes_and_advances_when_none_are_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            self._source_complete_run(paths, with_pending=1)
            inventory = CandidateInventory.load(paths.inventory_path)
            inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})
            inventory.save(paths.inventory_path)

            action = prepare_detail_action(
                paths,
                updated_at="2026-09-07T12:02:00+08:00",
            )

            active = load_active_workflow_run(paths)
        self.assertEqual(action["status"], "skipped_no_pending")
        self.assertEqual(action["candidate_ids"], [])
        self.assertEqual(action["next_action"], "llm_scoring")
        self.assertEqual(active.state["status"], "detail_complete")
        self.assertEqual(action["boss_requests"], 0)

    def test_detail_plan_freezes_exact_pool_and_drift_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = workflow_paths(root / "project")
            auth_dir = root / "auth"
            self._source_complete_run(
                paths,
                with_pending=3,
                account_key=account_key_for(auth_dir),
            )

            plan, saved = freeze_detail_plan(
                paths,
                selection=2,
                auth_dir=auth_dir,
                updated_at="2026-09-07T12:02:00+08:00",
            )
            verified, _ = verify_frozen_detail_plan(paths, auth_dir=auth_dir)
            self.assertEqual(plan, verified)
            self.assertEqual(plan["selected_count"], 2)
            self.assertEqual(
                [row["candidate_id"] for row in plan["candidates"]],
                ["candidate-1", "candidate-2"],
            )
            self.assertIn("detail_plan", saved.state["artifacts"])

            inventory = CandidateInventory.load(paths.inventory_path)
            inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})
            inventory.save(paths.inventory_path)
            with self.assertRaisesRegex(ValueError, "冻结详情计划与当前 pending"):
                verify_frozen_detail_plan(paths, auth_dir=auth_dir)

    def test_no_active_run_does_not_guess_from_dormant_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            paths.runs_root.mkdir(parents=True)
            (paths.runs_root / "old-run").mkdir()

            status = build_workflow_status(paths)

        self.assertEqual(status["status"], "no_active_run")
        self.assertEqual(status["next_action"], "start")
        self.assertEqual(status["boss_requests"], 0)

    def test_status_recomputes_funnel_and_rejects_job_artifact_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = workflow_paths(Path(temporary) / "project")
            config = {
                "recommendation_source_enabled": 0,
                "top_priority_search_query_count": 1,
            }
            jd_digest = content_hash("jd-current")
            rubric = {"source_jd_hash": jd_digest, "version": "rubric-v1"}
            search_plan = {"source_jd_hash": jd_digest, "version": "search-v1"}
            state = build_workflow_run_state(
                run_id="20260907-120000-search-one-abcd1234",
                config_name="search_one",
                config=config,
                config_digest=content_hash(config),
                board_date="2026-09-07",
                account_key="0123456789abcdef",
                job_id="job-open",
                source_jd_hash=jd_digest,
                search_plan_version="search-v1",
                search_plan_digest=content_hash(search_plan),
                rubric_version="rubric-v1",
                rubric_digest=content_hash(rubric),
                created_at="2026-09-07T12:00:00+08:00",
            )
            create_workflow_run(paths, state)
            inventory = CandidateInventory()
            inventory.record_job_artifacts(
                "job-open",
                jd_digest,
                rubric=rubric,
                search_plan=search_plan,
            )
            inventory.record_source_card(
                "candidate-a",
                "search",
                {"encryptGeekId": "boss-a", "encryptJobId": "job-open"},
            )
            inventory.record_source_card(
                "candidate-b",
                "search",
                {"encryptGeekId": "boss-b", "encryptJobId": "job-open"},
            )
            inventory.ensure_resume("candidate-b", lambda: {"work_experience": []})
            inventory.save(paths.inventory_path)

            status = build_workflow_status(paths)
            self.assertEqual(status["funnel"]["candidate_card_count"], 2)
            self.assertEqual(status["funnel"]["pending_detail_count"], 1)
            self.assertEqual(status["funnel"]["pending_score_count"], 1)
            self.assertEqual(status["boss_requests"], 0)

            drifted = CandidateInventory.load(paths.inventory_path)
            drifted.record_job_artifacts(
                "job-open",
                jd_digest,
                rubric={**rubric, "criteria": ["changed"]},
                search_plan=search_plan,
            )
            drifted.save(paths.inventory_path)
            with self.assertRaisesRegex(ValueError, "rubric 摘要"):
                build_workflow_status(paths)

if __name__ == "__main__":
    unittest.main()
