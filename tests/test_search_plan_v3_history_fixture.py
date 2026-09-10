from __future__ import annotations

import json
import unittest
from pathlib import Path

from boss_hire.search_plan import SearchPlanConfig, finalize_search_plan, query_history_key
from boss_hire.single_job_run_plan import SingleJobRunConfig, build_single_job_run_plan


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "search_plan_v3_september_3_history.json"


class SearchPlanV3HistoryFixtureTests(unittest.TestCase):
    def test_september_3_executed_queries_are_demoted_behind_unseen_precise_routes(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        history = {
            query_history_key(row["tokens"]): row
            for row in fixture["history_rows"]
        }

        plan = finalize_search_plan(
            fixture["routes"],
            fixture["jd_text"],
            strategy=fixture["strategy"],
            config=SearchPlanConfig(total_query_budget=8),
            route_history=history,
        )

        route_ids = [route["id"] for route in plan["routes"]]
        self.assertEqual(route_ids[:4], [
            "new_belt",
            "new_brand",
            "new_cross_growth",
            "new_cross_platform",
        ])
        for repeated_route in ("old_platform", "old_leader", "old_cross"):
            if repeated_route in route_ids:
                self.assertGreater(route_ids.index(repeated_route), 3)
        self.assertTrue(all(route["history"]["status"] == "unseen" for route in plan["routes"][:4]))

        run_plan = build_single_job_run_plan(
            board_date="2026-09-04",
            config=SingleJobRunConfig.from_mapping(
                {
                    "recommendation_source_enabled": 0,
                    "top_priority_search_query_count": 3,
                }
            ),
            auth_dir=Path("data/local/private-auth"),
            search_job_id="job-fixture",
            persisted_search_plan=plan,
        )
        self.assertEqual(
            [route["query"] for route in run_plan["selected_search_routes"]],
            ["汽配 产业带 招商", "汽配 品牌商 招商", "汽配 跨境 商家增长"],
        )
        self.assertEqual(run_plan["search_query_shortfall"], 0)

    def test_fixture_contains_aggregates_only(self) -> None:
        fixture_text = FIXTURE.read_text(encoding="utf-8")

        for forbidden in ("candidate_id", "resume", "cookie", "securityId", "encryptGeekId"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, fixture_text)


if __name__ == "__main__":
    unittest.main()
