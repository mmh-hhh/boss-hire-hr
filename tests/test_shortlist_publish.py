from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boss_hire.shortlist_publish import publish_candidate_shortlist
from boss_hire.supply_inventory import CandidateInventory
from tests.test_supply_inventory import evaluation


class ShortlistPublishTests(unittest.TestCase):
    def inventory(self, path: Path, count: int = 7) -> None:
        inventory = CandidateInventory()
        for index in range(1, count + 1):
            candidate_id = f"candidate-{index}"
            inventory.record_source_card(
                candidate_id,
                "search",
                {
                    "encryptGeekId": f"boss-{index}",
                    "encryptJobId": "job-open",
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
        inventory.save(path)

    def test_publish_selects_global_top_five_and_same_output_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            output_dir = root / "batches" / "2026-09-02-am"
            self.inventory(inventory_path)

            first = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=output_dir,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-02T10:00:00+08:00",
            )
            second = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=output_dir,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-02T10:00:00+08:00",
            )
            restored = CandidateInventory.load(inventory_path)

            self.assertEqual(first["batch"], second["batch"])
            self.assertEqual(first["batch"]["requested_count"], 5)
            self.assertEqual(first["batch"]["actual_count"], 5)
            self.assertEqual(
                [row["candidate_id"] for row in first["batch"]["candidates"]],
                [f"candidate-{index}" for index in range(1, 6)],
            )
            self.assertEqual(restored.undelivered_count("job-open", rubric_version="rubric-v1"), 2)
            self.assertEqual(json.loads(first["json_path"].read_text(encoding="utf-8")), first["batch"])
            self.assertIn("candidate-1", first["csv_path"].read_text(encoding="utf-8-sig"))

    def test_publish_reports_shortage_without_backfilling_or_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path, count=3)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-02T10:00:00+08:00",
            )

            self.assertEqual(result["batch"]["actual_count"], 3)
            self.assertEqual(result["batch"]["shortage_count"], 2)
            self.assertEqual(result["batch"]["shortage_reason"], "undelivered_inventory_exhausted")

    def test_publish_excludes_registered_boss_ids_and_only_advances_within_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path, count=7)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
                favorite_candidate_ids={"boss-2", "boss-4"},
                favorite_sync_status="anchor_reached",
                favorite_sync_complete=True,
                favorite_sync_receipt_id="favorite-sync-plan-a",
            )

            self.assertEqual(
                [row["candidate_id"] for row in result["batch"]["candidates"]],
                ["candidate-1", "candidate-3", "candidate-5", "candidate-6", "candidate-7"],
            )
            self.assertEqual(result["batch"]["favorite_registry_excluded_count"], 2)
            self.assertEqual(
                result["batch"]["favorite_registry_excluded_candidate_ids"],
                ["candidate-2", "candidate-4"],
            )
            self.assertIsNone(result["batch"]["favorite_coverage_warning"])
            self.assertEqual(result["summary"]["boss_requests"], 0)

    def test_publish_shortage_after_favorite_exclusion_never_backfills_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path, count=5)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
                favorite_candidate_ids={"boss-1", "boss-2"},
                favorite_sync_status="sync_incomplete",
                favorite_sync_complete=False,
                favorite_sync_receipt_id="favorite-sync-plan-b",
            )

            self.assertEqual(result["batch"]["actual_count"], 3)
            self.assertEqual(result["batch"]["shortage_count"], 2)
            self.assertEqual(
                result["batch"]["shortage_reason"],
                "favorite_registry_exclusion_exhausted_inventory",
            )
            self.assertEqual(
                result["batch"]["favorite_coverage_warning"],
                "favorite_registry_sync_incomplete",
            )
            self.assertEqual(result["summary"]["boss_requests"], 0)

    def test_only_stable_boss_id_matches_registry_not_local_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path, count=5)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
                favorite_candidate_ids={"candidate-1"},
                favorite_sync_status="end_reached",
                favorite_sync_complete=True,
            )

            self.assertEqual(result["batch"]["favorite_registry_excluded_count"], 0)
            self.assertEqual(result["batch"]["candidates"][0]["candidate_id"], "candidate-1")

    def test_publish_uses_latest_observed_security_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            inventory = CandidateInventory()
            for security_id in ("security-old", "security-new"):
                inventory.record_source_card(
                    "candidate-1",
                    "search",
                    {
                        "encryptGeekId": "boss-1",
                        "encryptJobId": "job-open",
                        "securityId": security_id,
                        "name": "候选人 1",
                    },
                )
            inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})
            inventory.record_evaluation(
                "job-open", "candidate-1", evaluation("candidate-1", 90)
            )
            inventory.save(inventory_path)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
            )

            self.assertEqual(
                result["batch"]["candidates"][0]["boss_identifiers"]["securityId"],
                "security-new",
            )

    def test_recovery_publish_uses_all_evaluated_top_five_without_redelivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path, count=7)
            inventory = CandidateInventory.load(inventory_path)
            inventory.mark_delivered(
                "batch-old",
                "job-open",
                ["candidate-1", "candidate-2", "candidate-3"],
            )
            before = inventory.to_dict()["delivered"]
            inventory.save(inventory_path)

            result = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=root / "recovery-batch",
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T11:00:00+08:00",
                favorite_candidate_ids={"boss-2"},
                favorite_sync_status="anchor_reached",
                favorite_sync_complete=True,
                selection_scope="all_evaluated",
            )

            self.assertEqual(result["batch"]["selection_scope"], "all_evaluated")
            self.assertEqual(
                [row["candidate_id"] for row in result["batch"]["candidates"]],
                ["candidate-1", "candidate-3", "candidate-4", "candidate-5", "candidate-6"],
            )
            self.assertEqual(result["batch"]["favorite_registry_excluded_candidate_ids"], ["candidate-2"])
            self.assertEqual(result["summary"]["boss_requests"], 0)
            self.assertEqual(CandidateInventory.load(inventory_path).to_dict()["delivered"], before)

    def test_existing_batch_rejects_selection_scope_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            self.inventory(inventory_path)
            output_dir = root / "batch"
            publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=output_dir,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
            )

            with self.assertRaisesRegex(ValueError, "selection_scope"):
                publish_candidate_shortlist(
                    inventory_path=inventory_path,
                    output_dir=output_dir,
                    job_id="job-open",
                    job_title="平台招商负责人",
                    rubric_version="rubric-v1",
                    published_at="2026-09-04T10:00:00+08:00",
                    selection_scope="all_evaluated",
                )

    def test_existing_immutable_batch_is_rejected_if_candidate_is_now_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            output_dir = root / "batch"
            self.inventory(inventory_path, count=7)
            publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=output_dir,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-04T10:00:00+08:00",
                favorite_candidate_ids=(),
                favorite_sync_status="end_reached",
                favorite_sync_complete=True,
            )

            with self.assertRaisesRegex(ValueError, "已收藏候选人"):
                publish_candidate_shortlist(
                    inventory_path=inventory_path,
                    output_dir=output_dir,
                    job_id="job-open",
                    job_title="平台招商负责人",
                    rubric_version="rubric-v1",
                    published_at="2026-09-04T10:00:00+08:00",
                    favorite_candidate_ids={"boss-1"},
                    favorite_sync_status="anchor_reached",
                    favorite_sync_complete=True,
                    favorite_sync_receipt_id="new-sync",
                )

    def test_failed_batch_write_does_not_mark_candidates_delivered_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory_path = root / "candidate_inventory.json"
            output_dir = root / "batch"
            self.inventory(inventory_path)

            with patch(
                "boss_hire.shortlist_publish.write_immutable_csv",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    publish_candidate_shortlist(
                        inventory_path=inventory_path,
                        output_dir=output_dir,
                        job_id="job-open",
                        job_title="平台招商负责人",
                        rubric_version="rubric-v1",
                        published_at="2026-09-02T10:00:00+08:00",
                    )

            self.assertEqual(
                CandidateInventory.load(inventory_path).undelivered_count(
                    "job-open", rubric_version="rubric-v1"
                ),
                7,
            )

            resumed = publish_candidate_shortlist(
                inventory_path=inventory_path,
                output_dir=output_dir,
                job_id="job-open",
                job_title="平台招商负责人",
                rubric_version="rubric-v1",
                published_at="2026-09-02T10:00:00+08:00",
            )

            self.assertEqual(resumed["batch"]["actual_count"], 5)
            self.assertEqual(
                CandidateInventory.load(inventory_path).undelivered_count(
                    "job-open", rubric_version="rubric-v1"
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
