from __future__ import annotations

import unittest

from boss_hire.search_plan import (
    SearchPlanConfig,
    finalize_search_plan,
    query_history_key,
    validate_query_history,
)


JD = """岗位名称：跨境汽配平台招商总监
负责制定汽配平台招商策略并建设核心商家池。
必须有汽配行业经验和汽配商家资源。
"""


def strategy() -> dict[str, object]:
    return {
        "target_persona": "做过平台招商的汽配人",
        "dominant_axis": "industry",
        "dominant_anchor": "汽配",
        "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
    }


def routes() -> list[dict[str, object]]:
    return [
        {
            "id": "industry_role",
            "priority": 1,
            "type": "industry_role",
            "query": "汽配平台招商",
            "target_persona": "直接做过汽配平台招商的人",
            "reason": "行业和职能同时匹配",
            "jd_evidence": ["汽配平台招商策略"],
        },
        {
            "id": "cross_border_role",
            "priority": 2,
            "type": "industry_role",
            "query": "跨境汽配招商",
            "target_persona": "做过跨境汽配招商的人",
            "reason": "保留跨境汽配行业锚点",
            "jd_evidence": ["跨境汽配平台招商总监"],
        },
        {
            "id": "leader_title",
            "priority": 3,
            "type": "title",
            "query": "汽配招商负责人",
            "target_persona": "负责汽配招商团队和结果的人",
            "reason": "覆盖常见负责人称谓",
            "jd_evidence": ["跨境汽配平台招商总监"],
        },
    ]


