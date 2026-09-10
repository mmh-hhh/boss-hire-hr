from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boss_hire.recruiter_jobs import fetch_selected_open_job
from boss_hire.single_job_run_plan import build_single_job_run_plan, SingleJobRunConfig
from boss_hire.boss_live_authorization import _operation_manifest
from tests.test_recruiter_jobs import FakeRecruiterClient, FIXTURE


class SelectedJobTests(unittest.TestCase):
    def test_only_selected_detail_is_read_with_multiple_same_name_jobs(self):
        fixture = json.loads(FIXTURE.read_text())
        fixture['list_response']['zpData'].append({'encryptJobId': 'other', 'jobName': '同名', 'jobOnlineStatus': 1})
        client = FakeRecruiterClient(fixture['list_response'], fixture['details'])
        self.assertEqual(fetch_selected_open_job(client, 'job-open').encrypt_job_id, 'job-open')
        self.assertEqual(client.detail_calls, ['job-open'])

    def test_missing_closed_or_duplicate_selection_stops_before_detail(self):
        fixture = json.loads(FIXTURE.read_text())
        client = FakeRecruiterClient(fixture['list_response'], fixture['details'])
        for job_id in ('missing', 'job-closed'):
            with self.assertRaises(ValueError):
                fetch_selected_open_job(client, job_id)
        self.assertEqual(client.detail_calls, [])

    def test_selected_manifest_binds_recommendation_only_job(self):
        plan = build_single_job_run_plan(board_date='2026-09-09', config=SingleJobRunConfig(1, 0), auth_dir=Path('/tmp/fake-auth'), selected_job_id='B')
        self.assertEqual(plan['selected_job_id'], 'B')
        self.assertEqual(plan['operation_manifest'][1]['binding']['job_id'], 'B')
        self.assertEqual(_operation_manifest(plan), plan['operation_manifest'])
        broken = deepcopy(plan)
        broken['operation_manifest'][1]['binding']['job_id'] = 'A'
        with self.assertRaises(ValueError):
            _operation_manifest(broken)

    def test_selection_uses_only_current_job_artifacts_and_rejects_drift(self):
        from boss_hire.job_setup import resolve_prepared_job
        from boss_hire.local_security import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = {"A": {"source_jd_hash": "a"}, "B": {"source_jd_hash": "b"}}
            with self.assertRaises(ValueError):
                resolve_prepared_job(root, artifacts, account_key="test")
            atomic_write_json(root / "selected_job.json", {"account_key": "test", "job_id": "B", "source_jd_hash": "b"})
            self.assertEqual(resolve_prepared_job(root, artifacts, account_key="test")[0], "B")
            artifacts["B"]["source_jd_hash"] = "changed"
            with self.assertRaises(ValueError):
                resolve_prepared_job(root, artifacts, account_key="test")

    def test_transport_rejects_wrong_selected_job_before_reservation(self):
        from tests.test_safe_recruiter_client import FakeAuth, FakeGuard, FakeHttpClient
        from boss_hire.safe_recruiter_client import SafeBossRecruiterClient, BossRequestFailed
        guard = FakeGuard()
        guard.operation_bindings = {"metadata:selected-job-detail": {"job_id": "B"}}
        with SafeBossRecruiterClient(FakeAuth(), guard=guard) as client:
            http = FakeHttpClient([])
            client._client = http
            with client.operation("metadata:selected-job-detail"), self.assertRaises(BossRequestFailed):
                client.job_detail("A")
            self.assertEqual(http.calls, [])
            self.assertEqual(guard.attempts, [])

    def test_metadata_plans_cannot_mix_list_detail_or_change_job(self):
        from boss_hire.job_setup import build_job_read_plan
        summary = {'encrypt_job_id': 'B', 'name': '岗位', 'online_status': 1}
        plan = build_job_read_plan(account_key='abc', board_date='2026-09-09', summary=summary)
        self.assertEqual(len(_operation_manifest(plan)), 1)
        wrong = deepcopy(plan)
        wrong['operation_manifest'][0]['binding']['job_id'] = 'A'
        with self.assertRaises(ValueError):
            _operation_manifest(wrong)
        mixed = deepcopy(plan)
        mixed['operation_manifest'] += build_job_read_plan(account_key='abc', board_date='2026-09-09')['operation_manifest']
        with self.assertRaises(ValueError):
            _operation_manifest(mixed)
