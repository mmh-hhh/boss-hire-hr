from __future__ import annotations

import tempfile
import unittest
import json
import os
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from boss_hire.single_job_live import (
    collect_single_job_cards,
    execute_favorite_candidate,
    run_candidate_detail_collection,
    run_favorite_delivery,
    run_favorite_registry_sync,
    run_single_job_source_collection,
)
from boss_hire.single_job_run_plan import (
    SingleJobRunConfig,
    build_candidate_detail_run_plan,
    build_favorite_delivery_operation_manifest,
    build_favorite_sync_run_plan,
    build_single_job_run_plan,
    write_single_job_run_plan,
)
from boss_hire.boss_access import BossLiveAccessDenied, account_key_for
from boss_hire.favorite_delivery import FavoriteDeliveryLedger, build_favorite_delivery_plan
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.safe_recruiter_client import BossRequestFailed, BossRiskStop
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory
from boss_hire.workflow_run import (
    load_active_workflow_run,
    load_workflow_run,
    save_active_workflow_run,
    transition_workflow_run_state,
)
from scripts import run_single_job_live
from tests.test_favorite_delivery import published_batch
from tests.test_local_scoring import FakeEvaluationLlm, RUBRIC as LOCAL_SCORING_RUBRIC
from tests.test_supply_inventory import evaluation

def card(candidate_id: str, name: str) -> dict[str, Any]:
    return {
        "geekCard": {
            "encryptGeekId": candidate_id,
            "securityId": f"security-{candidate_id}",
            "encryptJobId": "job-open",
            "name": name,
            "workYear": "8年",
            "highestDegreeName": "本科",
            "current": {"name": "招商负责人"},
        }
    }


