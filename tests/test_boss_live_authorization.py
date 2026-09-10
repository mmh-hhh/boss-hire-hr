from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from boss_hire.boss_access import BossLiveAccessDenied, account_key_for
from boss_hire.boss_live_authorization import (
    FIXED_ACCOUNT_STATE_ROOT,
    FIXED_AUTH_DIR,
    FIXED_GUARD_DIR,
    LiveAuthorizationStore,
    SHARED_PROJECT_ROOT,
    favorite_account_state_dir,
)
from boss_hire.single_job_run_plan import (
    build_candidate_detail_operation_manifest,
    build_favorite_delivery_operation_manifest,
    build_favorite_sync_operation_manifest,
    build_source_operation_manifest,
)


class BossLiveAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.plan = {
            "plan_id": "single-job-test",
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "recent_view_filter": "include_all",
            "operation_manifest_kind": "source_collection",
            "operation_manifest": build_source_operation_manifest(
                recommendation_source_enabled=1,
                search_routes=[
                    {
                        "id": "title_match",
                        "query": "平台招商负责人",
                        "job_id": "job-open",
                    }
                ],
                recent_view_filter="include_all",
            ),
        }

    def test_project_uses_one_fixed_auth_guard_and_account_state_namespace(self) -> None:
        source_root = Path(__file__).resolve().parents[1]

        self.assertEqual(FIXED_AUTH_DIR, SHARED_PROJECT_ROOT / "data/local/boss_agent_cli_auth")
        self.assertEqual(FIXED_GUARD_DIR, SHARED_PROJECT_ROOT / "data/local/boss_guard")
        self.assertEqual(FIXED_ACCOUNT_STATE_ROOT, SHARED_PROJECT_ROOT / "data/local/boss_accounts")
        self.assertEqual(
            favorite_account_state_dir(),
            FIXED_ACCOUNT_STATE_ROOT / account_key_for(FIXED_AUTH_DIR) / "favorites",
        )
        if (source_root / ".git").is_file():
            self.assertNotEqual(SHARED_PROJECT_ROOT, source_root)
            self.assertTrue((SHARED_PROJECT_ROOT / ".git").is_dir())

    def test_account_state_path_rejects_arbitrary_or_traversal_namespaces(self) -> None:
        for value in ("", "account-a", "../other-account", "A" * 16, "0" * 15):
            with self.subTest(value=value), self.assertRaises(ValueError):
                favorite_account_state_dir(value)

        self.assertEqual(
            favorite_account_state_dir("0123456789abcdef"),
            FIXED_ACCOUNT_STATE_ROOT / "0123456789abcdef" / "favorites",
        )

    def test_authorization_binds_date_session_and_manifest_and_is_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=self.plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工检查官方页面后授权本计划",
                now=lambda: self.now,
                token_factory=lambda: "authorization-1",
            )

            consumed = store.consume(
                authorization_id=receipt.authorization_id,
                plan=self.plan,
                session_fingerprint="session-a",
                now=lambda: self.now,
            )

            self.assertEqual(consumed.authorization_id, "authorization-1")
            self.assertEqual(consumed.operation_count, 2)
            with self.assertRaisesRegex(BossLiveAccessDenied, "already exists"):
                store.issue(
                    plan=self.plan,
                    session_fingerprint="session-a",
                    confirm_live="2026-09-01",
                    note="重复授权",
                    now=lambda: self.now,
                    token_factory=lambda: "authorization-new",
                )
            with self.assertRaisesRegex(BossLiveAccessDenied, "already consumed"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan=self.plan,
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )

    def test_authorization_rejects_changed_session_or_plan_without_consuming_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=self.plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认",
                now=lambda: self.now,
                token_factory=lambda: "authorization-2",
            )

            with self.assertRaisesRegex(BossLiveAccessDenied, "session"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan=self.plan,
                    session_fingerprint="session-b",
                    now=lambda: self.now,
                )
            changed_plans = []
            for field, changed_value in (
                ("query", "重点商家拓展"),
                ("job_id", "job-changed"),
            ):
                changed_manifest = [dict(item) for item in self.plan["operation_manifest"]]
                changed_manifest[1] = {
                    **changed_manifest[1],
                    "binding": {
                        **changed_manifest[1]["binding"],
                        field: changed_value,
                    },
                }
                changed_plans.append({**self.plan, "operation_manifest": changed_manifest})
            changed_route_manifest = [dict(item) for item in self.plan["operation_manifest"]]
            changed_route_manifest[1] = {
                **changed_route_manifest[1],
                "operation_key": "source:search:merchant:page:1",
                "binding": {
                    **changed_route_manifest[1]["binding"],
                    "route_id": "merchant",
                },
            }
            changed_plans.append({**self.plan, "operation_manifest": changed_route_manifest})
            for changed_plan in changed_plans:
                with self.subTest(changed=changed_plan), self.assertRaisesRegex(
                    BossLiveAccessDenied, "plan"
                ):
                    store.consume(
                        authorization_id=receipt.authorization_id,
                        plan=changed_plan,
                        session_fingerprint="session-a",
                        now=lambda: self.now,
                    )

            store.consume(
                authorization_id=receipt.authorization_id,
                plan=self.plan,
                session_fingerprint="session-a",
                now=lambda: self.now,
            )

    def test_authorization_rejects_incomplete_or_invalid_manifest_binding(self) -> None:
        invalid_plans = (
            {key: value for key, value in self.plan.items() if key != "operation_manifest"},
            {**self.plan, "operation_manifest": []},
            {
                **self.plan,
                "operation_manifest": [
                    {
                        **self.plan["operation_manifest"][1],
                        "binding": {
                            **self.plan["operation_manifest"][1]["binding"],
                            "page": 2,
                        },
                    }
                ],
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            for plan in invalid_plans:
                with self.subTest(plan=plan), self.assertRaises(ValueError):
                    store.issue(
                        plan=plan,
                        session_fingerprint="session-a",
                        confirm_live="2026-09-01",
                        note="人工确认",
                        now=lambda: self.now,
                    )

    def test_source_authorization_binds_recent_view_filter(self) -> None:
        filtered_plan = {
            **self.plan,
            "plan_id": "single-job-filtered",
            "recent_view_filter": "exclude_14d",
            "operation_manifest": build_source_operation_manifest(
                recommendation_source_enabled=0,
                search_routes=[
                    {
                        "id": "title_match",
                        "query": "平台招商负责人",
                        "job_id": "job-open",
                    }
                ],
                recent_view_filter="exclude_14d",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=filtered_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认过滤搜索",
                now=lambda: self.now,
                token_factory=lambda: "authorization-filtered",
            )
            changed_manifest = [dict(item) for item in filtered_plan["operation_manifest"]]
            changed_manifest[0] = {
                **changed_manifest[0],
                "binding": {
                    **changed_manifest[0]["binding"],
                    "recent_view_filter": "include_all",
                },
            }
            with self.assertRaisesRegex(ValueError, "recent_view_filter"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan={**filtered_plan, "operation_manifest": changed_manifest},
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )

    def test_source_authorization_binds_search_filter_params(self) -> None:
        filtered_plan = {
            **self.plan,
            "plan_id": "single-job-degree-filtered",
            "search_filters": [
                {
                    "field_id": "degree",
                    "field_label": "学历",
                    "parameter": "degree",
                    "option_ids": ["bachelor_plus"],
                    "option_labels": ["本科及以上"],
                    "value": "203,201",
                }
            ],
            "search_filter_params": {"degree": "203,201"},
            "operation_manifest": build_source_operation_manifest(
                recommendation_source_enabled=0,
                search_routes=[
                    {"id": "title_match", "query": "平台招商负责人", "job_id": "job-open"}
                ],
                search_filter_params={"degree": "203,201"},
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=filtered_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认学历筛选",
                now=lambda: self.now,
                token_factory=lambda: "authorization-degree-filtered",
            )
            changed_manifest = [dict(item) for item in filtered_plan["operation_manifest"]]
            changed_manifest[0] = {
                **changed_manifest[0],
                "binding": {**changed_manifest[0]["binding"], "search_filter_params": {}},
            }
            with self.assertRaisesRegex(ValueError, "search_filter_params"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan={**filtered_plan, "operation_manifest": changed_manifest},
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )

    def test_source_authorization_allows_only_route_bound_second_pages(self) -> None:
        page2_plan = {
            **self.plan,
            "operation_manifest": build_source_operation_manifest(
                recommendation_source_enabled=1,
                search_routes=[
                    {"id": "title_match", "query": "平台招商负责人", "job_id": "job-open"},
                    {"id": "merchant", "query": "重点商家拓展", "job_id": "job-open"},
                ],
                second_page_search_query_count=1,
            ),
        }
        invalid_manifests = []
        recommendation_page2 = [dict(item) for item in page2_plan["operation_manifest"]]
        recommendation_page2[0] = {
            **recommendation_page2[0],
            "operation_key": "source:recommendation:page:2",
            "binding": {"page": 2},
        }
        invalid_manifests.append(recommendation_page2)
        page3 = [dict(item) for item in page2_plan["operation_manifest"]]
        page3[2] = {
            **page3[2],
            "operation_key": "source:search:title_match:page:3",
            "binding": {**page3[2]["binding"], "page": 3},
        }
        invalid_manifests.append(page3)
        wrong_key = [dict(item) for item in page2_plan["operation_manifest"]]
        wrong_key[2] = {**wrong_key[2], "operation_key": "source:search:merchant:page:2"}
        invalid_manifests.append(wrong_key)
        page2_without_page1 = page2_plan["operation_manifest"][:1] + page2_plan["operation_manifest"][2:]
        invalid_manifests.append(page2_without_page1)

        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=page2_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认受控第二页",
                now=lambda: self.now,
                token_factory=lambda: "authorization-page2",
            )
            self.assertEqual(receipt.operation_count, 4)
            for manifest in invalid_manifests:
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    store.issue(
                        plan={**page2_plan, "plan_id": "changed-plan", "operation_manifest": manifest},
                        session_fingerprint="session-a",
                        confirm_live="2026-09-01",
                        note="人工确认",
                        now=lambda: self.now,
                    )

    def test_detail_authorization_binds_exact_persisted_candidate_list(self) -> None:
        detail_plan = {
            "plan_id": "candidate-details-test",
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "operation_manifest_kind": "candidate_details",
            "operation_manifest": build_candidate_detail_operation_manifest(
                [
                    {
                        "candidate_id": "candidate-a",
                        "encrypt_geek_id": "geek-a",
                        "encrypt_job_id": "job-open",
                        "security_id": "security-a",
                    },
                    {
                        "candidate_id": "candidate-b",
                        "encrypt_geek_id": "geek-b",
                        "encrypt_job_id": "job-open",
                    },
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=detail_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认详情候选清单",
                now=lambda: self.now,
                token_factory=lambda: "authorization-details",
            )
            changed = {
                **detail_plan,
                "operation_manifest": detail_plan["operation_manifest"][:-1],
            }
            with self.assertRaisesRegex(BossLiveAccessDenied, "plan or manifest"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan=changed,
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )
            consumed = store.consume(
                authorization_id=receipt.authorization_id,
                plan=detail_plan,
                session_fingerprint="session-a",
                now=lambda: self.now,
            )
            self.assertEqual(consumed.operation_count, 2)

    def test_favorite_authorization_recomputes_exact_candidate_subset_and_pairs(self) -> None:
        candidates = [
            {
                "candidate_id": "candidate-a",
                "rank": 1,
                "encrypt_geek_id": "geek-a",
                "encrypt_job_id": "job-open",
                "security_id": "security-a",
                "action": "favorite",
            },
            {
                "candidate_id": "candidate-b",
                "rank": 2,
                "encrypt_geek_id": "geek-b",
                "encrypt_job_id": "job-open",
                "security_id": "security-b",
                "action": "already_confirmed",
            },
        ]
        favorite_plan = {
            "plan_id": "favorite-test",
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "batch_id": "batch-a",
            "candidates": candidates,
            "operation_manifest_kind": "favorite_delivery",
            "operation_manifest": build_favorite_delivery_operation_manifest(
                batch_id="batch-a",
                candidates=candidates,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=favorite_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认收藏候选子集",
                now=lambda: self.now,
                token_factory=lambda: "authorization-favorite",
            )
            changed_candidates = [
                {**candidates[0], "encrypt_geek_id": "geek-changed"},
                candidates[1],
            ]
            changed_plan = {**favorite_plan, "candidates": changed_candidates}
            with self.assertRaisesRegex(ValueError, "manifest"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan=changed_plan,
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )
            reordered = {
                **favorite_plan,
                "operation_manifest": list(reversed(favorite_plan["operation_manifest"])),
            }
            with self.assertRaisesRegex(ValueError, "manifest"):
                store.consume(
                    authorization_id=receipt.authorization_id,
                    plan=reordered,
                    session_fingerprint="session-a",
                    now=lambda: self.now,
                )
            consumed = store.consume(
                authorization_id=receipt.authorization_id,
                plan=favorite_plan,
                session_fingerprint="session-a",
                now=lambda: self.now,
            )
            self.assertEqual(consumed.operation_count, 2)

    def test_favorite_authorization_rejects_any_other_post(self) -> None:
        candidate = {
            "candidate_id": "candidate-a",
            "rank": 1,
            "encrypt_geek_id": "geek-a",
            "encrypt_job_id": "job-open",
            "security_id": "security-a",
            "action": "favorite",
        }
        manifest = build_favorite_delivery_operation_manifest(
            batch_id="batch-a",
            candidates=[candidate],
        )
        invalid_plan = {
            "plan_id": "favorite-test",
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "batch_id": "batch-a",
            "candidates": [candidate],
            "operation_manifest_kind": "favorite_delivery",
            "operation_manifest": [
                {**manifest[0], "endpoint_name": "send_message"},
                manifest[1],
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            with self.assertRaisesRegex(ValueError, "manifest"):
                store.issue(
                    plan=invalid_plan,
                    session_fingerprint="session-a",
                    confirm_live="2026-09-01",
                    note="人工确认",
                    now=lambda: self.now,
                )

    def test_favorite_sync_authorization_recomputes_exact_read_only_pages(self) -> None:
        plan_id = "favorite-sync-test"
        sync_plan = {
            "plan_kind": "favorite_registry_sync",
            "plan_id": plan_id,
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "mode": "incremental",
            "purpose": "publish",
            "max_pages": 3,
            "checkpoint_digest": "a" * 64,
            "operation_manifest_kind": "favorite_registry_sync",
            "operation_manifest": build_favorite_sync_operation_manifest(
                plan_id=plan_id,
                max_pages=3,
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            receipt = store.issue(
                plan=sync_plan,
                session_fingerprint="session-a",
                confirm_live="2026-09-01",
                note="人工确认只读同步收藏列表",
                now=lambda: self.now,
                token_factory=lambda: "authorization-sync",
            )
            consumed = store.consume(
                authorization_id=receipt.authorization_id,
                plan=sync_plan,
                session_fingerprint="session-a",
                now=lambda: self.now,
            )
            self.assertEqual(consumed.operation_count, 3)

    def test_favorite_sync_authorization_rejects_tampering_and_mixed_writes(self) -> None:
        plan_id = "favorite-sync-test"
        manifest = build_favorite_sync_operation_manifest(plan_id=plan_id, max_pages=2)
        base = {
            "plan_kind": "favorite_registry_sync",
            "plan_id": plan_id,
            "board_date": "2026-09-01",
            "account_key": "fixed-account",
            "mode": "initialize",
            "purpose": "favorite_delivery",
            "max_pages": 2,
            "checkpoint_digest": None,
            "batch_id": "batch-a",
            "batch_digest": "b" * 64,
            "operation_manifest_kind": "favorite_registry_sync",
            "operation_manifest": manifest,
        }
        tampered_plans = [
            {**base, "plan_kind": "candidate_details"},
            {**base, "mode": "single_page"},
            {**base, "purpose": "unknown"},
            {**base, "checkpoint_digest": "c" * 64},
            {key: value for key, value in base.items() if key != "batch_digest"},
            {
                **base,
                "operation_manifest": [
                    manifest[0],
                    {
                        **manifest[1],
                        "binding": {"tag": 4, "page": 3},
                    },
                ],
            },
            {
                **base,
                "operation_manifest": [
                    {
                        **manifest[0],
                        "binding": {"tag": 5, "page": 1},
                    },
                    manifest[1],
                ],
            },
            {
                **base,
                "operation_manifest": [
                    manifest[0],
                    {
                        "operation_key": "favorite:batch-a:candidate-a:write",
                        "request_class": "write",
                        "method": "POST",
                        "endpoint_name": "favorite_candidate",
                        "binding": {
                            "batch_id": "batch-a",
                            "candidate_id": "candidate-a",
                        },
                    },
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            for index, plan in enumerate(tampered_plans):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    store.issue(
                        plan=plan,
                        session_fingerprint=f"session-{index}",
                        confirm_live="2026-09-01",
                        note="人工确认",
                        now=lambda: self.now,
                    )

    def test_authorization_requires_current_explicit_shanghai_date_and_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LiveAuthorizationStore(Path(tmp) / "guard")
            with self.assertRaisesRegex(BossLiveAccessDenied, "2026-09-01"):
                store.issue(
                    plan=self.plan,
                    session_fingerprint="session-a",
                    confirm_live="2026-08-31",
                    note="人工确认",
                    now=lambda: self.now,
                )
            with self.assertRaisesRegex(ValueError, "note"):
                store.issue(
                    plan=self.plan,
                    session_fingerprint="session-a",
                    confirm_live="2026-09-01",
                    note=" ",
                    now=lambda: self.now,
                )


if __name__ == "__main__":
    unittest.main()