class SearchPlanTests(unittest.TestCase):
    def test_v3_compiles_factor_tokens_and_reports_honest_shortfall(self) -> None:
        v3_strategy = {
            "target_persona": "做过平台招商的汽配人",
            "dominant_axis": "industry",
            "dominant_anchor": "汽配",
            "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            "factors": [
                {
                    "id": "industry_autoparts",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "role_platform_acquisition",
                    "category": "role",
                    "token": "平台招商",
                    "source_term": "平台招商",
                    "jd_evidence": ["负责制定汽配平台招商策略"],
                },
                {
                    "id": "role_acquisition_leader",
                    "category": "role",
                    "token": "招商负责人",
                    "source_term": "平台招商总监",
                    "jd_evidence": ["岗位名称：跨境汽配平台招商总监"],
                },
            ],
        }
        v3_routes = [
            {
                "id": "industry_platform_role",
                "priority": 1,
                "type": "industry_role",
                "factor_ids": ["industry_autoparts", "role_platform_acquisition"],
                "target_persona": "直接做过汽配平台招商的人",
                "reason": "行业与核心职能同时命中",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
            {
                "id": "industry_leader_role",
                "priority": 2,
                "type": "title",
                "factor_ids": ["industry_autoparts", "role_acquisition_leader"],
                "target_persona": "负责汽配招商结果的人",
                "reason": "覆盖管理角色",
                "jd_evidence": ["岗位名称：跨境汽配平台招商总监"],
            },
        ]

        plan = finalize_search_plan(
            v3_routes,
            JD,
            strategy=v3_strategy,
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual(plan["schema_version"], 3)
        self.assertEqual(plan["contract"], "generic_search_plan")
        self.assertEqual(plan["target_route_count"], 8)
        self.assertEqual(plan["generation_shortfall"], 6)
        self.assertEqual(plan["routes"][0]["tokens"], ["汽配", "平台招商"])
        self.assertEqual(plan["routes"][0]["query"], "汽配 平台招商")
        self.assertEqual(plan["routes"][0]["signature"], ["anchor", "role"])
        self.assertNotIn("query", v3_routes[0])

    def test_v3_requires_two_to_three_factors_with_anchor_and_core_role(self) -> None:
        v3_strategy = {
            **strategy(),
            "factors": [
                {
                    "id": "anchor",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "role",
                    "category": "role",
                    "token": "平台招商",
                    "source_term": "平台招商",
                    "jd_evidence": ["负责制定汽配平台招商策略"],
                },
                {
                    "id": "context",
                    "category": "context",
                    "token": "跨境",
                    "source_term": "跨境",
                    "jd_evidence": ["岗位名称：跨境汽配平台招商总监"],
                },
                {
                    "id": "ecosystem",
                    "category": "ecosystem",
                    "token": "商家池",
                    "source_term": "核心商家池",
                    "jd_evidence": ["负责制定汽配平台招商策略并建设核心商家池"],
                },
            ],
        }
        base_route = {
            "id": "valid",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", "role"],
            "target_persona": "汽配平台招商人才",
            "reason": "同时保留行业和核心职能",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }

        for factor_ids, message in (
            (["anchor"], "2-3"),
            (["anchor", "role", "context", "ecosystem"], "2-3"),
            (["role", "context"], "强锚点"),
            (["anchor", "context"], "核心职能"),
        ):
            with self.subTest(factor_ids=factor_ids):
                with self.assertRaisesRegex(ValueError, message):
                    finalize_search_plan(
                        [{**base_route, "factor_ids": factor_ids}],
                        JD,
                        strategy=v3_strategy,
                        config=SearchPlanConfig(total_query_budget=8),
                    )

    def test_v3_rejects_model_query_and_generic_action_disguised_as_role(self) -> None:
        v3_strategy = {
            **strategy(),
            "factors": [
                {
                    "id": "anchor",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "generic_role",
                    "category": "role",
                    "token": "商家入驻",
                    "source_term": "商家",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
            ],
        }
        route = {
            "id": "broad",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", "generic_role"],
            "target_persona": "汽配商家入驻人才",
            "reason": "过宽泛",
            "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
        }

        with self.assertRaisesRegex(ValueError, "泛化动作"):
            finalize_search_plan(
                [route],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )
        with self.assertRaisesRegex(ValueError, "query 必须由程序"):
            finalize_search_plan(
                [{**route, "query": "汽配 商家入驻"}],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )

        v3_strategy["factors"][1]["token"] = "商家入驻负责人"
        with self.assertRaisesRegex(ValueError, "泛化动作"):
            finalize_search_plan(
                [route],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_v3_factor_tokens_reject_whitespace_boolean_and_list_syntax(self) -> None:
        base_factor = {
            "id": "anchor",
            "category": "anchor",
            "token": "汽配",
            "source_term": "汽配行业经验",
            "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
        }
        role_factor = {
            "id": "role",
            "category": "role",
            "token": "平台招商",
            "source_term": "平台招商",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }
        route = {
            "id": "route",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", "role"],
            "target_persona": "汽配平台招商人才",
            "reason": "精确组合",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }

        for token in ("汽配 行业", "汽配 OR 招商", "汽配、招商"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "搜索因子 anchor token"):
                    finalize_search_plan(
                        [route],
                        JD,
                        strategy={**strategy(), "factors": [{**base_factor, "token": token}, role_factor]},
                        config=SearchPlanConfig(total_query_budget=8),
                    )

        with self.assertRaisesRegex(ValueError, "token 必须是字符串"):
            finalize_search_plan(
                [route],
                JD,
                strategy={**strategy(), "factors": [{**base_factor, "token": 123}, role_factor]},
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_v3_rejects_non_string_factor_references_and_duplicate_tokens_cleanly(self) -> None:
        v3_strategy = {
            **strategy(),
            "factors": [
                {
                    "id": "anchor",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "role",
                    "category": "role",
                    "token": "平台招商",
                    "source_term": "平台招商",
                    "jd_evidence": ["负责制定汽配平台招商策略"],
                },
            ],
        }
        route = {
            "id": "route",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", {"bad": "id"}],
            "target_persona": "汽配平台招商人才",
            "reason": "非法引用应关闭失败",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }
        with self.assertRaisesRegex(ValueError, "2-3 个有效搜索因子"):
            finalize_search_plan(
                [route],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )

        v3_strategy["factors"][1]["token"] = "汽配"
        route["factor_ids"] = ["anchor", "role"]
        with self.assertRaisesRegex(ValueError, "重复 token"):
            finalize_search_plan(
                [route],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_v3_deduplicates_token_order_compact_queries_and_seniority_synonyms(self) -> None:
        factors = [
            {
                "id": "anchor",
                "category": "anchor",
                "token": "汽配",
                "source_term": "汽配行业经验",
                "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            },
            {
                "id": "anchor_platform",
                "category": "anchor",
                "token": "汽配平台",
                "source_term": "汽配平台招商",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
            {
                "id": "platform_role",
                "category": "role",
                "token": "平台招商",
                "source_term": "平台招商",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
            {
                "id": "acquisition_role",
                "category": "role",
                "token": "招商",
                "source_term": "招商",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
            {
                "id": "director_role",
                "category": "role",
                "token": "招商总监",
                "source_term": "平台招商总监",
                "jd_evidence": ["岗位名称：跨境汽配平台招商总监"],
            },
            {
                "id": "leader_role",
                "category": "role",
                "token": "招商负责人",
                "source_term": "招商",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
        ]

        def route(route_id: str, priority: int, factor_ids: list[str]) -> dict[str, object]:
            return {
                "id": route_id,
                "priority": priority,
                "type": "industry_role",
                "factor_ids": factor_ids,
                "target_persona": "汽配平台招商人才",
                "reason": "验证语义判重",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            }

        plan = finalize_search_plan(
            [
                route("spaced", 1, ["anchor", "platform_role"]),
                route("reordered", 2, ["platform_role", "anchor"]),
                route("compact_equivalent", 3, ["anchor_platform", "acquisition_role"]),
                route("director", 4, ["anchor", "director_role"]),
                route("leader", 5, ["anchor", "leader_role"]),
            ],
            JD,
            strategy={**strategy(), "factors": factors},
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual([route["id"] for route in plan["routes"]], ["spaced", "director"])
        self.assertEqual([route["priority"] for route in plan["routes"]], [1, 2])
        self.assertEqual(plan["generation_shortfall"], 6)

    def test_v3_family_limit_preserves_diversity_and_reports_shortfall(self) -> None:
        factors = [
            {
                "id": "anchor",
                "category": "anchor",
                "token": "汽配",
                "source_term": "汽配行业经验",
                "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            }
        ]
        routes_with_one_family = []
        role_specs = (
            ("平台招商", "平台招商", "负责制定汽配平台招商策略并建设核心商家池"),
            ("招商策略", "招商策略", "负责制定汽配平台招商策略并建设核心商家池"),
            ("招商团队", "平台招商总监", "岗位名称：跨境汽配平台招商总监"),
            ("招商结果", "招商", "负责制定汽配平台招商策略并建设核心商家池"),
            ("商家拓展", "汽配商家资源", "必须有汽配行业经验和汽配商家资源"),
        )
        for index, (token, source_term, evidence) in enumerate(role_specs, start=1):
            factor_id = f"role_{index}"
            factors.append(
                {
                    "id": factor_id,
                    "category": "role",
                    "token": token,
                    "source_term": source_term,
                    "jd_evidence": [evidence],
                }
            )
            routes_with_one_family.append(
                {
                    "id": factor_id,
                    "priority": index,
                    "type": "industry_role",
                    "factor_ids": ["anchor", factor_id],
                    "target_persona": "汽配招商人才",
                    "reason": "同一直接职能 family",
                    "jd_evidence": ["负责制定汽配平台招商策略并建设核心商家池"],
                }
            )

        plan = finalize_search_plan(
            routes_with_one_family,
            JD,
            strategy={**strategy(), "factors": factors},
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual(len(plan["routes"]), 3)
        self.assertEqual({route["family"] for route in plan["routes"]}, {"direct_role"})
        self.assertEqual(plan["generation_shortfall"], 5)

    def test_v3_allows_only_one_adjacent_or_exploration_route(self) -> None:
        factors = [
            {
                "id": "anchor",
                "category": "anchor",
                "token": "汽配",
                "source_term": "汽配行业经验",
                "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            },
            {
                "id": "role",
                "category": "role",
                "token": "平台招商",
                "source_term": "平台招商",
                "jd_evidence": ["负责制定汽配平台招商策略"],
            },
            {
                "id": "role_alt",
                "category": "role",
                "token": "招商策略",
                "source_term": "招商策略",
                "jd_evidence": ["负责制定汽配平台招商策略并建设核心商家池"],
            },
        ]
        base = {
            "target_persona": "相邻汽配招商人才",
            "reason": "有限探索",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }
        plan = finalize_search_plan(
            [
                {**base, "id": "adjacent", "priority": 1, "type": "adjacent", "factor_ids": ["anchor", "role"]},
                {**base, "id": "exploration", "priority": 2, "type": "exploration", "factor_ids": ["anchor", "role_alt"]},
            ],
            JD,
            strategy={**strategy(), "factors": factors},
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual([route["id"] for route in plan["routes"]], ["adjacent"])
        self.assertEqual(plan["generation_shortfall"], 7)

    def test_v3_target_route_count_is_capped_at_twelve(self) -> None:
        self.assertEqual(SearchPlanConfig(total_query_budget=8).v3_target_route_count, 8)
        self.assertEqual(SearchPlanConfig(total_query_budget=10).v3_target_route_count, 10)
        self.assertEqual(SearchPlanConfig(total_query_budget=20).v3_target_route_count, 12)

    def test_query_history_accepts_only_local_aggregate_statistics(self) -> None:
        tokens = ["汽配", "平台招商"]
        key = query_history_key(tokens)
        history = {
            key: {
                "query": "汽配 平台招商",
                "tokens": tokens,
                "last_used_at": "2026-09-03T16:30:00+08:00",
                "execution_count": 2,
                "returned_count": 30,
                "new_to_inventory_count": 15,
                "marginal_new_count": 7,
                "duplicate_rate": 0.5,
                "evaluated_count": 15,
                "median_score": 43.75,
            }
        }

        validated = validate_query_history(history)

        self.assertEqual(validated[key]["query"], "汽配 平台招商")
        self.assertEqual(validated[key]["marginal_new_count"], 7)
        self.assertEqual(set(validated[key]), {
            "query",
            "tokens",
            "last_used_at",
            "execution_count",
            "returned_count",
            "new_to_inventory_count",
            "marginal_new_count",
            "duplicate_rate",
            "evaluated_count",
            "median_score",
        })

    def test_query_history_rejects_raw_data_capabilities_and_inconsistent_statistics(self) -> None:
        tokens = ["汽配", "平台招商"]
        key = query_history_key(tokens)
        base = {
            "query": "汽配 平台招商",
            "tokens": tokens,
            "last_used_at": "2026-09-03T16:30:00+08:00",
            "execution_count": 1,
            "returned_count": 15,
            "new_to_inventory_count": 5,
            "marginal_new_count": 4,
            "duplicate_rate": 0.5,
            "evaluated_count": 5,
            "median_score": 40,
        }
        invalid_rows = (
            ({**base, "resume": {"name": "不应进入历史"}}, "未知字段"),
            ({**base, "client": "boss"}, "未知字段"),
            ({**base, "execution_count": True}, "非负整数"),
            ({**base, "returned_count": 3}, "新增数量"),
            ({**base, "duplicate_rate": 1.1}, "超出范围"),
            ({**base, "last_used_at": "not-a-date"}, "ISO-8601"),
            ({**base, "evaluated_count": 0, "median_score": 40}, "median_score"),
        )
        for row, message in invalid_rows:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_query_history({key: row})

    def test_query_history_key_and_query_must_match_token_signature(self) -> None:
        tokens = ["汽配", "平台招商"]
        key = query_history_key(tokens)
        base = {
            "query": "汽配 平台招商",
            "tokens": tokens,
            "last_used_at": None,
            "execution_count": 0,
        }

        with self.assertRaisesRegex(ValueError, "key 与 tokens"):
            validate_query_history({"wrong|key": base})
        with self.assertRaisesRegex(ValueError, "query 与 tokens"):
            validate_query_history({key: {**base, "query": "汽配 招商负责人"}})

        long_tokens = ["跨境汽配平台", "供应链生态", "平台招商负责人"]
        long_key = query_history_key(long_tokens)
        validated = validate_query_history(
            {
                long_key: {
                    "query": " ".join(long_tokens),
                    "tokens": long_tokens,
                    "last_used_at": None,
                    "execution_count": 0,
                }
            }
        )
        self.assertEqual(validated[long_key]["query"], " ".join(long_tokens))

    def test_v3_accepts_valid_history_without_weakening_precision_gate(self) -> None:
        v3_strategy = {
            **strategy(),
            "factors": [
                {
                    "id": "anchor",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "role",
                    "category": "role",
                    "token": "商家入驻",
                    "source_term": "商家",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
            ],
        }
        tokens = ["汽配", "商家入驻"]
        history = {
            query_history_key(tokens): {
                "query": "汽配 商家入驻",
                "tokens": tokens,
                "last_used_at": "2026-09-03T16:30:00+08:00",
                "execution_count": 1,
                "returned_count": 100,
                "new_to_inventory_count": 100,
                "marginal_new_count": 100,
                "duplicate_rate": 0,
                "evaluated_count": 1,
                "median_score": 100,
            }
        }
        route = {
            "id": "broad",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", "role"],
            "target_persona": "汽配商家入驻人才",
            "reason": "历史很好也不能放宽硬门",
            "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
        }

        with self.assertRaisesRegex(ValueError, "泛化动作"):
            finalize_search_plan(
                [route],
                JD,
                strategy=v3_strategy,
                config=SearchPlanConfig(total_query_budget=8),
                route_history=history,
            )

    def test_v3_history_moves_unseen_precise_routes_ahead_before_family_selection(self) -> None:
        factors = [
            {
                "id": "anchor",
                "category": "anchor",
                "token": "汽配",
                "source_term": "汽配行业经验",
                "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            }
        ]
        role_specs = (
            ("platform", "平台招商", "平台招商"),
            ("strategy", "招商策略", "招商策略"),
            ("director", "招商总监", "平台招商总监"),
            ("merchant", "商家拓展", "汽配商家资源"),
        )
        candidate_routes = []
        for priority, (factor_id, token, source_term) in enumerate(role_specs, start=1):
            factors.append(
                {
                    "id": factor_id,
                    "category": "role",
                    "token": token,
                    "source_term": source_term,
                    "jd_evidence": [
                        "岗位名称：跨境汽配平台招商总监"
                        if factor_id == "director"
                        else "必须有汽配行业经验和汽配商家资源"
                        if factor_id == "merchant"
                        else "负责制定汽配平台招商策略并建设核心商家池"
                    ],
                }
            )
            candidate_routes.append(
                {
                    "id": factor_id,
                    "priority": priority,
                    "type": "industry_role",
                    "factor_ids": ["anchor", factor_id],
                    "target_persona": "汽配招商人才",
                    "reason": "比较未执行和已执行组合",
                    "jd_evidence": ["负责制定汽配平台招商策略并建设核心商家池"],
                }
            )

        history = {}
        for factor_id in ("platform", "strategy", "director"):
            token = next(factor["token"] for factor in factors if factor["id"] == factor_id)
            tokens = ["汽配", token]
            history[query_history_key(tokens)] = {
                "query": " ".join(tokens),
                "tokens": tokens,
                "last_used_at": "2026-09-03T16:30:00+08:00",
                "execution_count": 1,
                "returned_count": 15,
                "new_to_inventory_count": 5,
                "marginal_new_count": 2,
                "duplicate_rate": 0.5,
                "evaluated_count": 5,
                "median_score": 40,
            }

        plan = finalize_search_plan(
            candidate_routes,
            JD,
            strategy={**strategy(), "factors": factors},
            config=SearchPlanConfig(total_query_budget=8),
            route_history=history,
        )

        self.assertEqual([route["id"] for route in plan["routes"]], ["merchant", "platform", "strategy"])
        self.assertEqual(plan["routes"][0]["history"]["status"], "unseen")
        self.assertEqual(plan["routes"][0]["ordering_reason"], "unseen_precise_combination")
        self.assertTrue(all(route["history"]["status"] == "executed" for route in plan["routes"][1:]))

    def test_v3_executed_history_uses_marginal_supply_quality_and_stable_ties(self) -> None:
        factors = [
            {
                "id": "anchor",
                "category": "anchor",
                "token": "汽配",
                "source_term": "汽配行业经验",
                "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
            }
        ]
        specs = (
            ("low", "平台招商", "平台招商", 1, 35.0),
            ("high", "招商策略", "招商策略", 5, 60.0),
            ("medium", "商家拓展", "汽配商家资源", 3, 50.0),
        )
        candidate_routes = []
        history = {}
        for priority, (factor_id, token, source_term, marginal_new, median_score) in enumerate(specs, start=1):
            evidence = (
                "必须有汽配行业经验和汽配商家资源"
                if factor_id == "medium"
                else "负责制定汽配平台招商策略并建设核心商家池"
            )
            factors.append(
                {
                    "id": factor_id,
                    "category": "role",
                    "token": token,
                    "source_term": source_term,
                    "jd_evidence": [evidence],
                }
            )
            candidate_routes.append(
                {
                    "id": factor_id,
                    "priority": priority,
                    "type": "industry_role",
                    "factor_ids": ["anchor", factor_id],
                    "target_persona": "汽配招商人才",
                    "reason": "比较历史边际供给",
                    "jd_evidence": [evidence],
                }
            )
            tokens = ["汽配", token]
            history[query_history_key(tokens)] = {
                "query": " ".join(tokens),
                "tokens": tokens,
                "last_used_at": "2026-09-03T16:30:00+08:00",
                "execution_count": 1,
                "returned_count": 15,
                "new_to_inventory_count": marginal_new,
                "marginal_new_count": marginal_new,
                "duplicate_rate": 0.2,
                "evaluated_count": marginal_new,
                "median_score": median_score,
            }

        plan = finalize_search_plan(
            candidate_routes,
            JD,
            strategy={**strategy(), "factors": factors},
            config=SearchPlanConfig(total_query_budget=8),
            route_history=history,
        )

        self.assertEqual([route["id"] for route in plan["routes"]], ["high", "medium", "low"])
        self.assertEqual([route["priority"] for route in plan["routes"]], [1, 2, 3])

    def test_v3_without_history_is_reproducible(self) -> None:
        v3_strategy = {
            **strategy(),
            "factors": [
                {
                    "id": "anchor",
                    "category": "anchor",
                    "token": "汽配",
                    "source_term": "汽配行业经验",
                    "jd_evidence": ["必须有汽配行业经验和汽配商家资源"],
                },
                {
                    "id": "role",
                    "category": "role",
                    "token": "平台招商",
                    "source_term": "平台招商",
                    "jd_evidence": ["负责制定汽配平台招商策略"],
                },
            ],
        }
        route = {
            "id": "route",
            "priority": 1,
            "type": "industry_role",
            "factor_ids": ["anchor", "role"],
            "target_persona": "汽配平台招商人才",
            "reason": "无历史稳定生成",
            "jd_evidence": ["负责制定汽配平台招商策略"],
        }

        first = finalize_search_plan(
            [route],
            JD,
            strategy=v3_strategy,
            config=SearchPlanConfig(total_query_budget=8),
        )
        second = finalize_search_plan(
            [route],
            JD,
            strategy=v3_strategy,
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual(first, second)
        self.assertEqual(first["routes"][0]["history"]["status"], "unseen")

    def test_v2_plan_is_short_priority_ordered_and_has_no_query_budget(self) -> None:
        rows = routes()
        rows.reverse()

        plan = finalize_search_plan(
            rows,
            JD,
            strategy=strategy(),
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["contract"], "generic_search_plan")
        self.assertEqual(plan["strategy"]["dominant_anchor"], "汽配")
        self.assertEqual([row["priority"] for row in plan["routes"]], [1, 2, 3])
        self.assertEqual(
            [row["id"] for row in plan["routes"]],
            ["industry_role", "cross_border_role", "leader_title"],
        )
        self.assertTrue(all("query_budget" not in row for row in plan["routes"]))
        self.assertTrue(all("effective_weight" not in row for row in plan["routes"]))
        self.assertNotIn("config", plan)

    def test_exploration_is_optional_and_at_most_one(self) -> None:
        plan = finalize_search_plan(
            routes(),
            JD,
            strategy=strategy(),
            config=SearchPlanConfig(total_query_budget=8),
        )
        self.assertFalse(any(row["type"] == "exploration" for row in plan["routes"]))

        duplicated_exploration = routes() + [
            {
                "id": "explore_1",
                "priority": 4,
                "type": "exploration",
                "query": "汽配产业带招商",
                "target_persona": "汽配产业带招商人才",
                "reason": "探索产业带供给",
                "jd_evidence": ["汽配商家资源"],
            },
            {
                "id": "explore_2",
                "priority": 5,
                "type": "adjacent",
                "query": "汽配供应链招商",
                "target_persona": "汽配供应链招商人才",
                "reason": "探索供应链侧人才",
                "jd_evidence": ["汽配行业经验"],
            },
        ]
        with self.assertRaisesRegex(ValueError, "相邻或探索路线最多一条"):
            finalize_search_plan(
                duplicated_exploration,
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_rejects_boolean_lists_long_queries_and_generic_only_queries(self) -> None:
        invalid_queries = (
            "汽配平台招商 OR 招商总监",
            "汽配OR平台招商",
            "汽配平台招商、汽配供应链招商",
            "汽配平台招商商务谈判团队管理商家准入",
            "商家准入",
            "商家准入负责人",
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                bad = routes()
                bad[0]["query"] = query
                with self.assertRaisesRegex(ValueError, "搜索 query"):
                    finalize_search_plan(
                        bad,
                        JD,
                        strategy=strategy(),
                        config=SearchPlanConfig(total_query_budget=8),
                    )

    def test_industry_dominant_plan_requires_anchor_in_every_query(self) -> None:
        bad = routes()
        bad[1]["query"] = "平台招商负责人"
        with self.assertRaisesRegex(ValueError, "主导行业锚点"):
            finalize_search_plan(
                bad,
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_plan_requires_three_to_six_grounded_distinct_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "3-6"):
            finalize_search_plan(
                routes()[:2],
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

        too_many = routes()
        for priority in range(4, 8):
            too_many.append(
                {
                    "id": f"extra_{priority}",
                    "priority": priority,
                    "type": "industry_role",
                    "query": f"汽配招商经理{priority}",
                    "target_persona": "汽配招商人才",
                    "reason": "额外路线",
                    "jd_evidence": ["汽配行业经验"],
                }
            )
        with self.assertRaisesRegex(ValueError, "3-6"):
            finalize_search_plan(
                too_many,
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

        bad_evidence = routes()
        bad_evidence[0]["jd_evidence"] = ["不存在的 JD 原文"]
        with self.assertRaisesRegex(ValueError, "可定位 JD 原文"):
            finalize_search_plan(
                bad_evidence,
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_priorities_must_be_unique_and_contiguous(self) -> None:
        bad = routes()
        bad[2]["priority"] = 2
        with self.assertRaisesRegex(ValueError, "priority"):
            finalize_search_plan(
                bad,
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_strategy_must_be_grounded(self) -> None:
        bad_strategy = strategy()
        bad_strategy["jd_evidence"] = ["模型虚构的行业要求"]
        with self.assertRaisesRegex(ValueError, "strategy.*JD 原文"):
            finalize_search_plan(
                routes(),
                JD,
                strategy=bad_strategy,
                config=SearchPlanConfig(total_query_budget=8),
            )

    def test_legacy_capacity_config_cannot_allow_fewer_than_three_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少容纳 3 条"):
            finalize_search_plan(
                routes(),
                JD,
                strategy=strategy(),
                config=SearchPlanConfig(total_query_budget=2),
            )


if __name__ == "__main__":
    unittest.main()
