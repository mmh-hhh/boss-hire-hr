from __future__ import annotations

import unittest

from boss_hire.llm_confirmation import (
    build_llm_confirmation_preview,
    build_llm_confirmation_receipt,
    verify_llm_confirmation_preview,
)
from boss_hire.ranking_contract import finalize_continuous_rubric
from boss_hire.supply_inventory import CandidateInventory


JD = "岗位名称：平台招商负责人\n负责重点商家拓展。"
ANCHORS = {
    "none": "没有相关证据",
    "weak": "只有相邻证据",
    "partial": "有部分直接证据",
    "strong": "有完整直接证据",
    "exceptional": "有规模化直接证据",
}
RUBRIC = finalize_continuous_rubric(
    {
        "job_title": "平台招商负责人",
        "dimensions": [
            {
                "id": "merchant",
                "name": "商家拓展",
                "weight": 40,
                "critical": True,
                "requirement": "评价商家拓展",
                "jd_evidence": ["负责重点商家拓展"],
                "anchors": ANCHORS,
            },
            {
                "id": "execution",
                "name": "执行",
                "weight": 30,
                "critical": False,
                "requirement": "评价执行",
                "jd_evidence": ["负责重点商家拓展"],
                "anchors": ANCHORS,
            },
            {
                "id": "growth",
                "name": "增长",
                "weight": 30,
                "critical": False,
                "requirement": "评价增长",
                "jd_evidence": ["负责重点商家拓展"],
                "anchors": ANCHORS,
            },
        ],
    },
    JD,
)


def inventory() -> CandidateInventory:
    result = CandidateInventory()
    for index in range(1, 4):
        candidate_id = f"candidate-{index}"
        result.record_source_card(
            candidate_id,
            "search",
            {"encryptGeekId": f"boss-{index}", "encryptJobId": "job-open"},
        )
        result.ensure_resume(candidate_id, lambda: {"basic": {"name": "真实姓名"}})
    result.record_evaluation(
        "job-open",
        "candidate-1",
        {
            "schema_version": 1,
            "contract": "continuous_ranking",
            "candidate_id": "candidate-1",
            "rubric_version": RUBRIC["version"],
            "score": 80,
        },
    )
    return result


class LlmConfirmationTests(unittest.TestCase):
    def test_preview_binds_job_rubric_model_workers_and_pending_id_digest(self) -> None:
        preview = build_llm_confirmation_preview(
            inventory=inventory(),
            job_id="job-open",
            rubric=RUBRIC,
            model="gpt-test",
            workers=4,
            created_at="2026-09-07T12:00:00+08:00",
        )

        self.assertEqual(preview["reuse_count"], 1)
        self.assertEqual(preview["pending_count"], 2)
        self.assertEqual(preview["job_id"], "job-open")
        self.assertEqual(preview["rubric_version"], RUBRIC["version"])
        self.assertEqual(preview["llm_model"], "gpt-test")
        self.assertEqual(preview["workers"], 4)
        self.assertEqual(len(preview["pending_candidate_ids_digest"]), 64)
        self.assertNotIn("candidate_ids", preview)

    def test_receipt_contains_no_key_resume_or_direct_identity(self) -> None:
        preview = build_llm_confirmation_preview(
            inventory=inventory(),
            job_id="job-open",
            rubric=RUBRIC,
            model="gpt-test",
            created_at="2026-09-07T12:00:00+08:00",
        )
        receipt = build_llm_confirmation_receipt(
            preview,
            confirmed_at="2026-09-07T12:01:00+08:00",
        )
        rendered = str(receipt)

        self.assertEqual(receipt["status"], "confirmed")
        self.assertNotIn("API", rendered)
        self.assertNotIn("真实姓名", rendered)
        self.assertNotIn("candidate-1", rendered)

    def test_verification_rejects_pending_or_model_drift(self) -> None:
        current = inventory()
        preview = build_llm_confirmation_preview(
            inventory=current,
            job_id="job-open",
            rubric=RUBRIC,
            model="gpt-test",
            workers=4,
            created_at="2026-09-07T12:00:00+08:00",
        )
        verify_llm_confirmation_preview(
            preview,
            inventory=current,
            job_id="job-open",
            rubric=RUBRIC,
            model="gpt-test",
            workers=4,
            checked_at="2026-09-07T12:02:00+08:00",
        )

        current.record_evaluation(
            "job-open",
            "candidate-2",
            {
                "schema_version": 1,
                "contract": "continuous_ranking",
                "candidate_id": "candidate-2",
                "rubric_version": RUBRIC["version"],
                "score": 70,
            },
        )
        with self.assertRaisesRegex(ValueError, "确认范围已变化"):
            verify_llm_confirmation_preview(
                preview,
                inventory=current,
                job_id="job-open",
                rubric=RUBRIC,
                model="gpt-test",
                workers=4,
                checked_at="2026-09-07T12:03:00+08:00",
            )
        with self.assertRaisesRegex(ValueError, "确认范围已变化"):
            verify_llm_confirmation_preview(
                preview,
                inventory=inventory(),
                job_id="job-open",
                rubric=RUBRIC,
                model="other-model",
                workers=4,
                checked_at="2026-09-07T12:03:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
