from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from boss_hire.supply_inventory import CandidateInventory, stable_local_candidate_id


def evaluation(candidate_id: str, score: float, coverage: float = 80.0) -> dict[str, object]:
    return {
        "schema_version": 2,
        "contract": "continuous_ranking",
        "candidate_id": candidate_id,
        "rubric_version": "rubric-v1",
        "total_score": score,
        "evidence_coverage": coverage,
        "dimension_scores": [],
        "evidence": ["候选人原文证据"],
        "gaps": [],
        "risks": [],
        "follow_up_questions": [],
        "summary": f"候选人 {candidate_id} 摘要",
    }


class SupplyInventoryTests(unittest.TestCase):
    def test_stable_local_candidate_id_does_not_depend_on_run_date(self) -> None:
        first = stable_local_candidate_id("boss-candidate-1")
        second = stable_local_candidate_id("boss-candidate-1")

        self.assertEqual(first, second)
        self.assertNotIn("boss-candidate-1", first)

    def test_private_inventory_persists_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate_inventory.json"
            inventory = CandidateInventory()
            inventory.ensure_resume("candidate-1", lambda: {"id": "candidate-1"})
            inventory.save(path)

            restored = CandidateInventory.load(path)

            self.assertEqual(restored.to_dict(), inventory.to_dict())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_resume_is_loaded_once_and_reused_across_jobs(self) -> None:
        inventory = CandidateInventory()
        calls: list[str] = []

        def load_resume() -> dict[str, object]:
            calls.append("loaded")
            return {"work_experience": [{"company": "某平台"}]}

        first = inventory.ensure_resume("candidate-1", load_resume)
        second = inventory.ensure_resume("candidate-1", load_resume)
        inventory.record_evaluation("job-a", "candidate-1", evaluation("candidate-1", 88))
        inventory.record_evaluation("job-b", "candidate-1", evaluation("candidate-1", 75))

        self.assertEqual(calls, ["loaded"])
        self.assertEqual(first, second)
        self.assertEqual(len(inventory.to_dict()["candidates"]), 1)
        self.assertEqual(len(inventory.to_dict()["evaluations"]), 2)

    def test_unshown_inventory_is_ranked_and_reports_shortage(self) -> None:
        inventory = CandidateInventory()
        for candidate_id, score, coverage in [
            ("candidate-b", 80, 90),
            ("candidate-a", 90, 70),
            ("candidate-c", 80, 60),
        ]:
            inventory.ensure_resume(candidate_id, lambda candidate_id=candidate_id: {"id": candidate_id})
            inventory.record_source_card(candidate_id, "search", {"encryptGeekId": candidate_id})
            inventory.record_evaluation("job-a", candidate_id, evaluation(candidate_id, score, coverage))

        selected = inventory.select_unshown("job-a", 5)

        self.assertEqual([row["candidate_id"] for row in selected["candidates"]], ["candidate-a", "candidate-b", "candidate-c"])
        self.assertEqual(selected["actual_count"], 3)
        self.assertEqual(selected["shortage_count"], 2)
        self.assertEqual(selected["shortage_reason"], "inventory_exhausted")

    def test_delivery_history_is_job_specific_and_prevents_repeat(self) -> None:
        inventory = CandidateInventory()
        inventory.ensure_resume("candidate-1", lambda: {"id": "candidate-1"})
        inventory.record_evaluation("job-a", "candidate-1", evaluation("candidate-1", 90))
        inventory.record_evaluation("job-b", "candidate-1", evaluation("candidate-1", 70))

        inventory.mark_delivered("batch-1", "job-a", ["candidate-1"])

        self.assertEqual(inventory.select_unshown("job-a", 5)["actual_count"], 0)
        self.assertEqual(inventory.select_unshown("job-b", 5)["actual_count"], 1)
        with self.assertRaisesRegex(ValueError, "重复交付"):
            inventory.mark_delivered("batch-2", "job-a", ["candidate-1"])

    def test_select_evaluated_includes_delivered_without_changing_history(self) -> None:
        inventory = CandidateInventory()
        for candidate_id, score in (("candidate-high", 95), ("candidate-low", 80)):
            inventory.ensure_resume(candidate_id, lambda: {"id": candidate_id})
            inventory.record_source_card(
                candidate_id,
                "search",
                {"encryptGeekId": candidate_id},
            )
            inventory.record_evaluation(
                "job-a",
                candidate_id,
                evaluation(candidate_id, score),
            )
        inventory.mark_delivered("batch-old", "job-a", ["candidate-high"])

        selected = inventory.select_evaluated(
            "job-a",
            5,
            rubric_version="rubric-v1",
        )

        self.assertEqual(
            [row["candidate_id"] for row in selected["candidates"]],
            ["candidate-high", "candidate-low"],
        )
        self.assertEqual(inventory.evaluated_count("job-a", rubric_version="rubric-v1"), 2)
        self.assertEqual(inventory.undelivered_count("job-a", rubric_version="rubric-v1"), 1)

    def test_legacy_displayed_history_migrates_to_delivered_without_consuming_previews(self) -> None:
        legacy = {
            "schema_version": 1,
            "candidates": {
                "candidate-1": {
                    "candidate_id": "candidate-1",
                    "resume": {"id": "candidate-1"},
                    "sources": {},
                }
            },
            "evaluations": {"job-a": {"candidate-1": evaluation("candidate-1", 90)}},
            "displayed": {"job-a": {"candidate-1": {"board_id": "legacy-board"}}},
            "job_artifacts": {},
        }

        inventory = CandidateInventory(legacy)
        migrated = inventory.to_dict()

        self.assertEqual(migrated["schema_version"], 2)
        self.assertNotIn("displayed", migrated)
        self.assertEqual(
            migrated["delivered"]["job-a"]["candidate-1"],
            {"batch_id": "legacy-board", "migrated_from": "displayed"},
        )
        self.assertEqual(inventory.select_undelivered("job-a", 5)["actual_count"], 0)

        candidate_2 = "candidate-2"
        inventory.ensure_resume(candidate_2, lambda: {"id": candidate_2})
        inventory.record_evaluation("job-a", candidate_2, evaluation(candidate_2, 80))
        preview = inventory.select_undelivered("job-a", 5)
        repeated_preview = inventory.select_undelivered("job-a", 5)
        self.assertEqual(preview, repeated_preview)
        self.assertEqual(inventory.undelivered_count("job-a"), 1)

    def test_sources_are_merged_without_changing_candidate_identity(self) -> None:
        inventory = CandidateInventory()
        inventory.record_source_card("candidate-1", "recommendation", {"position": 1})
        inventory.record_source_card("candidate-1", "search", {"query": "平台招商"})

        candidate = inventory.to_dict()["candidates"]["candidate-1"]
        self.assertEqual(sorted(candidate["sources"]), ["recommendation", "search"])

    def test_card_and_resume_statuses_are_persisted_without_detail_reads(self) -> None:
        inventory = CandidateInventory()
        inventory.record_source_card(
            "candidate-1",
            "recommendation",
            {"encryptGeekId": "boss-1", "encryptJobId": "job-open"},
        )

        self.assertEqual(
            inventory.candidate_status("candidate-1"),
            {"card": "ready", "resume": "pending"},
        )
        self.assertEqual(
            [row["candidate_id"] for row in inventory.list_resume_pending(job_id="job-open")],
            ["candidate-1"],
        )

        inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})

        self.assertEqual(
            inventory.candidate_status("candidate-1"),
            {"card": "ready", "resume": "ready"},
        )
        self.assertEqual(inventory.list_resume_pending(job_id="job-open"), [])

    def test_detail_selection_uses_discovery_order_and_keeps_unselected_candidates_pending(self) -> None:
        inventory = CandidateInventory()
        for index in range(1, 4):
            inventory.record_source_card(
                f"candidate-{index}",
                "search",
                {
                    "encryptGeekId": f"boss-{index}",
                    "encryptJobId": "job-open",
                    "securityId": f"security-{index}",
                },
            )
        inventory.ensure_resume("candidate-1", lambda: {"cached": True})

        selected = inventory.select_resume_pending(job_id="job-open", selection="1")

        self.assertEqual(
            [row["candidate_id"] for row in selected["candidates"]],
            ["candidate-2"],
        )
        self.assertEqual(selected["pending_count"], 2)
        self.assertEqual(selected["selected_count"], 1)
        self.assertEqual(selected["remaining_pending_count"], 1)
        self.assertEqual(
            [row["candidate_id"] for row in inventory.list_resume_pending(job_id="job-open")],
            ["candidate-2", "candidate-3"],
        )

        select_all = inventory.select_resume_pending(job_id="job-open", selection="all")
        self.assertEqual(
            [row["candidate_id"] for row in select_all["candidates"]],
            ["candidate-2", "candidate-3"],
        )

    def test_serialized_state_round_trips_without_shared_mutation(self) -> None:
        inventory = CandidateInventory()
        inventory.ensure_resume("candidate-1", lambda: {"id": "candidate-1"})
        saved = inventory.to_dict()
        restored = CandidateInventory(saved)
        saved["candidates"]["candidate-1"]["resume"]["id"] = "changed"

        self.assertEqual(restored.to_dict()["candidates"]["candidate-1"]["resume"]["id"], "candidate-1")

    def test_resume_evaluation_and_job_artifacts_are_reused_only_when_current(self) -> None:
        inventory = CandidateInventory()
        inventory.ensure_resume("candidate-1", lambda: {"id": "candidate-1"})
        inventory.record_evaluation("job-a", "candidate-1", evaluation("candidate-1", 88))
        rubric = {"source_jd_hash": "jd-v1", "version": "rubric-v1"}
        search_plan = {"source_jd_hash": "jd-v1", "version": "search-v1"}
        inventory.record_job_artifacts(
            "job-a",
            "jd-v1",
            rubric=rubric,
            search_plan=search_plan,
        )

        self.assertEqual(inventory.get_resume("candidate-1"), {"id": "candidate-1"})
        self.assertIsNotNone(
            inventory.get_evaluation("job-a", "candidate-1", rubric_version="rubric-v1")
        )
        self.assertIsNone(
            inventory.get_evaluation("job-a", "candidate-1", rubric_version="rubric-v2")
        )
        self.assertIsNotNone(inventory.get_job_artifacts("job-a", "jd-v1"))
        self.assertIsNone(inventory.get_job_artifacts("job-a", "jd-v2"))
        self.assertEqual(inventory.unshown_count("job-a", rubric_version="rubric-v1"), 1)

    def test_job_artifacts_preserve_v3_search_plan_without_approval_state(self) -> None:
        inventory = CandidateInventory()
        rubric = {"source_jd_hash": "jd-v3", "version": "rubric-v3"}
        search_plan = {
            "schema_version": 3,
            "contract": "generic_search_plan",
            "source_jd_hash": "jd-v3",
            "version": "search-v3",
            "target_route_count": 8,
            "generation_shortfall": 7,
            "routes": [
                {
                    "id": "route",
                    "priority": 1,
                    "tokens": ["汽配", "平台招商"],
                    "query": "汽配 平台招商",
                }
            ],
        }

        inventory.record_job_artifacts(
            "job-v3",
            "jd-v3",
            rubric=rubric,
            search_plan=search_plan,
        )
        stored = inventory.get_job_artifacts("job-v3", "jd-v3")

        self.assertEqual(stored["search_plan"], search_plan)
        self.assertNotIn("draft", stored)
        self.assertNotIn("approved", stored)
        self.assertNotIn("approval", stored)


if __name__ == "__main__":
    unittest.main()
