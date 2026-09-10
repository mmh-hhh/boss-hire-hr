from __future__ import annotations

import unittest

from boss_hire.ranking_contract import (
    adapt_legacy_evaluation,
    finalize_continuous_evaluation,
    finalize_continuous_rubric,
    ranking_key,
)


JD = """岗位名称：平台招商负责人
任职要求
1. 必须有从0到1搭建招商团队的经验。
2. 负责重点商家拓展、商务谈判和招商结果管理。
"""


def rubric_core() -> dict[str, object]:
    anchors = {
        "none": "没有相关证据或证据明确相反",
        "weak": "只有间接、相邻或执行层证据",
        "partial": "有直接证据，但范围或结果不完整",
        "strong": "有直接、完整且可核验的负责证据",
        "exceptional": "有直接负责证据，并有显著规模或结果",
    }
    return {
        "job_title": "平台招商负责人",
        "dimensions": [
            {
                "id": "team_building",
                "name": "团队搭建",
                "weight": 40,
                "critical": True,
                "requirement": "评价从0到1搭建招商团队的证据",
                "jd_evidence": ["必须有从0到1搭建招商团队的经验"],
                "anchors": anchors,
            },
            {
                "id": "merchant_growth",
                "name": "商家拓展",
                "weight": 35,
                "critical": True,
                "requirement": "评价重点商家拓展和招商结果",
                "jd_evidence": ["负责重点商家拓展、商务谈判和招商结果管理"],
                "anchors": anchors,
            },
            {
                "id": "negotiation",
                "name": "商务谈判",
                "weight": 25,
                "critical": False,
                "requirement": "评价商务谈判和复杂协作",
                "jd_evidence": ["负责重点商家拓展、商务谈判和招商结果管理"],
                "anchors": anchors,
            },
        ],
    }


class RankingContractTests(unittest.TestCase):
    def test_rubric_is_continuous_and_has_no_gate_or_decision_contract(self) -> None:
        rubric = finalize_continuous_rubric(rubric_core(), JD)

        self.assertEqual(rubric["schema_version"], 2)
        self.assertEqual(rubric["contract"], "continuous_ranking")
        self.assertEqual(sum(row["weight"] for row in rubric["dimensions"]), 100)
        self.assertNotIn("hard_gates", rubric)
        self.assertNotIn("decision_rules", rubric)
        self.assertTrue(rubric["dimensions"][0]["critical"])

    def test_rubric_evidence_must_be_exactly_grounded_in_jd(self) -> None:
        core = rubric_core()
        core["dimensions"][0]["jd_evidence"] = ["需要具备团队搭建能力"]

        with self.assertRaisesRegex(ValueError, "可定位 JD 原文"):
            finalize_continuous_rubric(core, JD)

    def test_program_computes_score_and_coverage_from_levels(self) -> None:
        rubric = finalize_continuous_rubric(rubric_core(), JD)
        candidate = {
            "candidate_id": "candidate-1",
            "work_experience": [
                {
                    "responsibility": "从0到1组建招商团队，负责重点商家谈判",
                    "performance": "一年引入600家商家",
                }
            ],
        }
        llm_result = {
            "candidate_id": "candidate-id-that-model-mutated",
            "dimension_assessments": [
                {
                    "id": "team_building",
                    "level": "strong",
                    "evidence": ["从0到1组建招商团队"],
                    "reason": "有直接负责证据",
                    "gaps": [],
                },
                {
                    "id": "merchant_growth",
                    "level": "exceptional",
                    "evidence": ["一年引入600家商家"],
                    "reason": "有量化结果",
                    "gaps": [],
                },
                {
                    "id": "negotiation",
                    "level": "none",
                    "evidence": [],
                    "reason": "谈判复杂度不明确",
                    "gaps": ["缺少复杂谈判案例"],
                },
            ],
            "evidence": ["从0到1组建招商团队", "一年引入600家商家"],
            "gaps": ["缺少复杂谈判案例"],
            "risks": [],
            "follow_up_questions": ["最大单体商家的谈判周期多长？"],
            "summary": "团队搭建和商家增长证据较强，谈判复杂度待核实。",
        }

        result = finalize_continuous_evaluation(llm_result, candidate, rubric)

        self.assertEqual(result["total_score"], 65.0)
        self.assertEqual(result["evidence_coverage"], 75.0)
        self.assertEqual(result["candidate_id"], "candidate-1")
        self.assertNotIn("decision", result)
        self.assertNotIn("hard_gate", result)
        self.assertEqual(result["dimension_scores"][0]["score"], 30.0)
        self.assertEqual(result["dimension_scores"][1]["score"], 35.0)

        result_without_model_id = dict(llm_result)
        result_without_model_id.pop("candidate_id")
        self.assertEqual(
            finalize_continuous_evaluation(result_without_model_id, candidate, rubric)["candidate_id"],
            "candidate-1",
        )

    def test_llm_cannot_supply_gate_decision_or_total_score(self) -> None:
        rubric = finalize_continuous_rubric(rubric_core(), JD)
        candidate = {"candidate_id": "candidate-1"}
        result = {
            "candidate_id": "candidate-1",
            "total_score": 100,
            "decision": "优先跟进",
            "dimension_assessments": [],
        }

        with self.assertRaisesRegex(ValueError, "程序计算字段"):
            finalize_continuous_evaluation(result, candidate, rubric)

    def test_ungrounded_positive_dimension_is_downgraded_without_discarding_candidate(self) -> None:
        rubric = finalize_continuous_rubric(rubric_core(), JD)
        candidate = {"candidate_id": "candidate-1", "work_experience": []}
        result = {
            "candidate_id": "candidate-1",
            "dimension_assessments": [
                {
                    "id": row["id"],
                    "level": "strong" if index == 0 else "none",
                    "evidence": ["从0到1组建招商团队"] if index == 0 else [],
                    "reason": "",
                    "gaps": [],
                }
                for index, row in enumerate(rubric["dimensions"])
            ],
            "evidence": [],
            "gaps": [],
            "risks": [],
            "follow_up_questions": [],
            "summary": "",
        }

        evaluation = finalize_continuous_evaluation(result, candidate, rubric)

        first = evaluation["dimension_scores"][0]
        self.assertEqual(first["level"], "none")
        self.assertEqual(first["score"], 0)
        self.assertEqual(first["evidence"], [])
        self.assertIn("正向证据无法逐字定位", first["gaps"])
        self.assertEqual(evaluation["total_score"], 0)

    def test_legacy_evaluation_is_read_only_adapted_for_ranking(self) -> None:
        legacy = {
            "candidate_id": "candidate-1",
            "hard_gate": "uncertain",
            "decision": "备选",
            "total_score": 72,
            "dimension_scores": [
                {"id": "team_building", "score": 30, "max_score": 40, "evidence": ["团队"]},
                {"id": "merchant_growth", "score": 25, "max_score": 35, "evidence": ["商家"]},
                {"id": "negotiation", "score": 17, "max_score": 25, "evidence": []},
            ],
            "evidence": ["团队", "商家"],
            "missing_requirements": ["谈判复杂度"],
            "risks": [],
            "follow_up_questions": [],
            "summary": "旧评价",
        }

        adapted = adapt_legacy_evaluation(legacy)

        self.assertEqual(adapted["source_contract"], "legacy_gate_v1")
        self.assertEqual(adapted["total_score"], 72.0)
        self.assertEqual(adapted["gaps"], ["谈判复杂度"])
        self.assertEqual(ranking_key(adapted, "candidate-1"), (-72.0, -2 / 3, "candidate-1"))


if __name__ == "__main__":
    unittest.main()
