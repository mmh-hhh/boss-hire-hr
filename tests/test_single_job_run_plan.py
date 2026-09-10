from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from boss_hire.boss_access import account_key_for
from boss_hire.single_job_run_plan import (
    RUN_PLAN_CONTRACT,
    SingleJobRunConfig,
    build_candidate_detail_operation_manifest,
    build_favorite_delivery_operation_manifest,
    build_favorite_sync_operation_manifest,
    build_favorite_sync_run_plan,
    build_candidate_detail_run_plan,
    build_single_job_run_plan,
    build_source_operation_manifest,
    load_single_job_run_plan,
    read_single_job_run_config,
    write_single_job_run_plan,
)


def config(
    *,
    recommendation_source_enabled: int = 1,
    top_priority_search_query_count: int = 0,
    second_page_search_query_count: int = 0,
    recent_view_filter: str = "include_all",
) -> SingleJobRunConfig:
    return SingleJobRunConfig.from_mapping(
        {
            "recommendation_source_enabled": recommendation_source_enabled,
            "top_priority_search_query_count": top_priority_search_query_count,
            "second_page_search_query_count": second_page_search_query_count,
            "recent_view_filter": recent_view_filter,
        }
    )


SEARCH_PLAN = {
    "schema_version": 1,
    "contract": "generic_search_plan",
    "version": "search-current",
    "source_jd_hash": "jd-current",
    "routes": [
        {"id": "title_match", "query": "平台招商负责人"},
        {"id": "merchant", "query": "重点商家拓展"},
        {"id": "industry", "query": "汽配平台招商"},
    ],
}

SEARCH_PLAN_V2 = {
    "schema_version": 2,
    "contract": "generic_search_plan",
    "version": "search-v2-current",
    "source_jd_hash": "jd-current",
    "strategy": {
        "target_persona": "做过平台招商的汽配人",
        "dominant_axis": "industry",
        "dominant_anchor": "汽配",
        "jd_evidence": ["汽配平台招商"],
    },
    "routes": [
        {"id": "third", "priority": 3, "query": "汽配产业带招商"},
        {"id": "first", "priority": 1, "query": "汽配平台招商"},
        {"id": "second", "priority": 2, "query": "跨境汽配招商"},
    ],
}

SEARCH_PLAN_V3 = {
    "schema_version": 3,
    "contract": "generic_search_plan",
    "version": "search-v3-current",
    "source_jd_hash": "jd-current",
    "target_route_count": 8,
    "generation_shortfall": 5,
    "strategy": {
        "target_persona": "做过平台招商的汽配人",
        "dominant_axis": "industry",
        "dominant_anchor": "汽配",
        "jd_evidence": ["汽配平台招商"],
    },
    "routes": [
        {
            "id": "third",
            "priority": 3,
            "tokens": ["汽配", "供应链", "招商"],
            "query": "汽配 供应链 招商",
            "signature": ["anchor", "ecosystem", "role"],
        },
        {
            "id": "first",
            "priority": 1,
            "tokens": ["汽配", "平台招商"],
            "query": "汽配 平台招商",
            "signature": ["anchor", "role"],
        },
        {
            "id": "second",
            "priority": 2,
            "tokens": ["汽配", "商家拓展"],
            "query": "汽配 商家拓展",
            "signature": ["anchor", "role"],
        },
    ],
}


