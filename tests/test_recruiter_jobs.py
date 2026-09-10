from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from boss_hire.recruiter_jobs import (
    OPEN_JOB_ONLINE_STATUS,
    fetch_open_jobs,
    fetch_single_open_job,
    normalize_job_detail,
    normalize_job_summary,
)


FIXTURE = Path(__file__).parent / "fixtures/recruiter_jobs.json"


class FakeRecruiterClient:
    def __init__(self, list_response: dict[str, Any], details: dict[str, Any]) -> None:
        self.list_response = list_response
        self.details = details
        self.detail_calls: list[str] = []

    def list_jobs(self) -> dict[str, Any]:
        return self.list_response

    def job_detail(self, encrypt_job_id: str) -> dict[str, Any]:
        self.detail_calls.append(encrypt_job_id)
        return self.details[encrypt_job_id]


class RecruiterJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fetch_open_jobs_filters_before_loading_details(self) -> None:
        client = FakeRecruiterClient(
            self.fixture["list_response"],
            self.fixture["details"],
        )

        jobs = fetch_open_jobs(client)

        self.assertEqual(client.detail_calls, ["job-open"])
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertTrue(job.is_open)
        self.assertEqual(job.online_status, OPEN_JOB_ONLINE_STATUS)
        self.assertEqual(job.name, "跨境汽配平台招商总监")
        self.assertEqual(job.description, "负责建立跨境汽配商家池并管理平台招商结果。")
        self.assertEqual(job.experience_label, "5-10年")
        self.assertEqual(job.degree_label, "本科")
        self.assertIn("薪资范围：30-60K", job.to_jd_text())
        self.assertIn("平台学历要求：本科", job.to_jd_text())

    def test_non_open_statuses_are_not_treated_as_open(self) -> None:
        summaries = [
            normalize_job_summary(item)
            for item in self.fixture["list_response"]["zpData"]
        ]
        self.assertEqual(
            [row["encrypt_job_id"] for row in summaries if row["online_status"] == OPEN_JOB_ONLINE_STATUS],
            ["job-open"],
        )

    def test_missing_summary_status_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing identity/status"):
            normalize_job_summary({"jobName": "岗位", "encryptJobId": "job"})

    def test_detail_identity_mismatch_fails_closed(self) -> None:
        response = self.fixture["details"]["job-open"]
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            normalize_job_detail(
                {"encrypt_job_id": "different-job", "name": "岗位", "online_status": 1},
                response,
            )

    def test_open_job_without_full_description_fails_closed(self) -> None:
        response = {
            "code": 0,
            "zpData": {"job": {"encryptId": "job-open", "jobName": "岗位"}},
        }
        with self.assertRaisesRegex(RuntimeError, "no postDescription"):
            normalize_job_detail(
                {"encrypt_job_id": "job-open", "name": "岗位", "online_status": 1},
                response,
            )

    def test_list_failure_is_not_silently_treated_as_empty(self) -> None:
        client = FakeRecruiterClient({"code": 9, "message": "limited"}, {})
        with self.assertRaisesRegex(RuntimeError, "list_jobs failed"):
            fetch_open_jobs(client)

    def test_single_job_fetch_stops_before_details_when_open_job_count_is_not_one(self) -> None:
        response = {
            "code": 0,
            "zpData": [
                {"encryptJobId": "job-1", "jobName": "岗位一", "jobOnlineStatus": 1},
                {"encryptJobId": "job-2", "jobName": "岗位二", "jobOnlineStatus": 1},
            ],
        }
        client = FakeRecruiterClient(response, {})

        with self.assertRaisesRegex(RuntimeError, "exactly one open job"):
            fetch_single_open_job(client)

        self.assertEqual(client.detail_calls, [])

    def test_single_job_fetch_loads_only_the_unique_open_job_detail(self) -> None:
        client = FakeRecruiterClient(self.fixture["list_response"], self.fixture["details"])

        job = fetch_single_open_job(client)

        self.assertEqual(job.encrypt_job_id, "job-open")
        self.assertEqual(client.detail_calls, ["job-open"])


if __name__ == "__main__":
    unittest.main()