class FakeClient:
    def __init__(self) -> None:
        self.recommendation_calls = 0
        self.search_calls: list[str] = []
        self.search_filter_calls: list[str] = []
        self.search_param_calls: list[dict[str, str]] = []
        self.detail_calls: list[str] = []

    def list_jobs(self) -> dict[str, Any]:
        return {
            "code": 0,
            "zpData": [
                {"encryptJobId": "job-open", "jobName": "平台招商负责人", "jobOnlineStatus": 1},
                {"encryptJobId": "job-closed", "jobName": "已关闭岗位", "jobOnlineStatus": 2},
            ],
        }

    def job_detail(self, job_id: str) -> dict[str, Any]:
        self.detail_calls.append(f"job:{job_id}")
        return {
            "code": 0,
            "zpData": {
                "job": {
                    "encryptId": "job-open",
                    "jobName": "平台招商负责人",
                    "jobStatus": 1,
                    "postDescription": "负责重点商家拓展、平台招商策略、商务谈判和招商团队建设。需要理解电商平台经营和商家增长。",
                    "positionName": "招商负责人",
                    "locationName": "上海",
                    "experience": 106,
                    "degree": 203,
                }
            },
        }

    def _request(self, method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.recommendation_calls += 1
        return {"code": 0, "zpData": {"geekList": [card("boss-a", "候选人甲"), card("boss-c", "候选人丙")], "hasMore": False}}

    def search_geeks(
        self,
        query: str,
        *,
        page: int,
        job_id: str,
        recent_view_filter: str = "include_all",
        **search_filter_params: str,
    ) -> dict[str, Any]:
        self.search_calls.append(query)
        self.search_filter_calls.append(recent_view_filter)
        self.search_param_calls.append(search_filter_params)
        return {"code": 0, "zpData": {"geeks": [card("boss-b", "候选人乙")]}}

    def view_geek(self, geek_id: str, job_id: str, *, security_id: str) -> dict[str, Any]:
        self.detail_calls.append(geek_id)
        responsibility = {
            "boss-a": "负责重点商家拓展、平台招商策略、商务谈判和招商团队建设",
            "boss-b": "负责重点商家拓展",
            "boss-c": "负责平台招商策略和商务谈判",
        }[geek_id]
        return {
            "resume": {
                "basic": {"name": "真实姓名", "phone": "13800138000", "degree": "本科", "work_years": "8年"},
                "work_experience": [{"company": "某平台", "position": "招商负责人", "responsibility": responsibility}],
                "education": [{"school": "某学校", "degree": "本科", "major": "市场营销"}],
            }
        }



class RouteClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    @contextmanager
    def operation(self, operation_key: str):
        self.events.append(f"operation:{operation_key}")
        yield

    def _request(self, method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.events.append("request:recommendation")
        self.recommendation_calls += 1
        return {
            "code": 0,
            "zpData": {
                "geekList": [card("rec-1", "推荐甲"), card("rec-2", "推荐乙")],
                "hasMore": True,
            },
        }

    def search_geeks(
        self,
        query: str,
        *,
        page: int,
        job_id: str,
        recent_view_filter: str = "include_all",
        **search_filter_params: str,
    ) -> dict[str, Any]:
        self.events.append(f"request:search:{query}")
        self.search_calls.append(query)
        self.search_filter_calls.append(recent_view_filter)
        self.search_param_calls.append(search_filter_params)
        prefix = "title" if query == "平台招商负责人" else "merchant"
        return {
            "code": 0,
            "zpData": {
                "geeks": [
                    card(f"{prefix}-1", f"{prefix}甲"),
                    card(f"{prefix}-2", f"{prefix}乙"),
                ]
            },
        }


class PageRouteClient(RouteClient):
    def __init__(self, *, fail_on: tuple[str, int] | None = None) -> None:
        super().__init__()
        self.fail_on = fail_on

    def search_geeks(
        self,
        query: str,
        *,
        page: int,
        job_id: str,
        recent_view_filter: str = "include_all",
        **search_filter_params: str,
    ) -> dict[str, Any]:
        self.events.append(f"request:search:{query}:page:{page}")
        self.search_calls.append(f"{query}:page:{page}")
        self.search_filter_calls.append(recent_view_filter)
        self.search_param_calls.append(search_filter_params)
        if self.fail_on == (query, page):
            raise BossRiskStop("risk stop", outcome="risk", transport_attempted=True)
        prefix = "title" if query == "平台招商负责人" else "merchant"
        ids = {
            ("title", 1): ["shared", "title-p1"],
            ("title", 2): ["shared", "title-p2"],
            ("merchant", 1): ["shared", "merchant-p1"],
            ("merchant", 2): ["merchant-p2"],
        }[(prefix, page)]
        return {
            "code": 0,
            "zpData": {"geeks": [card(candidate_id, candidate_id) for candidate_id in ids], "hasMore": True},
        }


class FavoriteSyncClient:
    def __init__(self, payloads: list[dict[str, Any] | Exception]) -> None:
        self.payloads = payloads
        self.events: list[str] = []

    @contextmanager
    def operation(self, operation_key: str):
        self.events.append(f"operation:{operation_key}")
        yield

    def favorite_list(self, *, page: int) -> dict[str, Any]:
        self.events.append(f"request:page:{page}")
        value = self.payloads[page - 1]
        if isinstance(value, Exception):
            raise value
        return value


def parse_resume(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["resume"]


class BossLiveExecutionTests(unittest.TestCase):
    def _favorite_registry(self, root: Path) -> FavoriteRegistry:
        return FavoriteRegistry(
            root / "favorites",
            account_key="0123456789abcdef",
        )

    def _plan(
        self,
        *,
        statuses: dict[str, str] | None = None,
        retry_definite_failures: bool = False,
    ) -> dict[str, Any]:
        plan = build_favorite_delivery_plan(
            published_batch(),
            selected_ranks=[1, 2, 4],
            favorite_statuses=statuses,
            retry_definite_failures=retry_definite_failures,
        )
        plan["operation_manifest_kind"] = "favorite_delivery"
        plan["operation_manifest"] = build_favorite_delivery_operation_manifest(
            batch_id=plan["batch_id"],
            candidates=plan["candidates"],
        )
        return plan

    def test_executor_retries_definite_failure_only_when_plan_explicitly_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = FavoriteDeliveryLedger(root / "state.json")
            ledger.set_status(
                "candidate-4",
                "favorite_failed",
                batch_id="shortlist-old",
                operation_key="favorite:shortlist-old:candidate-4:write",
            )
            ledger.save()
            plan = self._plan(
                statuses={"candidate-4": "favorite_failed"},
                retry_definite_failures=True,
            )
            calls: list[str] = []

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=self._favorite_registry(root),
                execute_candidate=lambda candidate, _write, _verify: (
                    calls.append(candidate["candidate_id"])
                    or {"status": "favorite_confirmed"}
                ),
                work_dir=root / "run",
            )

            self.assertEqual(calls, ["candidate-4", "candidate-2", "candidate-1"])
            self.assertEqual(result["receipt"]["confirmed_count"], 3)
            record = FavoriteDeliveryLedger(root / "state.json").record("candidate-4")
            self.assertEqual(record["status"], "favorite_confirmed")
            self.assertEqual(record["attempt_history"][0]["status"], "favorite_failed")

    def test_executor_delivers_low_to_high_rank_so_highest_score_is_most_recent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan()
            calls: list[tuple[str, str, str]] = []

            def execute(
                candidate: dict[str, Any],
                write: dict[str, Any],
                verify: dict[str, Any],
            ) -> dict[str, Any]:
                calls.append(
                    (
                        candidate["candidate_id"],
                        write["operation_key"],
                        verify["operation_key"],
                    )
                )
                self.assertEqual(
                    FavoriteDeliveryLedger(root / "state.json").status(
                        candidate["candidate_id"]
                    ),
                    "write_reserved",
                )
                return {"status": "favorite_confirmed"}

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=self._favorite_registry(root),
                execute_candidate=execute,
                work_dir=root / "run",
                generated_at="2026-09-03T10:01:00+08:00",
            )

            self.assertEqual(
                [row[0] for row in calls],
                ["candidate-4", "candidate-2", "candidate-1"],
            )
            self.assertEqual(result["receipt"]["confirmed_count"], 3)
            self.assertEqual(result["receipt"]["not_selected_count"], 2)
            self.assertEqual(
                [row["candidate_id"] for row in result["receipt"]["results"]],
                ["candidate-4", "candidate-2", "candidate-1"],
            )
            self.assertTrue(result["receipt_path"].is_file())

    def _incremental_plan(self, root: Path, registry: FavoriteRegistry) -> dict[str, Any]:
        registry.save_complete_checkpoint(
            anchor_group=["anchor-1", "anchor-2"],
            receipt_id="previous-sync",
            sync_status="end_reached",
            completed_at="2026-08-31T10:00:00+08:00",
        )
        return build_favorite_sync_run_plan(
            board_date="2026-09-01",
            auth_dir=root / "auth",
            mode="incremental",
            purpose="publish",
            checkpoint=registry.checkpoint(),
            max_pages=4,
        )

    def test_incremental_sync_stops_after_complete_anchor_without_later_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_key = account_key_for(root / "auth")
            registry = FavoriteRegistry(root / "favorites", account_key=account_key)
            plan = self._incremental_plan(root, registry)
            client = FavoriteSyncClient(
                [
                    {
                        "code": 0,
                        "zpData": {
                            "cardList": [
                                {"encryptGeekId": "new-1"},
                                {"encryptGeekId": "anchor-1"},
                            ],
                            "hasMore": True,
                        },
                    },
                    {
                        "code": 0,
                        "zpData": {
                            "cardList": [
                                {"encryptGeekId": "anchor-2"},
                                {"encryptGeekId": "older-1"},
                            ],
                            "hasMore": True,
                        },
                    },
                ]
            )

            result = run_favorite_registry_sync(
                plan=plan,
                client=client,
                registry=registry,
                work_dir=root / "run",
                generated_at="2026-09-01T10:00:00+08:00",
            )

            self.assertEqual(
                client.events,
                [
                    f"operation:{plan['operation_manifest'][0]['operation_key']}",
                    "request:page:1",
                    f"operation:{plan['operation_manifest'][1]['operation_key']}",
                    "request:page:2",
                ],
            )
            self.assertEqual(result["receipt"]["status"], "anchor_reached")
            self.assertTrue(result["receipt"]["complete"])
            self.assertTrue(registry.contains("older-1"))
            self.assertEqual(registry.checkpoint()["anchor_group"], ["new-1", "anchor-1"])

    def test_risk_stop_persists_prior_page_and_never_requests_following_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_key = account_key_for(root / "auth")
            registry = FavoriteRegistry(root / "favorites", account_key=account_key)
            plan = build_favorite_sync_run_plan(
                board_date="2026-09-01",
                auth_dir=root / "auth",
                mode="initialize",
                purpose="publish",
                checkpoint=None,
                max_pages=4,
            )
            client = FavoriteSyncClient(
                [
                    {
                        "code": 0,
                        "zpData": {
                            "cardList": [{"encryptGeekId": "new-1"}],
                            "hasMore": True,
                        },
                    },
                    BossRiskStop("risk", outcome="risk_stop", transport_attempted=True),
                ]
            )

            result = run_favorite_registry_sync(
                plan=plan,
                client=client,
                registry=registry,
                work_dir=root / "run",
                generated_at="2026-09-01T10:00:00+08:00",
            )

            self.assertEqual(client.events[-1], "request:page:2")
            self.assertNotIn("request:page:3", client.events)
            self.assertEqual(result["receipt"]["status"], "risk_stopped")
            self.assertFalse(result["receipt"]["complete"])
            self.assertTrue(registry.contains("new-1"))
            self.assertIsNone(registry.checkpoint())

    def test_incremental_sync_rejects_changed_checkpoint_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_key = account_key_for(root / "auth")
            registry = FavoriteRegistry(root / "favorites", account_key=account_key)
            plan = self._incremental_plan(root, registry)
            registry.save_complete_checkpoint(
                anchor_group=["newer-anchor"],
                receipt_id="other-sync",
                sync_status="anchor_reached",
                completed_at="2026-09-01T09:00:00+08:00",
            )
            client = FavoriteSyncClient([])

            with self.assertRaisesRegex(ValueError, "checkpoint"):
                run_favorite_registry_sync(
                    plan=plan,
                    client=client,
                    registry=registry,
                    work_dir=root / "run",
                    generated_at="2026-09-01T10:00:00+08:00",
                )

            self.assertEqual(client.events, [])

    def test_executor_skips_globally_confirmed_candidate_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(statuses={"candidate-1": "favorite_confirmed"})
            calls: list[str] = []

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=self._favorite_registry(root),
                execute_candidate=lambda candidate, _write, _verify: (
                    calls.append(candidate["candidate_id"])
                    or {"status": "favorite_confirmed"}
                ),
                work_dir=root / "run",
            )

            self.assertEqual(calls, ["candidate-4", "candidate-2"])
            self.assertEqual(result["receipt"]["already_confirmed_count"], 1)
            self.assertEqual(result["receipt"]["confirmed_count"], 2)

    def test_duplicate_global_reservation_fails_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan()
            first_write = plan["operation_manifest"][0]
            FavoriteDeliveryLedger(root / "state.json").reserve_write(
                "candidate-4",
                batch_id=plan["batch_id"],
                operation_key=first_write["operation_key"],
            )
            calls: list[str] = []

            with self.assertRaisesRegex(RuntimeError, "already attempted"):
                run_favorite_delivery(
                    plan=plan,
                    ledger=FavoriteDeliveryLedger(root / "state.json"),
                    registry=self._favorite_registry(root),
                    execute_candidate=lambda candidate, _write, _verify: (
                        calls.append(candidate["candidate_id"])
                        or {"status": "favorite_confirmed"}
                    ),
                    work_dir=root / "run",
                )

            self.assertEqual(calls, [])

    def test_exact_write_and_readback_confirm_each_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan()
            client = FavoriteClient(
                readbacks={
                    "geek-1": {"code": 0, "zpData": {"alreadyInterested": 1}},
                    "geek-2": {"code": 0, "zpData": {"alreadyInterested": 1}},
                    "geek-4": {"code": 0, "zpData": {"alreadyInterested": 1}},
                }
            )

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=self._favorite_registry(root),
                execute_candidate=lambda candidate, write, verify: execute_favorite_candidate(
                    client=client,
                    candidate=candidate,
                    write_operation=write,
                    verify_operation=verify,
                ),
                work_dir=root / "run",
            )

            self.assertEqual(result["receipt"]["confirmed_count"], 3)
            self.assertEqual(
                client.calls,
                [
                    ("write", "geek-4"),
                    ("read", "geek-4"),
                    ("write", "geek-2"),
                    ("read", "geek-2"),
                    ("write", "geek-1"),
                    ("read", "geek-1"),
                ],
            )

    def test_unconfirmed_readback_marks_unknown_and_stops_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan()
            client = FavoriteClient(
                readbacks={"geek-4": {"code": 0, "zpData": {"alreadyInterested": 0}}}
            )

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=self._favorite_registry(root),
                execute_candidate=lambda candidate, write, verify: execute_favorite_candidate(
                    client=client,
                    candidate=candidate,
                    write_operation=write,
                    verify_operation=verify,
                ),
                work_dir=root / "run",
            )

            self.assertEqual(client.calls, [("write", "geek-4"), ("read", "geek-4")])
            self.assertEqual(result["receipt"]["unknown_count"], 1)
            self.assertTrue(result["receipt"]["stopped_early"])
            self.assertEqual(
                FavoriteDeliveryLedger(root / "state.json").status("candidate-4"),
                "favorite_unknown",
            )

    def test_registry_recheck_skips_without_transport_or_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan()
            registry = self._favorite_registry(root)
            registry.record_candidates(
                ["geek-4"],
                source="manual_verified",
                receipt_id="manual-before-reservation",
                observed_at="2026-09-03T10:00:30+08:00",
            )
            calls: list[str] = []

            result = run_favorite_delivery(
                plan=plan,
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=registry,
                execute_candidate=lambda candidate, _write, _verify: (
                    calls.append(candidate["candidate_id"])
                    or {"status": "favorite_confirmed"}
                ),
                work_dir=root / "run",
                generated_at="2026-09-03T10:01:00+08:00",
            )

            self.assertEqual(calls, ["candidate-2", "candidate-1"])
            self.assertEqual(result["receipt"]["selected_count"], 3)
            self.assertEqual(result["receipt"]["already_confirmed_count"], 1)
            self.assertEqual(result["receipt"]["confirmed_count"], 2)
            self.assertEqual(
                FavoriteDeliveryLedger(root / "state.json").status("candidate-4"),
                "not_requested",
            )

    def test_confirmed_readback_registers_stable_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = self._favorite_registry(root)

            run_favorite_delivery(
                plan=self._plan(),
                ledger=FavoriteDeliveryLedger(root / "state.json"),
                registry=registry,
                execute_candidate=lambda candidate, _write, _verify: {
                    "status": "favorite_confirmed"
                },
                work_dir=root / "run",
                generated_at="2026-09-03T10:01:00+08:00",
            )

            self.assertEqual(
                registry.known_candidate_ids(),
                {"geek-1", "geek-2", "geek-4"},
            )
            self.assertIn("favorite_confirmed", registry.record("geek-4")["sources"])

    def test_write_failure_or_risk_stops_before_any_later_candidate(self) -> None:
        errors = (
            BossRequestFailed(
                "BOSS response code 121",
                outcome="response_error",
                transport_attempted=True,
                definite_rejection=True,
                response_code=121,
            ),
            BossRiskStop("BOSS risk stop: code_36"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                client = FavoriteClient(write_error=error)
                result = run_favorite_delivery(
                    plan=self._plan(),
                    ledger=FavoriteDeliveryLedger(root / "state.json"),
                    registry=self._favorite_registry(root),
                    execute_candidate=lambda candidate, write, verify: execute_favorite_candidate(
                        client=client,
                        candidate=candidate,
                        write_operation=write,
                        verify_operation=verify,
                    ),
                    work_dir=root / "run",
                )

                self.assertEqual(client.calls, [("write", "geek-4")])
                self.assertEqual(len(result["receipt"]["results"]), 1)
                self.assertTrue(result["receipt"]["stopped_early"])

    def test_definite_write_failure_skips_readback_and_stops_batch(self) -> None:
        client = FavoriteClient(
            write_error=BossRequestFailed(
                "BOSS response code 121",
                outcome="response_error",
                transport_attempted=True,
                definite_rejection=True,
                response_code=121,
            )
        )
        result = execute_favorite_candidate(
            client=client,
            candidate=self._plan()["candidates"][0],
            write_operation=self._plan()["operation_manifest"][0],
            verify_operation=self._plan()["operation_manifest"][1],
        )

        self.assertEqual(result["status"], "favorite_failed")
        self.assertEqual(result["response_code"], 121)
        self.assertEqual(client.calls, [("write", "geek-4")])

    def test_risk_or_network_uncertainty_never_reads_or_retries(self) -> None:
        for error in (
            BossRiskStop("BOSS risk stop: code_36"),
            BossRequestFailed(
                "single BOSS request failed: TimeoutError",
                outcome="network_error",
                transport_attempted=True,
            ),
        ):
            with self.subTest(error=type(error).__name__):
                client = FavoriteClient(write_error=error)
                plan = self._plan()
                result = execute_favorite_candidate(
                    client=client,
                    candidate=plan["candidates"][0],
                    write_operation=plan["operation_manifest"][0],
                    verify_operation=plan["operation_manifest"][1],
                )

                self.assertEqual(result["status"], "favorite_unknown")
                self.assertEqual(client.calls, [("write", "geek-4")])


class FavoriteClient:
    def __init__(
        self,
        *,
        readbacks: dict[str, dict[str, Any]] | None = None,
        write_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.readbacks = readbacks or {}
        self.write_error = write_error
        self.read_error = read_error
        self.calls: list[tuple[str, str]] = []
        self.operations: list[str] = []

    @contextmanager
    def operation(self, operation_key: str):
        self.operations.append(operation_key)
        yield

    def favorite_candidate(self, *, encrypt_geek_id: str, security_id: str) -> dict[str, Any]:
        self.calls.append(("write", encrypt_geek_id))
        if self.write_error is not None:
            raise self.write_error
        return {"code": 0, "zpData": {"mark": True}}

    def favorite_status(
        self,
        *,
        encrypt_geek_id: str,
        encrypt_job_id: str,
        security_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("read", encrypt_geek_id))
        if self.read_error is not None:
            raise self.read_error
        return self.readbacks.get(encrypt_geek_id, {"code": 0, "zpData": {}})


class SingleJobLiveTests(unittest.TestCase):
    def test_help_marks_new_primary_flow_and_preserves_legacy_commands(self) -> None:
        help_text = run_single_job_live.build_parser().format_help()

        self.assertIn("configs/start/status/continue", help_text)
        self.assertIn("authorize", help_text)
        self.assertIn("run", help_text)
        self.assertIn("favorite", help_text)
        self.assertIn("clear-circuit", help_text)
        authorize = run_single_job_live.parse_args(
            [
                "authorize",
                "--config",
                "config.json",
                "--work-dir",
                "runs",
                "--confirm-live",
                "2026-09-07",
                "--note",
                "人工确认",
            ]
        )
        legacy_run = run_single_job_live.parse_args(
            [
                "run",
                "--live",
                "--authorization-id",
                "authorization-1",
                "--config",
                "config.json",
                "--work-dir",
                "runs",
            ]
        )
        clear = run_single_job_live.parse_args(["clear-circuit", "--note", "人工复核"])

        self.assertEqual(authorize.command, "authorize")
        self.assertEqual(legacy_run.authorization_id, "authorization-1")
        self.assertEqual(clear.command, "clear-circuit")

    def test_plain_favorite_dispatches_to_the_normal_workflow_router(self) -> None:
        with patch.object(run_single_job_live, "favorite_command", return_value=0) as command:
            result = run_single_job_live.main(["favorite"])

        self.assertEqual(result, 0)
        command.assert_called_once()
        self.assertTrue(command.call_args.args[0].favorite_workflow)

    def test_close_changes_only_local_state_and_preserves_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(root, pending_count=1)
            active = load_active_workflow_run(paths)
            source_plan_path = active.state_path.parent / "source_plan.json"
            args = run_single_job_live.parse_args(["close", "--note", "本轮手工结束"])
            output: list[str] = []
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "LiveAuthorizationStore") as auth_store:
                    with patch.object(run_single_job_live, "clear_circuit_command") as clear_circuit:
                        result = run_single_job_live.close_command(
                            args,
                            output_fn=output.append,
                            now=lambda: datetime(
                                2026, 9, 7, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                            ),
                        )

            closed = load_active_workflow_run(paths)
            self.assertEqual(result, 0)
            self.assertEqual(closed.state["status"], "closed")
            self.assertTrue(source_plan_path.is_file())
            self.assertTrue((closed.state_path.parent / "close_receipt.json").is_file())
            auth_store.assert_not_called()
            clear_circuit.assert_not_called()
            self.assertIn("未删除产物", output[1])

    def test_close_requires_explicit_run_when_active_pointer_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(root, pending_count=1)
            run_id = load_active_workflow_run(paths).state["run_id"]
            paths.active_run_path.unlink()
            ambiguous = run_single_job_live.parse_args(["close", "--note", "结束"])
            explicit = run_single_job_live.parse_args(
                ["close", "--run", run_id, "--note", "结束"]
            )
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with self.assertRaisesRegex(ValueError, "请显式传入 close --run"):
                    run_single_job_live.close_command(ambiguous)
                result = run_single_job_live.close_command(
                    explicit,
                    output_fn=lambda _line: None,
                    now=lambda: datetime(
                        2026, 9, 7, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                    ),
                )

            self.assertEqual(result, 0)
            self.assertEqual(load_workflow_run(paths, run_id).state["status"], "closed")

    def test_close_fails_closed_when_active_pointer_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(root, pending_count=1)
            pointer = json.loads(paths.active_run_path.read_text(encoding="utf-8"))
            pointer["state_digest"] = "f" * 64
            paths.active_run_path.write_text(json.dumps(pointer), encoding="utf-8")
            args = run_single_job_live.parse_args(["close", "--note", "结束"])
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "LiveAuthorizationStore") as auth_store:
                    with self.assertRaisesRegex(ValueError, "摘要不一致"):
                        run_single_job_live.close_command(args)
            auth_store.assert_not_called()

    def _write_start_inputs(
        self,
        root: Path,
        *,
        job_count: int = 1,
        rubric: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        paths = run_single_job_live.workflow_paths(root)
        paths.config_root.mkdir(parents=True)
        (paths.config_root / "search_one.json").write_text(
            json.dumps(
                {
                    "recommendation_source_enabled": 0,
                    "top_priority_search_query_count": 1,
                }
            ),
            encoding="utf-8",
        )
        inventory = CandidateInventory()
        for index in range(job_count):
            job_id = f"job-{index + 1}"
            selected_rubric = rubric if rubric is not None and index == 0 else None
            jd_digest = (
                str(selected_rubric["source_jd_hash"])
                if selected_rubric is not None
                else content_hash(f"jd-{index + 1}")
            )
            inventory.record_job_artifacts(
                job_id,
                jd_digest,
                rubric=(
                    selected_rubric
                    if selected_rubric is not None
                    else {"source_jd_hash": jd_digest, "version": f"rubric-{index + 1}"}
                ),
                search_plan={
                    "schema_version": 1,
                    "contract": "generic_search_plan",
                    "source_jd_hash": jd_digest,
                    "version": f"search-{index + 1}",
                    "routes": [{"id": "route-1", "query": "汽配 平台招商"}],
                },
            )
        inventory.save(paths.inventory_path)
        return paths

    def _write_detail_continue_inputs(
        self,
        root: Path,
        *,
        pending_count: int = 2,
        rubric: dict[str, Any] | None = None,
    ):
        paths = self._write_start_inputs(root, rubric=rubric)
        inventory = CandidateInventory.load(paths.inventory_path)
        for index in range(pending_count):
            inventory.record_source_card(
                f"candidate-{index + 1}",
                "search",
                {
                    "encryptGeekId": f"boss-{index + 1}",
                    "encryptJobId": "job-1",
                    "securityId": f"security-{index + 1}",
                },
            )
        inventory.save(paths.inventory_path)
        preparation = run_single_job_live._prepare_start(
            config_name="search_one",
            paths=paths,
            board_date="2026-09-07",
        )
        created = run_single_job_live._freeze_source_run(
            preparation,
            datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        source_complete = transition_workflow_run_state(
            created.state,
            status="source_complete",
            updated_at="2026-09-07T10:01:00+08:00",
        )
        save_active_workflow_run(paths, source_complete, expected_state_digest=created.state_digest)
        return paths

    def test_start_parser_uses_config_name_without_paths_or_authorization_id(self) -> None:
        args = run_single_job_live.parse_args(
            [
                "start",
                "--config",
                "search_one",
                "--filter",
                "学历=本科及以上",
                "--filter",
                "院校=985院校",
            ]
        )

        self.assertEqual(args.command, "start")
        self.assertEqual(args.config, "search_one")
        self.assertEqual(args.filter, ["学历=本科及以上", "院校=985院校"])
        self.assertFalse(hasattr(args, "work_dir"))
        self.assertFalse(hasattr(args, "authorization_id"))

    def test_start_preview_freezes_and_displays_readable_search_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp))
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", Path(tmp) / "auth"):
                preparation = run_single_job_live._prepare_start(
                    config_name="search_one",
                    paths=paths,
                    board_date="2026-09-07",
                    search_filter_inputs=("学历=本科及以上", "院校=985院校"),
                )
            output: list[str] = []
            expected = run_single_job_live._render_start_preview(preparation, output.append)

        self.assertEqual(
            preparation.plan["search_filter_params"],
            {"degree": "203,201", "school_level": "1104"},
        )
        self.assertIn("搜索条件：学历=本科及以上；院校要求=985院校", output)
        self.assertEqual(expected, "确认来源3次")

    def test_start_rejects_wrong_confirmation_before_auth_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp))
            args = run_single_job_live.parse_args(["start", "--config", "search_one"])
            output: list[str] = []
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", Path(tmp) / "auth"):
                    with patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync:
                        with self.assertRaisesRegex(BossLiveAccessDenied, "确认文本不匹配"):
                            run_single_job_live.start_command(
                                args,
                                input_fn=lambda _prompt: "不确认",
                                output_fn=output.append,
                                now=lambda: datetime(
                                    2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                                ),
                            )

        self.assertIn("确认文本：确认来源3次", output)
        auth_sync.assert_not_called()

    def test_start_fails_closed_for_missing_config_active_run_and_multiple_jobs(self) -> None:
        args = run_single_job_live.parse_args(["start", "--config", "missing"])
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp), job_count=2)
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with self.assertRaisesRegex(ValueError, "run config 不存在"):
                    run_single_job_live.start_command(args, input_fn=lambda _prompt: "")

            args = run_single_job_live.parse_args(["start", "--config", "search_one"])
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", Path(tmp) / "auth"):
                    with self.assertRaisesRegex(ValueError, "不会猜测岗位"):
                        run_single_job_live.start_command(args, input_fn=lambda _prompt: "")

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp))
            args = run_single_job_live.parse_args(["start", "--config", "search_one"])
            active = SimpleNamespace(state={"run_id": "run-active", "status": "source_complete"})
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "load_active_workflow_run", return_value=active):
                    with patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync:
                        with self.assertRaisesRegex(ValueError, "已有活动运行"):
                            run_single_job_live.start_command(args, input_fn=lambda _prompt: "")
            auth_sync.assert_not_called()

    def test_start_freezes_config_search_and_rubric_in_a_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp))
            args = run_single_job_live.parse_args(["start", "--config", "search_one"])
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", Path(tmp) / "auth"):
                    result = run_single_job_live.start_command(
                        args,
                        input_fn=lambda _prompt: "确认来源3次",
                        output_fn=lambda _line: None,
                        now=lambda: datetime(
                            2026, 9, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                        ),
                        after_confirmation=lambda preparation, current: (
                            run_single_job_live._freeze_source_run(preparation, current)
                            and 0
                        ),
                    )

            config_path = paths.config_root / "search_one.json"
            config_path.write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 1,
                        "top_priority_search_query_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            state_before = load_active_workflow_run(paths).state
            source_plan_path = paths.runs_root / state_before["run_id"] / "source_plan.json"
            frozen_plan = json.loads(source_plan_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(state_before["config"]["top_priority_search_query_count"], 1)
            self.assertEqual(frozen_plan["selected_search_routes"][0]["query"], "汽配 平台招商")

            drifted = CandidateInventory.load(paths.inventory_path)
            artifacts = drifted.to_dict()["job_artifacts"]["job-1"]
            changed_search_plan = dict(artifacts["search_plan"])
            changed_search_plan["routes"] = [{"id": "route-1", "query": "changed"}]
            drifted.record_job_artifacts(
                "job-1",
                artifacts["source_jd_hash"],
                rubric=artifacts["rubric"],
                search_plan=changed_search_plan,
            )
            drifted.save(paths.inventory_path)
            self.assertEqual(
                load_active_workflow_run(paths).state["search_plan_digest"],
                state_before["search_plan_digest"],
            )
            self.assertNotEqual(state_before["search_plan_digest"], content_hash(changed_search_plan))

    def test_start_issues_consumes_and_executes_source_only_after_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_start_inputs(Path(tmp))
            args = run_single_job_live.parse_args(["start", "--config", "search_one"])
            events: list[str] = []
            issued = SimpleNamespace(authorization_id="authorization-source")
            store = SimpleNamespace(
                issue=lambda **_kwargs: events.append("issue") or issued,
                consume=lambda **_kwargs: events.append("consume") or SimpleNamespace(
                    authorization_id="authorization-source"
                ),
            )
            real_load_active = run_single_job_live.load_active_workflow_run

            def tracked_load(active_paths):
                loaded = real_load_active(active_paths)
                if loaded is not None:
                    events.append("reload")
                return loaded

            def execute(**kwargs):
                events.append("client")
                work_dir = kwargs["work_dir"]
                work_dir.mkdir(parents=True)
                artifact = {
                    "schema_version": 1,
                    "contract": "single_job_source_collection",
                    "plan_id": kwargs["plan"]["plan_id"],
                    "candidate_count": 2,
                }
                artifact_path = work_dir / "source_collection.json"
                artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
                return {
                    "candidate_cards": 2,
                    "artifact": artifact,
                    "artifact_path": artifact_path,
                }

            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", Path(tmp) / "auth"):
                    with patch.object(run_single_job_live, "FIXED_GUARD_DIR", Path(tmp) / "guard"):
                        with patch.object(
                            run_single_job_live,
                            "preflight_boss_access",
                            return_value=SimpleNamespace(account_key="0123456789abcdef"),
                        ):
                            with patch.object(
                                run_single_job_live,
                                "sync_auth_from_chrome",
                                side_effect=lambda _path: events.append("auth_sync")
                                or {"session_fingerprint": "session-a"},
                            ):
                                with patch.object(
                                    run_single_job_live,
                                    "LiveAuthorizationStore",
                                    return_value=store,
                                ):
                                    with patch.object(
                                        run_single_job_live,
                                        "load_active_workflow_run",
                                        side_effect=tracked_load,
                                    ):
                                        with patch.object(
                                            run_single_job_live,
                                            "_execute_read_live_plan",
                                            side_effect=execute,
                                        ):
                                            with redirect_stdout(StringIO()):
                                                result = run_single_job_live.start_command(
                                                    args,
                                                    input_fn=lambda _prompt: events.append("confirm")
                                                    or "确认来源3次",
                                                    output_fn=lambda _line: None,
                                                    now=lambda: datetime(
                                                        2026,
                                                        9,
                                                        7,
                                                        10,
                                                        0,
                                                        tzinfo=ZoneInfo("Asia/Shanghai"),
                                                    ),
                                                )

            active = load_active_workflow_run(paths)
            self.assertEqual(result, 0)
            self.assertEqual(events, ["confirm", "auth_sync", "issue", "reload", "consume", "client"])
            self.assertEqual(active.state["status"], "source_complete")
            self.assertEqual(active.state["next_action"], "candidate_details")
            self.assertIn("source_collection", active.state["artifacts"])
            self.assertIn("source_receipt", active.state["artifacts"])

    def test_continue_details_confirms_then_issues_consumes_and_updates_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(root)
            args = run_single_job_live.parse_args(["continue", "--select", "1"])
            events: list[str] = []
            issued = SimpleNamespace(authorization_id="authorization-detail")
            store = SimpleNamespace(
                issue=lambda **_kwargs: events.append("issue") or issued,
                consume=lambda **_kwargs: events.append("consume") or SimpleNamespace(),
            )
            real_verify = run_single_job_live.verify_frozen_detail_plan

            def verify(*verify_args, **verify_kwargs):
                events.append("verify")
                return real_verify(*verify_args, **verify_kwargs)

            def execute(**kwargs):
                events.append("client")
                inventory = CandidateInventory.load(kwargs["inventory_path"])
                candidate_id = kwargs["plan"]["candidates"][0]["candidate_id"]
                inventory.ensure_resume(candidate_id, lambda: {"work_experience": []})
                inventory.save(kwargs["inventory_path"])
                work_dir = kwargs["work_dir"]
                work_dir.mkdir(parents=True)
                artifact = {
                    "schema_version": 1,
                    "contract": "candidate_detail_collection",
                    "plan_id": kwargs["plan"]["plan_id"],
                    "detail_request_count": 1,
                    "cached_resume_count": 0,
                    "remaining_pending_count": 1,
                }
                artifact_path = work_dir / "detail_collection.json"
                artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
                return {"artifact": artifact, "artifact_path": artifact_path}

            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                    with patch.object(run_single_job_live, "FIXED_GUARD_DIR", root / "guard"):
                        with patch.object(
                            run_single_job_live,
                            "preflight_boss_access",
                            return_value=SimpleNamespace(account_key=account_key_for(auth_dir)),
                        ):
                            with patch.object(
                                run_single_job_live,
                                "sync_auth_from_chrome",
                                side_effect=lambda _path: events.append("auth_sync")
                                or {"session_fingerprint": "session-a"},
                            ):
                                with patch.object(
                                    run_single_job_live,
                                    "LiveAuthorizationStore",
                                    return_value=store,
                                ):
                                    with patch.object(
                                        run_single_job_live,
                                        "verify_frozen_detail_plan",
                                        side_effect=verify,
                                    ):
                                        with patch.object(
                                            run_single_job_live,
                                            "_execute_read_live_plan",
                                            side_effect=execute,
                                        ):
                                            result = run_single_job_live.continue_command(
                                                args,
                                                input_fn=lambda _prompt: events.append("confirm")
                                                or "确认详情1人",
                                                output_fn=lambda _line: None,
                                                now=lambda: datetime(
                                                    2026,
                                                    9,
                                                    7,
                                                    10,
                                                    2,
                                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                                ),
                                            )

            active = load_active_workflow_run(paths)
            inventory = CandidateInventory.load(paths.inventory_path)
            self.assertEqual(result, 0)
            self.assertEqual(
                events,
                ["confirm", "verify", "auth_sync", "issue", "verify", "consume", "client"],
            )
            self.assertEqual(active.state["status"], "detail_complete")
            self.assertIsNotNone(inventory.get_resume("candidate-1"))
            self.assertEqual(len(inventory.list_resume_pending(job_id="job-1")), 1)

    def test_continue_detail_wrong_confirmation_stops_before_auth_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(root, pending_count=1)
            args = run_single_job_live.parse_args(["continue"])
            with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                    with patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync:
                        with self.assertRaisesRegex(BossLiveAccessDenied, "详情确认文本不匹配"):
                            run_single_job_live.continue_command(
                                args,
                                input_fn=lambda _prompt: "不确认",
                                output_fn=lambda _line: None,
                                now=lambda: datetime(
                                    2026,
                                    9,
                                    7,
                                    10,
                                    2,
                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                ),
                            )
            auth_sync.assert_not_called()

    def test_continue_scoring_reuses_valid_results_and_calls_only_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(
                    root,
                    pending_count=3,
                    rubric=LOCAL_SCORING_RUBRIC,
                )
            inventory = CandidateInventory.load(paths.inventory_path)
            for index in range(1, 4):
                inventory.ensure_resume(
                    f"candidate-{index}",
                    lambda: {
                        "work_experience": [
                            {
                                "position": "平台招商负责人",
                                "responsibility": "负责重点商家拓展",
                            }
                        ]
                    },
                )
            existing_evaluation = evaluation("candidate-1", 88)
            existing_evaluation["rubric_version"] = LOCAL_SCORING_RUBRIC["version"]
            inventory.record_evaluation("job-1", "candidate-1", existing_evaluation)
            inventory.save(paths.inventory_path)
            active = load_active_workflow_run(paths)
            detail_complete = transition_workflow_run_state(
                active.state,
                status="detail_complete",
                updated_at="2026-09-07T10:02:00+08:00",
            )
            save_active_workflow_run(
                paths,
                detail_complete,
                expected_state_digest=active.state_digest,
            )
            args = run_single_job_live.parse_args(["continue"])
            llm = FakeEvaluationLlm()
            output: list[str] = []
            with patch.dict(
                os.environ,
                {
                    "OPENAI_BASE_URL": "https://llm.invalid/v1",
                    "OPENAI_API_KEY": "secret-for-test",
                    "OPENAI_MODEL": llm.model,
                    "BOSS_HIRE_LLM_WORKERS": "1",
                },
                clear=True,
            ):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(
                        run_single_job_live,
                        "OpenAICompatibleJsonLlm",
                        return_value=llm,
                    ) as llm_class:
                        with patch.object(
                            run_single_job_live,
                            "_execute_read_live_plan",
                        ) as boss_execute:
                            result = run_single_job_live.continue_command(
                                args,
                                input_fn=lambda _prompt: "确认评分2人",
                                output_fn=output.append,
                                now=lambda: datetime(
                                    2026,
                                    9,
                                    7,
                                    10,
                                    3,
                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                ),
                            )

            restored = CandidateInventory.load(paths.inventory_path)
            final_run = load_active_workflow_run(paths)
            self.assertEqual(result, 0)
            self.assertEqual(llm.calls, ["candidate-2", "candidate-3"])
            self.assertEqual(restored.evaluated_count("job-1", rubric_version=LOCAL_SCORING_RUBRIC["version"]), 3)
            self.assertEqual(final_run.state["status"], "scoring_complete")
            self.assertIn("llm_confirmation_receipt", final_run.state["artifacts"])
            self.assertIn("score_summary", final_run.state["artifacts"])
            llm_class.assert_called_once()
            boss_execute.assert_not_called()
            self.assertIn("复用 1 人，新增 2 人", output[0])

    def test_continue_scoring_wrong_confirmation_creates_no_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(
                    root,
                    pending_count=1,
                    rubric=LOCAL_SCORING_RUBRIC,
                )
            inventory = CandidateInventory.load(paths.inventory_path)
            inventory.ensure_resume("candidate-1", lambda: {"work_experience": []})
            inventory.save(paths.inventory_path)
            active = load_active_workflow_run(paths)
            save_active_workflow_run(
                paths,
                transition_workflow_run_state(
                    active.state,
                    status="detail_complete",
                    updated_at="2026-09-07T10:02:00+08:00",
                ),
                expected_state_digest=active.state_digest,
            )
            args = run_single_job_live.parse_args(["continue"])
            with patch.dict(
                os.environ,
                {
                    "OPENAI_BASE_URL": "https://llm.invalid/v1",
                    "OPENAI_API_KEY": "secret-for-test",
                    "OPENAI_MODEL": "fake-local-model",
                },
                clear=True,
            ):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(run_single_job_live, "OpenAICompatibleJsonLlm") as llm_class:
                        with self.assertRaisesRegex(ValueError, "评分确认文本不匹配"):
                            run_single_job_live.continue_command(
                                args,
                                input_fn=lambda _prompt: "不确认",
                                output_fn=lambda _line: None,
                                now=lambda: datetime(
                                    2026,
                                    9,
                                    7,
                                    10,
                                    3,
                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                ),
                            )
            llm_class.assert_not_called()

    def test_continue_scoring_partial_failure_preserves_results_and_retries_only_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(
                    root,
                    pending_count=3,
                    rubric=LOCAL_SCORING_RUBRIC,
                )
            inventory = CandidateInventory.load(paths.inventory_path)
            for index in range(1, 4):
                inventory.ensure_resume(
                    f"candidate-{index}",
                    lambda: {
                        "work_experience": [
                            {"position": "平台招商负责人", "responsibility": "负责重点商家拓展"}
                        ]
                    },
                )
            inventory.save(paths.inventory_path)
            active = load_active_workflow_run(paths)
            save_active_workflow_run(
                paths,
                transition_workflow_run_state(
                    active.state,
                    status="detail_complete",
                    updated_at="2026-09-07T10:02:00+08:00",
                ),
                expected_state_digest=active.state_digest,
            )
            args = run_single_job_live.parse_args(["continue"])
            failing_llm = FakeEvaluationLlm(fail_candidate_id="candidate-2")
            settings = {
                "OPENAI_BASE_URL": "https://llm.invalid/v1",
                "OPENAI_API_KEY": "secret-for-test",
                "OPENAI_MODEL": failing_llm.model,
                "BOSS_HIRE_LLM_WORKERS": "1",
            }
            with patch.dict(os.environ, settings, clear=True):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(
                        run_single_job_live,
                        "OpenAICompatibleJsonLlm",
                        return_value=failing_llm,
                    ):
                        first = run_single_job_live.continue_command(
                            args,
                            input_fn=lambda _prompt: "确认评分3人",
                            output_fn=lambda _line: None,
                            now=lambda: datetime(
                                2026, 9, 7, 10, 3, tzinfo=ZoneInfo("Asia/Shanghai")
                            ),
                        )

            after_first = load_active_workflow_run(paths)
            self.assertEqual(first, 2)
            self.assertEqual(failing_llm.calls, ["candidate-1", "candidate-2", "candidate-3"])
            self.assertEqual(after_first.state["status"], "detail_complete")
            self.assertIn("failed=1 remaining=1", after_first.state["last_error"])
            self.assertEqual(
                [
                    row["candidate_id"]
                    for row in CandidateInventory.load(paths.inventory_path).list_score_pending(
                        job_id="job-1",
                        rubric_version=LOCAL_SCORING_RUBRIC["version"],
                    )
                ],
                ["candidate-2"],
            )

            retry_llm = FakeEvaluationLlm()
            settings["OPENAI_MODEL"] = retry_llm.model
            with patch.dict(os.environ, settings, clear=True):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(
                        run_single_job_live,
                        "OpenAICompatibleJsonLlm",
                        return_value=retry_llm,
                    ):
                        second = run_single_job_live.continue_command(
                            args,
                            input_fn=lambda _prompt: "确认评分1人",
                            output_fn=lambda _line: None,
                            now=lambda: datetime(
                                2026, 9, 7, 10, 4, tzinfo=ZoneInfo("Asia/Shanghai")
                            ),
                        )

            self.assertEqual(second, 0)
            self.assertEqual(retry_llm.calls, ["candidate-2"])
            self.assertEqual(load_active_workflow_run(paths).state["status"], "scoring_complete")

    def test_continue_scoring_429_stops_dispatch_and_remains_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(
                    root,
                    pending_count=3,
                    rubric=LOCAL_SCORING_RUBRIC,
                )
            inventory = CandidateInventory.load(paths.inventory_path)
            for index in range(1, 4):
                inventory.ensure_resume(
                    f"candidate-{index}",
                    lambda: {"work_experience": []},
                )
            inventory.save(paths.inventory_path)
            active = load_active_workflow_run(paths)
            save_active_workflow_run(
                paths,
                transition_workflow_run_state(
                    active.state,
                    status="detail_complete",
                    updated_at="2026-09-07T10:02:00+08:00",
                ),
                expected_state_digest=active.state_digest,
            )
            args = run_single_job_live.parse_args(["continue"])
            llm = FakeEvaluationLlm(
                fail_candidate_id="candidate-1",
                failure_message="LLM candidate_evaluation HTTP 429",
            )
            with patch.dict(
                os.environ,
                {
                    "OPENAI_BASE_URL": "https://llm.invalid/v1",
                    "OPENAI_API_KEY": "secret-for-test",
                    "OPENAI_MODEL": llm.model,
                    "BOSS_HIRE_LLM_WORKERS": "1",
                },
                clear=True,
            ):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(
                        run_single_job_live,
                        "OpenAICompatibleJsonLlm",
                        return_value=llm,
                    ):
                        result = run_single_job_live.continue_command(
                            args,
                            input_fn=lambda _prompt: "确认评分3人",
                            output_fn=lambda _line: None,
                            now=lambda: datetime(
                                2026, 9, 7, 10, 3, tzinfo=ZoneInfo("Asia/Shanghai")
                            ),
                        )

            current = load_active_workflow_run(paths)
            self.assertEqual(result, 2)
            self.assertEqual(llm.calls, ["candidate-1"])
            self.assertEqual(current.state["status"], "detail_complete")
            self.assertIn("backpressure=True", current.state["last_error"])

    def test_continue_scoring_interruption_keeps_checkpointed_scores_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                paths = self._write_detail_continue_inputs(
                    root,
                    pending_count=2,
                    rubric=LOCAL_SCORING_RUBRIC,
                )
            inventory = CandidateInventory.load(paths.inventory_path)
            for index in range(1, 3):
                inventory.ensure_resume(f"candidate-{index}", lambda: {"work_experience": []})
            inventory.save(paths.inventory_path)
            active = load_active_workflow_run(paths)
            save_active_workflow_run(
                paths,
                transition_workflow_run_state(
                    active.state,
                    status="detail_complete",
                    updated_at="2026-09-07T10:02:00+08:00",
                ),
                expected_state_digest=active.state_digest,
            )

            def interrupted_score(**_kwargs):
                checkpoint = CandidateInventory.load(paths.inventory_path)
                saved_evaluation = evaluation("candidate-1", 88)
                saved_evaluation["rubric_version"] = LOCAL_SCORING_RUBRIC["version"]
                checkpoint.record_evaluation("job-1", "candidate-1", saved_evaluation)
                checkpoint.save(paths.inventory_path)
                raise RuntimeError("simulated process interruption")

            args = run_single_job_live.parse_args(["continue"])
            with patch.dict(
                os.environ,
                {
                    "OPENAI_BASE_URL": "https://llm.invalid/v1",
                    "OPENAI_API_KEY": "secret-for-test",
                    "OPENAI_MODEL": "fake-local-model",
                    "BOSS_HIRE_LLM_WORKERS": "1",
                },
                clear=True,
            ):
                with patch.object(run_single_job_live, "workflow_paths", return_value=paths):
                    with patch.object(
                        run_single_job_live,
                        "OpenAICompatibleJsonLlm",
                        return_value=FakeEvaluationLlm(),
                    ):
                        with patch.object(
                            run_single_job_live,
                            "score_inventory_resumes",
                            side_effect=interrupted_score,
                        ):
                            with self.assertRaisesRegex(RuntimeError, "process interruption"):
                                run_single_job_live.continue_command(
                                    args,
                                    input_fn=lambda _prompt: "确认评分2人",
                                    output_fn=lambda _line: None,
                                    now=lambda: datetime(
                                        2026, 9, 7, 10, 3, tzinfo=ZoneInfo("Asia/Shanghai")
                                    ),
                                )

            current = load_active_workflow_run(paths)
            pending = CandidateInventory.load(paths.inventory_path).list_score_pending(
                job_id="job-1",
                rubric_version=LOCAL_SCORING_RUBRIC["version"],
            )
            self.assertEqual(current.state["status"], "detail_complete")
            self.assertIn("llm_confirmation_receipt", current.state["artifacts"])
            self.assertEqual([row["candidate_id"] for row in pending], ["candidate-2"])

    def test_configs_lists_valid_and_invalid_fixed_configs_without_external_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "data/local/run_configs"
            config_root.mkdir(parents=True)
            (config_root / "search_top5.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 0,
                        "top_priority_search_query_count": 5,
                    }
                ),
                encoding="utf-8",
            )
            (config_root / "invalid.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 1,
                        "top_priority_search_query_count": 0,
                        "work_dir": "/tmp/other",
                    }
                ),
                encoding="utf-8",
            )
            output: list[str] = []
            args = run_single_job_live.parse_args(["configs", "--json"])
            with patch.object(
                run_single_job_live,
                "workflow_paths",
                return_value=SimpleNamespace(config_root=config_root),
            ):
                with patch.object(run_single_job_live, "account_key_for") as account_key:
                    with patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync:
                        with patch.object(run_single_job_live, "LiveAuthorizationStore") as auth_store:
                            result = run_single_job_live.configs_command(args, output_fn=output.append)

        payload = json.loads(output[0])
        self.assertEqual(result, 0)
        self.assertEqual(payload["contract"], "boss_hire_run_configs")
        self.assertEqual([row["name"] for row in payload["configs"]], ["invalid", "search_top5"])
        self.assertFalse(payload["configs"][0]["valid"])
        self.assertEqual(payload["configs"][1]["source_summary"]["search_query_count"], 5)
        account_key.assert_not_called()
        auth_sync.assert_not_called()
        auth_store.assert_not_called()

    def test_configs_human_output_is_readable_without_jq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root = Path(tmp)
            (config_root / "recommendation.json").write_text(
                json.dumps(
                    {
                        "recommendation_source_enabled": 1,
                        "top_priority_search_query_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            output: list[str] = []
            args = run_single_job_live.parse_args(["configs"])
            with patch.object(
                run_single_job_live,
                "workflow_paths",
                return_value=SimpleNamespace(config_root=config_root),
            ):
                run_single_job_live.configs_command(args, output_fn=output.append)

        rendered = "\n".join(output)
        self.assertIn("recommendation：推荐 开，优先搜索词 0 个", rendered)
        self.assertIn("start --config <名称>", rendered)

    def test_status_outputs_exact_start_hint_without_an_active_run(self) -> None:
        args = run_single_job_live.parse_args(["status"])
        output: list[str] = []
        snapshot = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_status",
            "run_id": None,
            "status": "no_active_run",
            "next_action": "start",
            "next_command": ".venv/bin/python scripts/run_single_job_live.py start --config <名称>",
            "funnel": None,
            "artifacts": {},
            "blocker": None,
            "boss_requests": 0,
        }
        with patch.object(run_single_job_live, "build_workflow_status", return_value=snapshot):
            with patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync:
                result = run_single_job_live.status_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        self.assertIn("当前没有活动运行", output[0])
        self.assertIn("start --config <名称>", output[1])
        auth_sync.assert_not_called()

    def test_status_advertises_only_the_current_run_report_before_scoring_completes(self) -> None:
        args = run_single_job_live.parse_args(["status"])
        output: list[str] = []
        snapshot = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_status",
            "run_id": "run-one",
            "status": "source_complete",
            "next_action": "candidate_details",
            "next_command": ".venv/bin/python scripts/run_single_job_live.py continue",
            "config_name": "search_top5",
            "job_id": "job-open",
            "rubric_version": "rubric-v1",
            "funnel": {
                "candidate_card_count": 10,
                "pending_detail_count": 3,
                "pending_score_count": 2,
                "valid_score_count": 5,
            },
            "artifacts": {"source_collection": {"path": "source/source_collection.json"}},
            "blocker": None,
            "boss_requests": 0,
        }
        with patch.object(run_single_job_live, "build_workflow_status", return_value=snapshot):
            result = run_single_job_live.status_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        hints = "\n".join(output)
        self.assertIn("run_single_job_live.py report", hints)
        self.assertNotIn("run_single_job_live.py pool", hints)
        self.assertNotIn("show_single_job_report.py", hints)

    def test_status_advertises_report_and_pool_after_scoring_completes(self) -> None:
        args = run_single_job_live.parse_args(["status"])
        output: list[str] = []
        snapshot = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_status",
            "run_id": "run-one",
            "status": "scoring_complete",
            "next_action": None,
            "next_command": None,
            "config_name": "search_top5",
            "job_id": "job-open",
            "rubric_version": "rubric-v1",
            "funnel": {
                "candidate_card_count": 10,
                "pending_detail_count": 0,
                "pending_score_count": 0,
                "valid_score_count": 5,
            },
            "artifacts": {"source_collection": {"path": "source/source_collection.json"}},
            "blocker": None,
            "boss_requests": 0,
        }
        with patch.object(run_single_job_live, "build_workflow_status", return_value=snapshot):
            result = run_single_job_live.status_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        hints = "\n".join(output)
        self.assertIn("run_single_job_live.py report", hints)
        self.assertIn("run_single_job_live.py pool", hints)
        self.assertNotIn("show_single_job_report.py", hints)

    def test_pool_shows_active_job_rubric_candidates_from_local_report_only(self) -> None:
        args = run_single_job_live.parse_args(["pool", "--favorited-only", "--top", "1"])
        output: list[str] = []
        paths = object()
        active = SimpleNamespace(state={"run_id": "run-one"})
        report = {
            "run_id": "run-one",
            "candidate_scope": "same_job_rubric_pool",
            "favorite_status_source": {
                "kind": "local_registry_only",
                "boss_sync_performed": False,
            },
            "boss_requests": 0,
            "llm_requests": 0,
            "candidates": [
                {"candidate_id": "not-favorite", "total_score": 90, "local_favorite_status": "not_recorded_local"},
                {"candidate_id": "favorite-a", "total_score": 80, "local_favorite_status": "favorited_local"},
                {"candidate_id": "favorite-b", "total_score": 70, "local_favorite_status": "favorited_local"},
            ],
        }
        with patch.object(run_single_job_live, "workflow_paths", return_value=paths), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=active
        ), patch.object(run_single_job_live, "build_single_job_report", return_value=report) as build, patch.object(
            run_single_job_live, "format_single_job_report", return_value="评分详情"
        ) as format_report, patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync, patch.object(
            run_single_job_live, "OpenAICompatibleJsonLlm"
        ) as llm:
            result = run_single_job_live.pool_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            paths=paths,
            run_id="run-one",
            candidate_scope="pool",
            include_candidates=True,
        )
        self.assertEqual(output[0], "评分详情")
        self.assertIn("本地收藏注册表", output[1])
        self.assertIn("未同步或查询 BOSS", output[1])
        self.assertEqual(output[2], "筛选：仅本地已收藏；实际显示 1 人。")
        self.assertEqual(
            format_report.call_args.args[0]["candidates"],
            [{"candidate_id": "favorite-a", "total_score": 80, "local_favorite_status": "favorited_local"}],
        )
        self.assertEqual(
            format_report.call_args.args[0]["candidate_selection"],
            {"top": 1, "all": False, "favorited_only": True, "matched_count": 1},
        )
        auth_sync.assert_not_called()
        llm.assert_not_called()

    def test_pool_json_uses_active_run_and_preserves_local_only_contract(self) -> None:
        args = run_single_job_live.parse_args(["pool", "--json"])
        output: list[str] = []
        report = {
            "run_id": "run-one",
            "candidate_scope": "same_job_rubric_pool",
            "favorite_status_source": {
                "kind": "local_registry_only",
                "boss_sync_performed": False,
            },
            "boss_requests": 0,
            "llm_requests": 0,
            "candidates": [
                {"candidate_id": "favorite-a", "total_score": 80, "local_favorite_status": "favorited_local"},
                {"candidate_id": "other", "total_score": 70, "local_favorite_status": "not_recorded_local"},
            ],
        }
        with patch.object(run_single_job_live, "workflow_paths", return_value=object()), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=SimpleNamespace(state={"run_id": "run-one"})
        ), patch.object(run_single_job_live, "build_single_job_report", return_value=report):
            result = run_single_job_live.pool_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output[0]),
            {
                **report,
                "candidate_selection": {"top": 20, "all": False, "favorited_only": False, "matched_count": 2},
                "candidate_heading": "同 JD/rubric 总池 Top20：",
            },
        )

    def test_pool_rejects_nonpositive_top(self) -> None:
        with self.assertRaises(SystemExit):
            run_single_job_live.parse_args(["pool", "--top", "0"])

    def test_pool_all_is_mutually_exclusive_with_top(self) -> None:
        with self.assertRaises(SystemExit):
            run_single_job_live.parse_args(["pool", "--all", "--top", "10"])

    def test_pool_all_keeps_every_candidate_after_local_favorite_filter(self) -> None:
        args = run_single_job_live.parse_args(["pool", "--all", "--favorited-only"])
        output: list[str] = []
        report = {
            "run_id": "run-one",
            "candidate_scope": "same_job_rubric_pool",
            "candidates": [
                {"candidate_id": "favorite-a", "total_score": 80, "local_favorite_status": "favorited_local"},
                {"candidate_id": "favorite-b", "total_score": 70, "local_favorite_status": "favorited_local"},
                {"candidate_id": "other", "total_score": 60, "local_favorite_status": "not_recorded_local"},
            ],
        }
        with patch.object(run_single_job_live, "workflow_paths", return_value=object()), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=SimpleNamespace(state={"run_id": "run-one"})
        ), patch.object(run_single_job_live, "build_single_job_report", return_value=report), patch.object(
            run_single_job_live, "format_single_job_report", return_value="候选池"
        ) as format_report:
            result = run_single_job_live.pool_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        self.assertEqual(
            [row["candidate_id"] for row in format_report.call_args.args[0]["candidates"]],
            ["favorite-a", "favorite-b"],
        )
        self.assertEqual(
            format_report.call_args.args[0]["candidate_selection"],
            {"top": None, "all": True, "favorited_only": True, "matched_count": 2},
        )
        self.assertEqual(output[2], "筛选：仅本地已收藏；实际显示 2 人。")

    def test_pool_requires_an_active_run(self) -> None:
        args = run_single_job_live.parse_args(["pool"])
        with patch.object(run_single_job_live, "workflow_paths", return_value=object()), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=None
        ):
            with self.assertRaisesRegex(ValueError, "当前没有活动岗位"):
                run_single_job_live.pool_command(args)

    def test_report_shows_active_run_funnel_and_score_details_without_external_clients(self) -> None:
        args = run_single_job_live.parse_args(["report", "--scores", "--top", "3"])
        output: list[str] = []
        paths = object()
        active = SimpleNamespace(state={"run_id": "run-one"})
        report = {"run_id": "run-one", "candidate_scope": "new_scores", "candidates": []}
        with patch.object(run_single_job_live, "workflow_paths", return_value=paths), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=active
        ), patch.object(run_single_job_live, "build_single_job_report", return_value=report) as build, patch.object(
            run_single_job_live, "format_single_job_report", return_value="本轮评分"
        ) as format_report, patch.object(run_single_job_live, "sync_auth_from_chrome") as auth_sync, patch.object(
            run_single_job_live, "OpenAICompatibleJsonLlm"
        ) as llm:
            result = run_single_job_live.report_command(args, output_fn=output.append)

        self.assertEqual(result, 0)
        build.assert_called_once_with(
            paths=paths,
            run_id="run-one",
            candidates=3,
            candidate_scope="new",
            include_candidates=True,
        )
        self.assertEqual(format_report.call_args.kwargs["section"], "scores")
        self.assertEqual(format_report.call_args.args[0]["candidate_heading"], "本轮新评分 Top3：")
        self.assertEqual(output, ["本轮评分"])
        auth_sync.assert_not_called()
        llm.assert_not_called()

    def test_report_requires_an_active_run(self) -> None:
        args = run_single_job_live.parse_args(["report"])
        with patch.object(run_single_job_live, "workflow_paths", return_value=object()), patch.object(
            run_single_job_live, "load_active_workflow_run", return_value=None
        ):
            with self.assertRaisesRegex(ValueError, "当前没有活动岗位"):
                run_single_job_live.report_command(args)

    def test_status_json_preserves_blocker_funnel_and_next_command(self) -> None:
        args = run_single_job_live.parse_args(["status", "--run", "run-one", "--json"])
        output: list[str] = []
        snapshot = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_status",
            "run_id": "run-one",
            "status": "blocked",
            "next_action": "resolve_blocker",
            "next_command": ".venv/bin/python scripts/run_single_job_live.py continue",
            "config_name": "search_top5",
            "board_date": "2026-09-07",
            "job_id": "job-open",
            "rubric_version": "rubric-v1",
            "funnel": {
                "candidate_card_count": 10,
                "pending_detail_count": 3,
                "pending_score_count": 2,
                "valid_score_count": 5,
                "undelivered_score_count": 5,
                "delivered_score_count": 0,
            },
            "artifacts": {},
            "blocker": "risk_stopped",
            "boss_requests": 0,
        }
        with patch.object(run_single_job_live, "build_workflow_status", return_value=snapshot) as build:
            result = run_single_job_live.status_command(args, output_fn=output.append)

        payload = json.loads(output[0])
        self.assertEqual(result, 0)
        self.assertEqual(payload["blocker"], "risk_stopped")
        self.assertEqual(payload["funnel"]["pending_detail_count"], 3)
        self.assertEqual(payload["boss_requests"], 0)
        build.assert_called_once()
        self.assertEqual(build.call_args.kwargs["run_id"], "run-one")

    def test_bound_search_plan_uses_one_current_persisted_route(self) -> None:
        config_value = {
            "recommendation_source_enabled": 0,
            "top_priority_search_query_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config_value), encoding="utf-8")
            work_dir = root / "runs"
            work_dir.mkdir()
            inventory = {
                "schema_version": 1,
                "candidates": {},
                "evaluations": {},
                "displayed": {},
                "job_artifacts": {
                    "job-open": {
                        "source_jd_hash": "jd-current",
                        "rubric": {},
                        "search_plan": {
                            "contract": "generic_search_plan",
                            "version": "search-current",
                            "source_jd_hash": "jd-current",
                            "routes": [{"id": "title", "query": "平台招商负责人"}],
                        },
                    }
                },
            }
            (work_dir / "candidate_inventory.json").write_text(
                json.dumps(inventory),
                encoding="utf-8",
            )
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", root / "auth"):
                plan = run_single_job_live._bound_plan(
                    config_path=config_path,
                    work_dir=work_dir,
                    board_date="2026-09-01",
                )

        self.assertEqual(plan["recommendation_source_enabled"], 0)
        self.assertEqual(plan["search_job_id"], "job-open")
        self.assertEqual(plan["selected_search_routes"][0]["query"], "平台招商负责人")
        self.assertEqual(plan["search_plan_version"], "search-current")

    def test_search_plan_binding_fails_closed_when_inventory_is_missing(self) -> None:
        config_value = {
            "recommendation_source_enabled": 0,
            "top_priority_search_query_count": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config_value), encoding="utf-8")
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", root / "auth"):
                with self.assertRaisesRegex(ValueError, "持久化搜索计划"):
                    run_single_job_live._bound_plan(
                        config_path=config_path,
                        work_dir=root / "runs",
                        board_date="2026-09-01",
                    )

    def test_authorize_output_exposes_exact_candidate_source_binding(self) -> None:
        plan = {
            "plan_id": "single-job-test",
            "board_date": "2026-09-01",
            "recommendation_source_enabled": 0,
            "top_priority_search_query_count": 1,
            "selected_search_query_count": 1,
            "search_query_shortfall": 0,
            "search_plan_version": "search-current",
            "operation_manifest": [
                {
                    "operation_key": "source:search:title:page:1",
                    "request_class": "list",
                    "method": "GET",
                    "endpoint_name": "search_geeks",
                    "binding": {
                        "route_id": "title",
                        "query": "平台招商负责人",
                        "job_id": "job-open",
                        "page": 1,
                    },
                }
            ],
        }
        args = SimpleNamespace(
            config=Path("config.json"),
            work_dir=Path("runs"),
            confirm_live="2026-09-01",
            note="人工确认",
        )
        receipt = SimpleNamespace(
            authorization_id="authorization-1",
            status="READY",
            plan_id="single-job-test",
            local_date="2026-09-01",
            operation_count=1,
        )
        output = StringIO()
        with patch.object(run_single_job_live, "_bound_plan", return_value=plan):
            with patch.object(run_single_job_live, "preflight_boss_access") as preflight:
                preflight.return_value.summary.return_value = "mode=live"
                with patch.object(
                    run_single_job_live,
                    "sync_auth_from_chrome",
                    return_value={
                        "session_fingerprint": "session-a",
                        "source": "chrome",
                        "changed": False,
                        "cookie_count": 3,
                    },
                ):
                    with patch.object(run_single_job_live, "LiveAuthorizationStore") as store:
                        store.return_value.issue.return_value = receipt
                        with redirect_stdout(output):
                            result = run_single_job_live.authorize_command(
                                args,
                                now=lambda: datetime(
                                    2026,
                                    9,
                                    1,
                                    10,
                                    0,
                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                ),
                            )

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["recommendation_source_enabled"], 0)
        self.assertEqual(payload["requested_search_queries"], 1)
        self.assertEqual(payload["selected_search_queries"], 1)
        self.assertEqual(payload["search_plan_version"], "search-current")

    def test_recommendation_source_reads_exactly_one_page_and_ignores_has_more(self) -> None:
        cases = ([], [card("rec-1", "推荐甲")], [card("rec-1", "推荐甲"), card("rec-2", "推荐乙")])
        for rows in cases:
            with self.subTest(row_count=len(rows)):
                client = RouteClient()
                response = {"code": 0, "zpData": {"geekList": rows, "hasMore": True}}
                with patch.object(client, "_request", return_value=response) as request:
                    cards, sources, routes = collect_single_job_cards(
                        client,
                        job_id="job-open",
                        recommendation_source_enabled=1,
                        search_routes=[],
                    )

                self.assertEqual([row.encrypt_geek_id for row in cards], [row["geekCard"]["encryptGeekId"] for row in rows])
                request.assert_called_once()
                self.assertEqual(client.search_calls, [])
                self.assertEqual(set(sources), {row.encrypt_geek_id for row in cards})
                self.assertTrue(all(value == {"recommendation"} for value in sources.values()))
                self.assertEqual(routes, {})

    def test_search_source_reads_exactly_one_route_page(self) -> None:
        client = RouteClient()
        cards, sources, routes = collect_single_job_cards(
            client,
            job_id="job-open",
            recommendation_source_enabled=0,
            search_routes=[{"id": "title", "query": "平台招商负责人"}],
        )

        self.assertEqual([row.encrypt_geek_id for row in cards], ["title-1", "title-2"])
        self.assertEqual(client.recommendation_calls, 0)
        self.assertEqual(client.search_calls, ["平台招商负责人"])
        self.assertEqual(sources["title-1"], {"search"})
        self.assertEqual(routes["title-1"], {"title"})

    def test_search_second_page_is_route_major_and_dedupes_across_pages(self) -> None:
        client = PageRouteClient()
        cards, sources, routes = collect_single_job_cards(
            client,
            job_id="job-open",
            recommendation_source_enabled=0,
            search_routes=[
                {"id": "title", "query": "平台招商负责人"},
                {"id": "merchant", "query": "重点商家拓展"},
            ],
            second_page_search_query_count=1,
        )

        self.assertEqual(
            client.events,
            [
                "operation:source:search:title:page:1",
                "request:search:平台招商负责人:page:1",
                "operation:source:search:title:page:2",
                "request:search:平台招商负责人:page:2",
                "operation:source:search:merchant:page:1",
                "request:search:重点商家拓展:page:1",
            ],
        )
        self.assertEqual(
            [row.encrypt_geek_id for row in cards],
            ["shared", "title-p1", "title-p2", "merchant-p1"],
        )
        self.assertEqual(next(row for row in cards if row.encrypt_geek_id == "title-p2").source_page, 2)
        self.assertEqual(routes["shared"], {"title", "merchant"})
        self.assertTrue(all(value == {"search"} for value in sources.values()))

    def test_recommendation_then_search_routes_are_strictly_serial_and_never_follow_has_more(self) -> None:
        client = RouteClient()

        cards, sources, routes = collect_single_job_cards(
            client,
            job_id="job-open",
            recommendation_source_enabled=1,
            search_routes=[
                {"id": "title", "query": "平台招商负责人"},
                {"id": "merchant", "query": "重点商家拓展"},
            ],
        )

        self.assertEqual(
            client.events,
            [
                "operation:source:recommendation:page:1",
                "request:recommendation",
                "operation:source:search:title:page:1",
                "request:search:平台招商负责人",
                "operation:source:search:merchant:page:1",
                "request:search:重点商家拓展",
            ],
        )
        self.assertEqual(client.recommendation_calls, 1)
        self.assertEqual(client.search_calls, ["平台招商负责人", "重点商家拓展"])
        self.assertEqual(len(cards), 6)
        self.assertEqual(sources["rec-1"], {"recommendation"})
        self.assertEqual(routes["title-1"], {"title"})
        self.assertEqual(routes["merchant-1"], {"merchant"})

    def test_source_collection_consumes_exact_manifest_order_and_persists_all_cards(self) -> None:
        search_plan = {
            "contract": "generic_search_plan",
            "version": "search-current",
            "source_jd_hash": "jd-current",
            "routes": [
                {"id": "title", "query": "平台招商负责人"},
                {"id": "merchant", "query": "重点商家拓展"},
            ],
        }
        plan = build_single_job_run_plan(
            board_date="2026-09-01",
            config=SingleJobRunConfig.from_mapping(
                {
                    "recommendation_source_enabled": 1,
                    "top_priority_search_query_count": 2,
                    "recent_view_filter": "exclude_14d",
                }
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=search_plan,
        )
        client = RouteClient()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_single_job_source_collection(
                plan=plan,
                client=client,
                work_dir=root / "runs" / "run-1",
                generated_at="2026-09-01T08:00:00+08:00",
            )
            artifact = json.loads(result["artifact_path"].read_text(encoding="utf-8"))
            inventory = CandidateInventory.load(result["inventory_path"])

        self.assertEqual(
            client.events,
            [
                "operation:metadata:open-jobs",
                "operation:metadata:single-open-job-detail",
                "operation:source:recommendation:page:1",
                "request:recommendation",
                "operation:source:search:title:page:1",
                "request:search:平台招商负责人",
                "operation:source:search:merchant:page:1",
                "request:search:重点商家拓展",
            ],
        )
        self.assertEqual(artifact["candidate_count"], 6)
        self.assertEqual(artifact["schema_version"], 2)
        self.assertEqual(artifact["recent_view_filter"], "exclude_14d")
        self.assertEqual(client.search_filter_calls, ["exclude_14d", "exclude_14d"])
        self.assertEqual(
            [row["recent_view_filter"] for row in artifact["source_observations"]["pages"]],
            [None, "exclude_14d", "exclude_14d"],
        )
        self.assertEqual(artifact["inventory_baseline"]["known_job_candidate_ids_before_source"], [])
        self.assertEqual(artifact["source_observations"]["raw_row_count"], 6)
        self.assertEqual(artifact["source_observations"]["cross_observation_overlap_count"], 0)
        self.assertEqual(len(artifact["candidates"]), 6)
        self.assertEqual(len(inventory.to_dict()["candidates"]), 6)
        self.assertTrue(
            all(
                row["status"] == {"card": "ready", "resume": "pending"}
                for row in artifact["candidates"]
            )
        )
        self.assertEqual(len(inventory.list_resume_pending(job_id="job-open")), 6)
        self.assertEqual(client.detail_calls, ["job:job-open"])

    def test_source_collection_passes_frozen_search_filters_and_rejects_tampering(self) -> None:
        search_plan = {
            "contract": "generic_search_plan",
            "version": "search-current",
            "source_jd_hash": "jd-current",
            "routes": [{"id": "title", "query": "平台招商负责人"}],
        }
        plan = build_single_job_run_plan(
            board_date="2026-09-01",
            config=SingleJobRunConfig.from_mapping(
                {"recommendation_source_enabled": 0, "top_priority_search_query_count": 1}
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=search_plan,
            search_filter_inputs=("学历=本科及以上", "活跃度=近一周活跃"),
        )
        client = RouteClient()

        with tempfile.TemporaryDirectory() as tmp:
            result = run_single_job_source_collection(
                plan=plan,
                client=client,
                work_dir=Path(tmp) / "run",
                generated_at="2026-09-01T08:00:00+08:00",
            )
            artifact = json.loads(result["artifact_path"].read_text(encoding="utf-8"))

        self.assertEqual(
            client.search_param_calls,
            [{"activeness": "4", "degree": "203,201"}],
        )
        self.assertEqual(
            artifact["search_filter_params"],
            {"activeness": "4", "degree": "203,201"},
        )
        self.assertEqual(
            artifact["source_observations"]["pages"][0]["search_filter_params"],
            {"activeness": "4", "degree": "203,201"},
        )

        tampered = {**plan, "search_filter_params": {"degree": "204,201"}}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "当前定义不一致"):
            run_single_job_source_collection(
                plan=tampered,
                client=RouteClient(),
                work_dir=Path(tmp) / "run",
            )

    def test_source_collection_persists_each_page_and_stops_after_page2_risk(self) -> None:
        search_plan = {
            "contract": "generic_search_plan",
            "version": "search-current",
            "source_jd_hash": "jd-current",
            "routes": [
                {"id": "title", "query": "平台招商负责人"},
                {"id": "merchant", "query": "重点商家拓展"},
            ],
        }
        plan = build_single_job_run_plan(
            board_date="2026-09-01",
            config=SingleJobRunConfig.from_mapping(
                {
                    "recommendation_source_enabled": 0,
                    "top_priority_search_query_count": 2,
                    "second_page_search_query_count": 1,
                }
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=search_plan,
        )
        client = PageRouteClient(fail_on=("平台招商负责人", 2))

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "run"
            with self.assertRaises(BossRiskStop):
                run_single_job_source_collection(
                    plan=plan,
                    client=client,
                    work_dir=work_dir,
                    generated_at="2026-09-01T08:00:00+08:00",
                )
            page_files = sorted((work_dir / "source_pages").glob("*.json"))
            page_evidence = json.loads(page_files[0].read_text(encoding="utf-8"))

            self.assertEqual(len(page_files), 1)
            self.assertEqual(page_evidence["page"], 1)
            self.assertEqual(page_evidence["route_id"], "title")
            self.assertFalse((work_dir / "source_collection.json").exists())
            self.assertFalse((work_dir.parent / "candidate_inventory.json").exists())

        self.assertEqual(
            client.events[-2:],
            [
                "operation:source:search:title:page:2",
                "request:search:平台招商负责人:page:2",
            ],
        )

    def test_source_collection_rejects_page2_count_above_page1_count_before_requests(self) -> None:
        search_plan = {
            "contract": "generic_search_plan",
            "version": "search-current",
            "source_jd_hash": "jd-current",
            "routes": [{"id": "title", "query": "平台招商负责人"}],
        }
        plan = build_single_job_run_plan(
            board_date="2026-09-01",
            config=SingleJobRunConfig.from_mapping(
                {
                    "recommendation_source_enabled": 0,
                    "top_priority_search_query_count": 1,
                    "second_page_search_query_count": 1,
                }
            ),
            auth_dir=Path("data/local/auth"),
            search_job_id="job-open",
            persisted_search_plan=search_plan,
        )
        plan["second_page_search_query_count"] = 2
        plan["second_page_search_query_shortfall"] = 1
        client = PageRouteClient()

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "第二页"):
            run_single_job_source_collection(
                plan=plan,
                client=client,
                work_dir=Path(tmp) / "run",
            )

        self.assertEqual(client.events, [])

    def test_detail_collection_skips_cached_resumes_and_leaves_unselected_candidates_pending(self) -> None:
        inventory = CandidateInventory()
        for candidate_id, boss_id in (
            ("candidate-a", "boss-a"),
            ("candidate-b", "boss-b"),
            ("candidate-c", "boss-c"),
        ):
            inventory.record_source_card(
                candidate_id,
                "recommendation",
                {
                    "encryptGeekId": boss_id,
                    "encryptJobId": "job-open",
                    "securityId": f"security-{boss_id}",
                },
            )
        selection = inventory.select_resume_pending(job_id="job-open", selection="2")
        plan = build_candidate_detail_run_plan(
            board_date="2026-09-01",
            auth_dir=Path("data/local/auth"),
            selection=selection,
        )
        inventory.ensure_resume("candidate-a", lambda: {"cached": True})
        client = RouteClient()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            root.mkdir()
            inventory.save(root / "candidate_inventory.json")
            result = run_candidate_detail_collection(
                plan=plan,
                client=client,
                parse_resume=parse_resume,
                work_dir=root / "detail-run",
                generated_at="2026-09-01T09:00:00+08:00",
            )
            restored = CandidateInventory.load(result["inventory_path"])

        self.assertEqual(client.events, ["operation:detail:candidate-b"])
        self.assertEqual(client.detail_calls, ["boss-b"])
        self.assertEqual(result["artifact"]["cached_candidate_ids"], ["candidate-a"])
        self.assertEqual(result["artifact"]["fetched_candidate_ids"], ["candidate-b"])
        self.assertEqual(
            [row["candidate_id"] for row in restored.list_resume_pending(job_id="job-open")],
            ["candidate-c"],
        )

    def test_launcher_accepts_an_immutable_detail_plan_instead_of_source_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            plan = build_candidate_detail_run_plan(
                board_date="2026-09-01",
                auth_dir=auth_dir,
                selection={
                    "job_id": "job-open",
                    "selection": "all",
                    "pending_count": 1,
                    "selected_count": 1,
                    "remaining_pending_count": 0,
                    "candidates": [
                        {
                            "candidate_id": "candidate-a",
                            "encrypt_geek_id": "boss-a",
                            "encrypt_job_id": "job-open",
                            "security_id": "security-boss-a",
                        }
                    ],
                },
            )
            plan_path = root / "detail-plan.json"
            write_single_job_run_plan(plan, plan_path)
            args = run_single_job_live.parse_args(
                [
                    "authorize",
                    "--plan",
                    str(plan_path),
                    "--work-dir",
                    str(root / "runs"),
                    "--confirm-live",
                    "2026-09-01",
                    "--note",
                    "人工确认",
                ]
            )
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                bound = run_single_job_live._requested_plan(args, board_date="2026-09-01")

        self.assertEqual(bound["plan_kind"], "candidate_details")
        self.assertEqual(bound["selected_count"], 1)
        self.assertTrue(bound["execution_policy"]["fixed_auth_namespace"])

    def test_launcher_accepts_an_immutable_favorite_sync_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "auth"
            plan = build_favorite_sync_run_plan(
                board_date="2026-09-01",
                auth_dir=auth_dir,
                mode="initialize",
                purpose="publish",
                checkpoint=None,
                max_pages=2,
            )
            plan_path = root / "favorite-sync-plan.json"
            write_single_job_run_plan(plan, plan_path)
            args = run_single_job_live.parse_args(
                [
                    "authorize",
                    "--plan",
                    str(plan_path),
                    "--work-dir",
                    str(root / "runs"),
                    "--confirm-live",
                    "2026-09-01",
                    "--note",
                    "人工确认",
                ]
            )
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                bound = run_single_job_live._requested_plan(args, board_date="2026-09-01")

        self.assertEqual(bound["plan_kind"], "favorite_registry_sync")
        self.assertEqual(bound["mode"], "initialize")
        self.assertEqual(bound["max_pages"], 2)
        self.assertTrue(bound["execution_policy"]["fixed_guard_namespace"])

    def test_cli_run_requires_explicit_live_switch_before_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            args = run_single_job_live.parse_args(
                [
                    "run",
                    "--authorization-id", "authorization-1",
                    "--config", str(config_path),
                    "--work-dir", str(Path(tmp) / "run"),
                ]
            )
            with patch.object(run_single_job_live, "read_single_job_run_config") as read_config:
                with self.assertRaisesRegex(BossLiveAccessDenied, "run --live"):
                    run_single_job_live.run_command(args)
            read_config.assert_not_called()

    def test_cli_uses_fixed_namespaces_and_consumes_authorization_before_live_setup(self) -> None:
        config_value = {
            "recommendation_source_enabled": 1,
            "top_priority_search_query_count": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config_value), encoding="utf-8")
            auth_dir = root / "fixed-auth"
            guard_dir = root / "fixed-guard"
            args = run_single_job_live.parse_args(
                [
                    "run",
                    "--live",
                    "--authorization-id", "authorization-1",
                    "--config", str(config_path),
                    "--work-dir", str(root / "runs"),
                ]
            )
            authorization = SimpleNamespace(
                authorization_id="authorization-1",
                summary=lambda: "authorization=authorization-1 status=CONSUMED",
            )
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                with patch.object(run_single_job_live, "FIXED_GUARD_DIR", guard_dir):
                    with patch.object(
                        run_single_job_live,
                        "load_saved_session_fingerprint",
                        return_value="session-a",
                    ):
                        with patch.object(run_single_job_live, "LiveAuthorizationStore") as store_class:
                            store_class.return_value.consume.return_value = authorization
                            with patch.object(
                                run_single_job_live,
                                "run_single_job_source_collection",
                            ) as pipeline:
                                pipeline.return_value = {
                                    "candidate_cards": 2,
                                    "artifact_path": root / "source_collection.json",
                                }
                                fake_modules = {
                                    "boss_agent_cli.auth.manager": type(
                                        "AuthModule",
                                        (),
                                        {"AuthManager": lambda path: path},
                                    ),
                                }
                                with patch.dict("sys.modules", fake_modules):
                                    with patch("boss_hire.boss_guard.BossRequestGuard") as guard_class:
                                        guard_class.return_value.__enter__.return_value = (
                                            guard_class.return_value
                                        )
                                        with patch(
                                            "boss_hire.safe_recruiter_client.SafeBossRecruiterClient"
                                        ) as client_class:
                                            client_class.return_value.__enter__.return_value = FakeClient()
                                            result = run_single_job_live.run_command(
                                                args,
                                                now=lambda: datetime(
                                                    2026,
                                                    9,
                                                    1,
                                                    10,
                                                    0,
                                                    tzinfo=ZoneInfo("Asia/Shanghai"),
                                                ),
                                            )
            self.assertEqual(result, 0)
            plans = list((root / "runs").glob("*/run_plan.json"))
            self.assertEqual(len(plans), 1)
            audit_plan = json.loads(plans[0].read_text(encoding="utf-8"))
            self.assertEqual(audit_plan["execution_policy"]["plan_role"], "audit_record_only")
            self.assertTrue(audit_plan["execution_policy"]["fixed_auth_namespace"])
            store_class.assert_called_once_with(guard_dir)
            store_class.return_value.consume.assert_called_once()
            guard_class.assert_called_once_with(
                root=guard_dir,
                account_key=account_key_for(auth_dir),
                run_id=f"{audit_plan['plan_id']}-authoriz",
                operation_manifest=audit_plan["operation_manifest"],
            )
            pipeline.assert_called_once()

    def test_cli_routes_favorite_sync_to_fixed_account_registry_after_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_dir = root / "fixed-auth"
            guard_dir = root / "fixed-guard"
            account_state = root / "fixed-account-state"
            plan = build_favorite_sync_run_plan(
                board_date="2026-09-01",
                auth_dir=auth_dir,
                mode="initialize",
                purpose="publish",
                checkpoint=None,
                max_pages=2,
            )
            plan_path = root / "favorite-sync-plan.json"
            write_single_job_run_plan(plan, plan_path)
            args = run_single_job_live.parse_args(
                [
                    "run",
                    "--live",
                    "--authorization-id",
                    "authorization-sync",
                    "--plan",
                    str(plan_path),
                    "--work-dir",
                    str(root / "runs"),
                ]
            )
            authorization = SimpleNamespace(
                authorization_id="authorization-sync",
                summary=lambda: "authorization=authorization-sync status=CONSUMED",
            )
            pipeline_result = {
                "receipt": {
                    "status": "risk_stopped",
                    "complete": False,
                    "pages_read": 1,
                    "observed_count": 1,
                },
                "receipt_path": root / "favorite_sync_receipt.json",
            }
            with patch.object(run_single_job_live, "FIXED_AUTH_DIR", auth_dir):
                with patch.object(run_single_job_live, "FIXED_GUARD_DIR", guard_dir):
                    with patch.object(
                        run_single_job_live,
                        "favorite_account_state_dir",
                        return_value=account_state,
                    ):
                        with patch.object(
                            run_single_job_live,
                            "load_saved_session_fingerprint",
                            return_value="session-a",
                        ):
                            with patch.object(
                                run_single_job_live,
                                "LiveAuthorizationStore",
                            ) as store_class:
                                store_class.return_value.consume.return_value = authorization
                                with patch.object(
                                    run_single_job_live,
                                    "run_favorite_registry_sync",
                                    return_value=pipeline_result,
                                ) as pipeline:
                                    fake_modules = {
                                        "boss_agent_cli.auth.manager": type(
                                            "AuthModule",
                                            (),
                                            {"AuthManager": lambda path: path},
                                        ),
                                    }
                                    with patch.dict("sys.modules", fake_modules):
                                        with patch(
                                            "boss_hire.boss_guard.BossRequestGuard"
                                        ) as guard_class:
                                            guard_class.return_value.__enter__.return_value = (
                                                guard_class.return_value
                                            )
                                            with patch(
                                                "boss_hire.safe_recruiter_client.SafeBossRecruiterClient"
                                            ) as client_class:
                                                client_class.return_value.__enter__.return_value = (
                                                    FakeClient()
                                                )
                                                result = run_single_job_live.run_command(
                                                    args,
                                                    now=lambda: datetime(
                                                        2026,
                                                        9,
                                                        1,
                                                        10,
                                                        0,
                                                        tzinfo=ZoneInfo("Asia/Shanghai"),
                                                    ),
                                                )

            self.assertEqual(result, 2)
            pipeline.assert_called_once()
            called_registry = pipeline.call_args.kwargs["registry"]
            self.assertEqual(called_registry.root, account_state)
            self.assertEqual(called_registry.account_key, account_key_for(auth_dir))
            self.assertEqual(
                pipeline.call_args.kwargs["plan"]["plan_kind"],
                "favorite_registry_sync",
            )


if __name__ == "__main__":
    unittest.main()