class SingleJobRunPlanTests(unittest.TestCase):
    def test_favorite_sync_manifest_freezes_exact_pages_one_through_forty(self) -> None:
        manifest = build_favorite_sync_operation_manifest(plan_id="favorite-sync-demo")

        self.assertEqual(len(manifest), 40)
        self.assertEqual(manifest[0]["operation_key"], "favorite-sync:favorite-sync-demo:page:1")
        self.assertEqual(manifest[-1]["operation_key"], "favorite-sync:favorite-sync-demo:page:40")
        self.assertEqual(
            {(item["method"], item["endpoint_name"], item["request_class"]) for item in manifest},
            {("GET", "favorite_list", "list")},
        )
        self.assertEqual([item["binding"]["page"] for item in manifest], list(range(1, 41)))
        self.assertTrue(all(item["binding"]["tag"] == 4 for item in manifest))

    def test_favorite_sync_plan_binds_checkpoint_account_date_and_purpose(self) -> None:
        checkpoint = {
            "schema_version": 1,
            "contract": "boss_favorite_sync_checkpoint",
            "account_key": account_key_for(Path("data/local/auth")),
            "initialized_complete": True,
            "anchor_group": ["geek-1", "geek-2"],
        }

        plan = build_favorite_sync_run_plan(
            board_date="2026-09-04",
            auth_dir=Path("data/local/auth"),
            mode="incremental",
            purpose="publish",
            checkpoint=checkpoint,
        )

        self.assertEqual(plan["contract"], RUN_PLAN_CONTRACT)
        self.assertEqual(plan["plan_kind"], "favorite_registry_sync")
        self.assertEqual(plan["operation_manifest_kind"], "favorite_registry_sync")
        self.assertEqual(plan["max_pages"], 40)
        self.assertEqual(len(plan["operation_manifest"]), 40)
        self.assertIsNotNone(plan["checkpoint_digest"])
        self.assertNotIn("batch_id", plan)

    def test_favorite_delivery_sync_binds_immutable_batch_digest(self) -> None:
        batch = {
            "schema_version": 1,
            "contract": "candidate_shortlist_batch",
            "batch_id": "shortlist-demo",
            "candidates": [],
        }

        plan = build_favorite_sync_run_plan(
            board_date="2026-09-04",
            auth_dir=Path("data/local/auth"),
            mode="initialize",
            purpose="favorite_delivery",
            checkpoint=None,
            batch=batch,
        )

        self.assertEqual(plan["batch_id"], "shortlist-demo")
        self.assertTrue(plan["batch_digest"])
        changed = build_favorite_sync_run_plan(
            board_date="2026-09-04",
            auth_dir=Path("data/local/auth"),
            mode="initialize",
            purpose="favorite_delivery",
            checkpoint=None,
            batch={**batch, "candidates": [{"candidate_id": "changed"}]},
        )
        self.assertNotEqual(plan["plan_id"], changed["plan_id"])

    def test_favorite_sync_plan_rejects_unbound_or_expansive_inputs(self) -> None:
        checkpoint = {
            "contract": "boss_favorite_sync_checkpoint",
            "account_key": account_key_for(Path("data/local/auth")),
            "initialized_complete": True,
            "anchor_group": ["geek-1"],
        }
        cases = (
            {"mode": "full", "purpose": "publish", "checkpoint": checkpoint, "max_pages": 40},
            {"mode": "incremental", "purpose": "publish", "checkpoint": None, "max_pages": 40},
            {"mode": "initialize", "purpose": "unknown", "checkpoint": None, "max_pages": 40},
            {"mode": "initialize", "purpose": "publish", "checkpoint": None, "max_pages": 41},
            {"mode": "initialize", "purpose": "favorite_delivery", "checkpoint": None, "max_pages": 40},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                build_favorite_sync_run_plan(
                    board_date="2026-09-04",
                    auth_dir=Path("data/local/auth"),
                    **case,
                )

    def test_favorite_manifest_builds_adjacent_write_and_exact_read_pairs(self) -> None:
        manifest = build_favorite_delivery_operation_manifest(
            batch_id="batch-a",
            candidates=[
                {
                    "candidate_id": "candidate-a",
                    "rank": 1,
                    "encrypt_geek_id": "geek-a",
                    "encrypt_job_id": "job-open",
                    "security_id": "security-a",
                    "action": "favorite",
                },
                {
                    "candidate_id": "candidate-b",
                    "rank": 2,
                    "encrypt_geek_id": "geek-b",
                    "encrypt_job_id": "job-open",
                    "security_id": "security-b",
                    "action": "already_confirmed",
                },
                {
                    "candidate_id": "candidate-c",
                    "rank": 4,
                    "encrypt_geek_id": "geek-c",
                    "encrypt_job_id": "job-open",
                    "security_id": "security-c",
                    "action": "favorite",
                },
            ],
        )

        self.assertEqual(
            [(item["method"], item["endpoint_name"]) for item in manifest],
            [
                ("POST", "favorite_candidate"),
                ("GET", "favorite_status"),
                ("POST", "favorite_candidate"),
                ("GET", "favorite_status"),
            ],
        )
        self.assertEqual(
            [item["binding"]["candidate_id"] for item in manifest],
            ["candidate-a", "candidate-a", "candidate-c", "candidate-c"],
        )
        self.assertTrue(all(item["binding"]["batch_id"] == "batch-a" for item in manifest))

    def test_favorite_manifest_rejects_duplicate_candidates_and_unknown_actions(self) -> None:
        candidate = {
            "candidate_id": "candidate-a",
            "rank": 1,
            "encrypt_geek_id": "geek-a",
            "encrypt_job_id": "job-open",
            "security_id": "security-a",
            "action": "favorite",
        }
        with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
            build_favorite_delivery_operation_manifest(
                batch_id="batch-a",
                candidates=[candidate, candidate],
            )
        with self.assertRaisesRegex(ValueError, "action"):
            build_favorite_delivery_operation_manifest(
                batch_id="batch-a",
                candidates=[{**candidate, "action": "favorite_unknown"}],
            )

    def test_source_manifest_freezes_recommendation_and_search_route_bindings(self) -> None:
        manifest = build_source_operation_manifest(
            recommendation_source_enabled=1,
            search_routes=[
                {"id": "title", "query": "平台招商负责人", "job_id": "job-open"},
                {"id": "merchant", "query": "商家拓展", "job_id": "job-open"},
            ],
            recent_view_filter="exclude_14d",
        )

        self.assertEqual(
            [item["operation_key"] for item in manifest],
            [
                "source:recommendation:page:1",
                "source:search:title:page:1",
                "source:search:merchant:page:1",
            ],
        )
        self.assertTrue(all(item["binding"]["page"] == 1 for item in manifest))
        self.assertEqual(
            [item["binding"].get("recent_view_filter") for item in manifest[1:]],
            ["exclude_14d", "exclude_14d"],
        )

    def test_detail_manifest_uses_exact_persisted_candidate_ids(self) -> None:
        manifest = build_candidate_detail_operation_manifest(
            [
                {
                    "candidate_id": "candidate-a",
                    "encrypt_geek_id": "geek-a",
                    "encrypt_job_id": "job-open",
                    "security_id": "security-a",
                }
            ]
        )

        self.assertEqual(manifest[0]["operation_key"], "detail:candidate-a")
        self.assertEqual(manifest[0]["binding"]["encrypt_geek_id"], "geek-a")
        with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
            build_candidate_detail_operation_manifest(
                [
                    {
                        "candidate_id": "candidate-a",
                        "encrypt_geek_id": "geek-a",
                        "encrypt_job_id": "job-open",
                    },
                    {
                        "candidate_id": "candidate-a",
                        "encrypt_geek_id": "geek-b",
                        "encrypt_job_id": "job-open",
                    },
                ]
            )

    def test_detail_plan_freezes_operator_selection_independently_from_source_and_scoring(self) -> None:
        plan = build_candidate_detail_run_plan(
            board_date="2026-09-01",
            auth_dir=Path("data/local/auth"),
            selection={
                "job_id": "job-open",
                "selection": "1",
                "pending_count": 3,
                "selected_count": 1,
                "remaining_pending_count": 2,
                "candidates": [
                    {
                        "candidate_id": "candidate-a",
                        "encrypt_geek_id": "geek-a",
                        "encrypt_job_id": "job-open",
                        "security_id": "security-a",
                    }
                ],
            },
        )

        self.assertEqual(plan["operation_manifest_kind"], "candidate_details")
        self.assertEqual(plan["selection"], "1")
        self.assertEqual(plan["selected_count"], 1)
        self.assertEqual(plan["remaining_pending_count"], 2)
        self.assertEqual(plan["operation_manifest"][0]["operation_key"], "detail:candidate-a")
        self.assertNotIn("candidate_target", plan)
        self.assertNotIn("llm", plan)

    def test_config_has_four_bounded_source_controls(self) -> None:
        parsed = config(
            recommendation_source_enabled=1,
            top_priority_search_query_count=3,
            second_page_search_query_count=2,
            recent_view_filter="exclude_14d",
        )

        self.assertEqual(parsed.recommendation_source_enabled, 1)
        self.assertEqual(parsed.top_priority_search_query_count, 3)
        self.assertEqual(parsed.second_page_search_query_count, 2)
        self.assertEqual(parsed.recent_view_filter, "exclude_14d")
        self.assertEqual(
            set(parsed.__dataclass_fields__),
            {
                "recommendation_source_enabled",
                "top_priority_search_query_count",
                "second_page_search_query_count",
                "recent_view_filter",
            },
        )

    def test_config_defaults_second_page_count_to_zero_for_historical_files(self) -> None:
        parsed = SingleJobRunConfig.from_mapping(
            {
                "recommendation_source_enabled": 0,
                "top_priority_search_query_count": 3,
            }
        )

        self.assertEqual(parsed.second_page_search_query_count, 0)
        self.assertEqual(parsed.recent_view_filter, "include_all")

    def test_config_rejects_invalid_or_searchless_recent_view_filter(self) -> None:
        for invalid in ("", "exclude_30d", 1, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "recent_view_filter"
            ):
                SingleJobRunConfig.from_mapping(
                    {
                        "recommendation_source_enabled": 0,
                        "top_priority_search_query_count": 3,
                        "recent_view_filter": invalid,
                    }
                )
        with self.assertRaisesRegex(ValueError, "未启用搜索"):
            SingleJobRunConfig.from_mapping(
                {
                    "recommendation_source_enabled": 1,
                    "top_priority_search_query_count": 0,
                    "recent_view_filter": "exclude_14d",
                }
            )

    def test_config_rejects_unknown_fields_and_empty_source_selection(self) -> None:
        for field in (
            "mode",
            "candidate_target",
            "inventory_buffer_target",
            "candidate_evaluation_limit",
            "source_kind",
            "search_route_id",
            "llm",
        ):
            with self.subTest(field=field):
                value = {
                    "recommendation_source_enabled": 1,
                    "top_priority_search_query_count": 0,
                    field: "legacy",
                }
                with self.assertRaisesRegex(ValueError, "未知字段"):
                    SingleJobRunConfig.from_mapping(value)

        with self.assertRaisesRegex(ValueError, "至少启用一个候选来源"):
            config(recommendation_source_enabled=0, top_priority_search_query_count=0)

    def test_config_validates_boolean_like_flag_and_non_negative_count(self) -> None:
        for invalid in (-1, 2, True, "1"):
            with self.subTest(recommendation_source_enabled=invalid):
                with self.assertRaisesRegex(ValueError, "必须是 0 或 1"):
                    SingleJobRunConfig.from_mapping(
                        {
                            "recommendation_source_enabled": invalid,
                            "top_priority_search_query_count": 0,
                        }
                    )
        for invalid in (-1, True, "3"):
            with self.subTest(top_priority_search_query_count=invalid):
                with self.assertRaisesRegex(ValueError, "必须是非负整数"):
                    SingleJobRunConfig.from_mapping(
                        {
                            "recommendation_source_enabled": 1,
                            "top_priority_search_query_count": invalid,
                        }
                    )

        for invalid in (-1, True, "1"):
            with self.subTest(second_page_search_query_count=invalid):
                with self.assertRaisesRegex(ValueError, "必须是非负整数"):
                    SingleJobRunConfig.from_mapping(
                        {
                            "recommendation_source_enabled": 0,
                            "top_priority_search_query_count": 3,
                            "second_page_search_query_count": invalid,
                        }
                    )

        for value in (
            {
                "recommendation_source_enabled": 0,
                "top_priority_search_query_count": 2,
                "second_page_search_query_count": 3,
            },
            {
                "recommendation_source_enabled": 1,
                "top_priority_search_query_count": 0,
                "second_page_search_query_count": 1,
            },
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "不能超过"):
                SingleJobRunConfig.from_mapping(value)

    def test_example_config_contains_only_the_four_runtime_controls(self) -> None:
        example = Path(__file__).resolve().parents[1] / "configs" / "single_job_live.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        parsed = read_single_job_run_config(example)

        self.assertEqual(
            raw,
            {
                "recommendation_source_enabled": 1,
                "top_priority_search_query_count": 3,
                "second_page_search_query_count": 0,
                "recent_view_filter": "include_all",
            },
        )
        self.assertEqual(parsed, config(top_priority_search_query_count=3))

    def test_second_page_manifest_is_route_major_and_reports_shortfall(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(
                recommendation_source_enabled=0,
                top_priority_search_query_count=5,
                second_page_search_query_count=4,
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN,
        )

        self.assertEqual(plan["second_page_search_query_count"], 4)
        self.assertEqual(plan["selected_second_page_search_query_count"], 3)
        self.assertEqual(plan["second_page_search_query_shortfall"], 1)
        self.assertEqual(
            [row["operation_key"] for row in plan["operation_manifest"]],
            [
                "metadata:open-jobs",
                "metadata:single-open-job-detail",
                "source:search:title_match:page:1",
                "source:search:title_match:page:2",
                "source:search:merchant:page:1",
                "source:search:merchant:page:2",
                "source:search:industry:page:1",
                "source:search:industry:page:2",
            ],
        )
        self.assertEqual(
            [row["binding"]["page"] for row in plan["operation_manifest"][2:]],
            [1, 2, 1, 2, 1, 2],
        )

    def test_search_filters_are_resolved_once_and_bound_to_every_search_page(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(
                recommendation_source_enabled=0,
                top_priority_search_query_count=2,
                second_page_search_query_count=1,
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN,
            search_filter_inputs=("学历=本科及以上", "院校=985院校"),
        )

        self.assertEqual(
            plan["search_filter_params"],
            {"degree": "203,201", "school_level": "1104"},
        )
        self.assertEqual([row["field_label"] for row in plan["search_filters"]], ["学历", "院校要求"])
        self.assertEqual(
            [row["binding"]["search_filter_params"] for row in plan["operation_manifest"][2:]],
            [plan["search_filter_params"]] * 3,
        )
        self.assertEqual(
            [row["operation_key"] for row in plan["operation_manifest"]],
            [
                "metadata:open-jobs",
                "metadata:single-open-job-detail",
                "source:search:title_match:page:1",
                "source:search:title_match:page:2",
                "source:search:merchant:page:1",
            ],
        )

    def test_search_filter_runtime_input_keeps_legacy_recent_view_compatibility(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(recommendation_source_enabled=0, top_priority_search_query_count=1),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN,
            search_filter_inputs=("过滤近14天查看=开启",),
        )
        self.assertEqual(plan["recent_view_filter"], "exclude_14d")
        self.assertEqual(plan["search_filter_params"], {})
        self.assertEqual(plan["operation_manifest"][2]["binding"]["search_filter_params"], {})

        with self.assertRaisesRegex(ValueError, "冲突"):
            build_single_job_run_plan(
                board_date="2026-08-31",
                config=config(
                    recommendation_source_enabled=0,
                    top_priority_search_query_count=1,
                    recent_view_filter="exclude_14d",
                ),
                auth_dir=Path("data/local/auth"),
                search_job_id="job-open",
                persisted_search_plan=SEARCH_PLAN,
                search_filter_inputs=("过滤近14天查看=关闭",),
            )

    def test_v1_plan_preserves_frozen_array_order(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(top_priority_search_query_count=2),
            auth_dir=Path("data/local/private-auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN,
        )

        self.assertEqual(plan["contract"], RUN_PLAN_CONTRACT)
        self.assertEqual(plan["recommendation_source_enabled"], 1)
        self.assertEqual(plan["top_priority_search_query_count"], 2)
        self.assertEqual(plan["selected_search_query_count"], 2)
        self.assertEqual(plan["search_query_shortfall"], 0)
        self.assertEqual(
            [row["id"] for row in plan["selected_search_routes"]],
            ["title_match", "merchant"],
        )
        self.assertEqual(
            [row["operation_key"] for row in plan["operation_manifest"]],
            [
                "metadata:open-jobs",
                "metadata:single-open-job-detail",
                "source:recommendation:page:1",
                "source:search:title_match:page:1",
                "source:search:merchant:page:1",
            ],
        )
        for removed in (
            "candidate_target",
            "inventory_buffer_target",
            "candidate_detail_limit",
            "source_kind",
            "llm_call_budget",
            "llm",
            "boss_request_budget",
        ):
            self.assertNotIn(removed, plan)
        self.assertNotIn("private-auth", str(plan))

    def test_v2_plan_selects_routes_by_explicit_priority(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
            auth_dir=Path("data/local/private-auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN_V2,
        )

        self.assertEqual(
            [row["id"] for row in plan["selected_search_routes"]],
            ["first", "second"],
        )

    def test_v2_plan_rejects_invalid_priorities(self) -> None:
        invalid_plan = {
            **SEARCH_PLAN_V2,
            "routes": [
                {**SEARCH_PLAN_V2["routes"][0], "priority": 2},
                *SEARCH_PLAN_V2["routes"][1:],
            ],
        }

        with self.assertRaisesRegex(ValueError, "V2 搜索计划 priority 无效"):
            build_single_job_run_plan(
                board_date="2026-08-31",
                config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
                auth_dir=Path("data/local/private-auth"),
                search_job_id="job-open",
                persisted_search_plan=invalid_plan,
            )

    def test_v3_plan_selects_routes_by_explicit_priority(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
            auth_dir=Path("data/local/private-auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN_V3,
        )

        self.assertEqual(
            [row["id"] for row in plan["selected_search_routes"]],
            ["first", "second"],
        )
        self.assertEqual(
            [row["query"] for row in plan["selected_search_routes"]],
            ["汽配 平台招商", "汽配 商家拓展"],
        )

    def test_v3_plan_rejects_invalid_priorities(self) -> None:
        invalid_plan = {
            **SEARCH_PLAN_V3,
            "routes": [
                {**SEARCH_PLAN_V3["routes"][0], "priority": 2},
                *SEARCH_PLAN_V3["routes"][1:],
            ],
        }

        with self.assertRaisesRegex(ValueError, "V3 搜索计划 priority 无效"):
            build_single_job_run_plan(
                board_date="2026-08-31",
                config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
                auth_dir=Path("data/local/private-auth"),
                search_job_id="job-open",
                persisted_search_plan=invalid_plan,
            )

    def test_v1_v2_v3_plans_are_read_without_mutation(self) -> None:
        for persisted_plan in (SEARCH_PLAN, SEARCH_PLAN_V2, SEARCH_PLAN_V3):
            with self.subTest(schema_version=persisted_plan["schema_version"]):
                before = deepcopy(persisted_plan)
                build_single_job_run_plan(
                    board_date="2026-08-31",
                    config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
                    auth_dir=Path("data/local/private-auth"),
                    search_job_id="job-open",
                    persisted_search_plan=persisted_plan,
                )
                self.assertEqual(persisted_plan, before)

    def test_v3_plan_rejects_query_token_signature_and_shortfall_mismatches(self) -> None:
        invalid_plans = (
            (
                {
                    **SEARCH_PLAN_V3,
                    "routes": [
                        {**SEARCH_PLAN_V3["routes"][0], "query": "汽配供应链招商"},
                        *SEARCH_PLAN_V3["routes"][1:],
                    ],
                },
                "query 与 tokens",
            ),
            (
                {
                    **SEARCH_PLAN_V3,
                    "routes": [
                        {**SEARCH_PLAN_V3["routes"][0], "tokens": ["汽配 供应链", "招商"]},
                        *SEARCH_PLAN_V3["routes"][1:],
                    ],
                },
                "tokens 无效",
            ),
            (
                {
                    **SEARCH_PLAN_V3,
                    "routes": [
                        {**SEARCH_PLAN_V3["routes"][0], "signature": ["anchor", "role"]},
                        *SEARCH_PLAN_V3["routes"][1:],
                    ],
                },
                "signature 无效",
            ),
            ({**SEARCH_PLAN_V3, "generation_shortfall": 4}, "target/shortfall"),
        )

        for persisted_plan, message in invalid_plans:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                build_single_job_run_plan(
                    board_date="2026-08-31",
                    config=config(recommendation_source_enabled=0, top_priority_search_query_count=2),
                    auth_dir=Path("data/local/private-auth"),
                    search_job_id="job-open",
                    persisted_search_plan=persisted_plan,
                )

    def test_plan_reports_search_query_shortfall_without_backfill(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(recommendation_source_enabled=0, top_priority_search_query_count=5),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=SEARCH_PLAN,
        )

        self.assertEqual(plan["top_priority_search_query_count"], 5)
        self.assertEqual(plan["selected_search_query_count"], 3)
        self.assertEqual(plan["search_query_shortfall"], 2)
        self.assertEqual(len(plan["operation_manifest"]), 5)

    def test_recommendation_only_does_not_require_a_search_plan(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(),
            auth_dir=Path("data/local/auth"),
        )

        self.assertEqual(plan["selected_search_routes"], [])
        self.assertEqual(len(plan["operation_manifest"]), 3)

    def test_search_selection_fails_closed_without_current_persisted_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "持久化搜索计划"):
            build_single_job_run_plan(
                board_date="2026-08-31",
                config=config(top_priority_search_query_count=1),
                auth_dir=Path("data/local/auth"),
            )
        with self.assertRaisesRegex(ValueError, "contract 无效"):
            build_single_job_run_plan(
                board_date="2026-08-31",
                config=config(top_priority_search_query_count=1),
                auth_dir=Path("data/local/auth"),
                search_job_id="job-open",
                persisted_search_plan={**SEARCH_PLAN, "contract": "wrong"},
            )

    def test_invalid_board_date_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            build_single_job_run_plan(
                board_date="2026/08/31",
                config=config(),
                auth_dir=Path("data/local/auth"),
            )

    def test_plan_file_is_private_immutable_and_loadable(self) -> None:
        plan = build_single_job_run_plan(
            board_date="2026-08-31",
            config=config(),
            auth_dir=Path("data/local/auth"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-plan.json"
            write_single_job_run_plan(plan, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            write_single_job_run_plan(plan, path)
            self.assertEqual(load_single_job_run_plan(path), plan)
            changed = dict(plan)
            changed["plan_id"] = "changed"
            with self.assertRaisesRegex(ValueError, "不可覆盖"):
                write_single_job_run_plan(changed, path)


if __name__ == "__main__":
    unittest.main()
