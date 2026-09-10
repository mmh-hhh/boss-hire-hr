from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Mapping

from boss_hire.local_scoring import score_inventory_resumes
from boss_hire.ranking_contract import finalize_continuous_rubric
from boss_hire.single_job_llm import LlmHttpError
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


class FakeEvaluationLlm:
    model = "fake-local-model"

    def __init__(
        self,
        *,
        fail_candidate_id: str | None = None,
        failure_message: str = "temporary local LLM failure",
    ) -> None:
        self.fail_candidate_id = fail_candidate_id
        self.failure_message = failure_message
        self.calls: list[str] = []

    def complete_json(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.assert_operation(operation)
        candidate = payload["candidate"]
        candidate_id = str(candidate["candidate_id"])
        self.calls.append(candidate_id)
        if candidate_id == self.fail_candidate_id:
            raise RuntimeError(self.failure_message)
        return {
            "candidate_id": candidate_id,
            "dimension_assessments": [
                {
                    "id": "merchant",
                    "level": "strong",
                    "evidence": ["负责重点商家拓展"],
                    "reason": "有直接证据",
                    "gaps": [],
                },
                {
                    "id": "execution",
                    "level": "strong",
                    "evidence": ["负责重点商家拓展"],
                    "reason": "有直接证据",
                    "gaps": [],
                },
                {
                    "id": "growth",
                    "level": "strong",
                    "evidence": ["负责重点商家拓展"],
                    "reason": "有直接证据",
                    "gaps": [],
                },
            ],
            "evidence": ["负责重点商家拓展"],
            "gaps": [],
            "risks": [],
            "follow_up_questions": [],
            "summary": "商家拓展证据明确。",
        }

    @staticmethod
    def assert_operation(operation: str) -> None:
        if operation != "candidate_evaluation":
            raise AssertionError(operation)


class ConcurrentEvaluationLlm(FakeEvaluationLlm):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def complete_json(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            time.sleep(0.03)
            return super().complete_json(
                operation=operation,
                system_prompt=system_prompt,
                payload=payload,
            )
        finally:
            with self._lock:
                self.active_calls -= 1


class DetailedFailureLlm(FakeEvaluationLlm):
    def complete_json(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.assert_operation(operation)
        candidate_id = str(payload["candidate"]["candidate_id"])
        self.calls.append(candidate_id)
        raise LlmHttpError(
            operation="candidate_evaluation",
            public_details={
                "status": 502,
                "error": {"message": "unknown provider", "type": "server_error"},
                "headers": {"x-request-id": "request-123"},
            },
        )


class MutatedCandidateIdLlm(FakeEvaluationLlm):
    def complete_json(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = super().complete_json(
            operation=operation,
            system_prompt=system_prompt,
            payload=payload,
        )
        result["candidate_id"] = "candidate-id-that-model-mutated"
        return result


class LocalScoringTests(unittest.TestCase):
    def inventory(self, *, candidate_count: int = 3, ready_count: int = 2) -> CandidateInventory:
        inventory = CandidateInventory()
        for index in range(1, candidate_count + 1):
            candidate_id = f"candidate-{index}"
            inventory.record_source_card(
                candidate_id,
                "search",
                {"encryptGeekId": f"boss-{index}", "encryptJobId": "job-open"},
            )
        for index in range(1, ready_count + 1):
            inventory.ensure_resume(
                f"candidate-{index}",
                lambda: {
                    "work_experience": [
                        {"position": "平台招商负责人", "responsibility": "负责重点商家拓展"}
                    ]
                },
            )
        return inventory

    def test_score_reads_only_local_resumes_and_leaves_missing_details_outside_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            inventory = self.inventory()
            inventory.save(inventory_path)
            llm = FakeEvaluationLlm()

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=llm,
                output_dir=root / "scores",
                workers=1,
            )
            restored = CandidateInventory.load(inventory_path)

        self.assertEqual(llm.calls, ["candidate-1", "candidate-2"])
        self.assertEqual(result["summary"]["boss_requests"], 0)
        self.assertEqual(result["summary"]["scored_count"], 2)
        self.assertEqual(result["summary"]["workers"], 1)
        self.assertEqual(result["summary"]["remaining_pending_count"], 0)
        self.assertIsNotNone(
            restored.get_evaluation("job-open", "candidate-1", rubric_version=RUBRIC["version"])
        )
        self.assertEqual(restored.candidate_status("candidate-3")["resume"], "pending")

    def test_failed_local_score_remains_pending_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory().save(inventory_path)
            first = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=FakeEvaluationLlm(fail_candidate_id="candidate-1"),
                output_dir=root / "scores",
                selection="1",
            )
            second_llm = FakeEvaluationLlm()
            second = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=second_llm,
                output_dir=root / "scores",
                selection="all",
            )

        self.assertEqual(first["summary"]["failed_count"], 1)
        self.assertEqual(first["summary"]["remaining_pending_count"], 2)
        self.assertEqual(second_llm.calls, ["candidate-1", "candidate-2"])
        self.assertEqual(second["summary"]["remaining_pending_count"], 0)

    def test_score_summary_preserves_public_http_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(candidate_count=1, ready_count=1).save(inventory_path)

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=DetailedFailureLlm(),
                output_dir=root / "scores",
                workers=1,
            )

        failure = result["summary"]["failures"][0]
        self.assertIn("LLM candidate_evaluation HTTP 502", failure["error"])
        self.assertEqual(
            failure["http_error"],
            {
                "status": 502,
                "error": {"message": "unknown provider", "type": "server_error"},
                "headers": {"x-request-id": "request-123"},
            },
        )

    def test_score_binds_model_mutated_id_to_the_local_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(candidate_count=1, ready_count=1).save(inventory_path)

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=MutatedCandidateIdLlm(),
                output_dir=root / "scores",
                workers=1,
            )
            restored = CandidateInventory.load(inventory_path)

        self.assertEqual(result["summary"]["scored_count"], 1)
        self.assertEqual(result["summary"]["failed_count"], 0)
        self.assertEqual(result["summary"]["remaining_pending_count"], 0)
        evaluation = restored.get_evaluation("job-open", "candidate-1", rubric_version=RUBRIC["version"])
        self.assertEqual(evaluation["candidate_id"], "candidate-1")

    def test_score_preserves_candidate_id_that_contains_a_phone_like_sequence(self) -> None:
        candidate_id = "candidate-9b14370279869c9a"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            inventory = CandidateInventory()
            inventory.record_source_card(candidate_id, "search", {"encryptGeekId": "boss-1"})
            inventory.ensure_resume(
                candidate_id,
                lambda: {"work_experience": [{"responsibility": "负责重点商家拓展"}]},
            )
            inventory.save(inventory_path)

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=FakeEvaluationLlm(),
                output_dir=root / "scores",
                workers=1,
            )

        self.assertEqual(result["summary"]["scored_candidate_ids"], [candidate_id])
        self.assertEqual(result["summary"]["remaining_pending_count"], 0)

    def test_score_uses_bounded_parallel_llm_calls_with_single_inventory_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(candidate_count=4, ready_count=4).save(inventory_path)
            llm = ConcurrentEvaluationLlm()

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=llm,
                output_dir=root / "scores",
                workers=2,
            )
            restored = CandidateInventory.load(inventory_path)

        self.assertEqual(result["summary"]["workers"], 2)
        self.assertEqual(result["summary"]["attempted_count"], 4)
        self.assertEqual(result["summary"]["scored_count"], 4)
        self.assertFalse(result["summary"]["backpressure_stopped"])
        self.assertGreaterEqual(llm.max_active_calls, 2)
        self.assertLessEqual(llm.max_active_calls, 2)
        self.assertCountEqual(llm.calls, ["candidate-1", "candidate-2", "candidate-3", "candidate-4"])
        self.assertEqual(
            len(restored.to_dict()["evaluations"]["job-open"]),
            4,
        )

    def test_rate_limit_stops_dispatch_and_leaves_unattempted_candidates_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(candidate_count=3, ready_count=3).save(inventory_path)
            llm = FakeEvaluationLlm(
                fail_candidate_id="candidate-1",
                failure_message="LLM candidate_evaluation HTTP 429",
            )

            result = score_inventory_resumes(
                inventory_path=inventory_path,
                job_id="job-open",
                rubric=RUBRIC,
                llm=llm,
                output_dir=root / "scores",
                workers=1,
            )

        self.assertEqual(llm.calls, ["candidate-1"])
        self.assertEqual(result["summary"]["attempted_count"], 1)
        self.assertEqual(result["summary"]["scored_count"], 0)
        self.assertEqual(result["summary"]["failed_count"], 1)
        self.assertEqual(result["summary"]["remaining_pending_count"], 3)
        self.assertTrue(result["summary"]["backpressure_stopped"])

    def test_score_rejects_unsafe_worker_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory().save(inventory_path)

            with self.assertRaisesRegex(ValueError, "评分并发数"):
                score_inventory_resumes(
                    inventory_path=inventory_path,
                    job_id="job-open",
                    rubric=RUBRIC,
                    llm=FakeEvaluationLlm(),
                    output_dir=root / "scores",
                    workers=0,
                )


if __name__ == "__main__":
    unittest.main()
