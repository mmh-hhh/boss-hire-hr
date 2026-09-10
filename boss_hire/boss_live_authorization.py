from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from boss_hire.boss_access import BossLiveAccessDenied, SHANGHAI, account_key_for
from boss_hire.boss_guard import _connect
from boss_hire.favorite_sync_contract import SYNC_MODES, SYNC_PURPOSES
from boss_hire.single_job_run_plan import (
    build_favorite_delivery_operation_manifest,
    build_favorite_sync_operation_manifest,
)
from boss_hire.search_filters import validate_resolved_filter_payload, validate_search_filter_params


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _shared_project_root(source_root: Path) -> Path:
    git_marker = source_root / ".git"
    if not git_marker.is_file():
        return source_root
    marker = git_marker.read_text(encoding="utf-8").strip()
    if not marker.startswith("gitdir:"):
        return source_root
    git_dir = Path(marker.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (source_root / git_dir).resolve(strict=False)
    common_marker = git_dir / "commondir"
    if not common_marker.is_file():
        return source_root
    common_git = (git_dir / common_marker.read_text(encoding="utf-8").strip()).resolve(strict=False)
    return common_git.parent


SHARED_PROJECT_ROOT = _shared_project_root(PROJECT_ROOT)
FIXED_AUTH_DIR = SHARED_PROJECT_ROOT / "data/local/boss_agent_cli_auth"
FIXED_GUARD_DIR = SHARED_PROJECT_ROOT / "data/local/boss_guard"
FIXED_ACCOUNT_STATE_ROOT = SHARED_PROJECT_ROOT / "data/local/boss_accounts"


def favorite_account_state_dir(account_key: str | None = None) -> Path:
    raw_account_key = account_key_for(FIXED_AUTH_DIR) if account_key is None else account_key
    stable_account_key = str(raw_account_key).strip()
    if re.fullmatch(r"[0-9a-f]{16}", stable_account_key) is None:
        raise ValueError("favorite account key must be a 16-character lowercase hex digest")
    return FIXED_ACCOUNT_STATE_ROOT / stable_account_key / "favorites"


@dataclass(frozen=True)
class LiveAuthorizationReceipt:
    authorization_id: str
    account_key: str
    session_fingerprint: str
    local_date: str
    plan_id: str
    plan_digest: str
    operation_count: int
    status: str

    def summary(self) -> str:
        return (
            f"authorization={self.authorization_id} status={self.status} "
            f"date={self.local_date} plan={self.plan_id} operations={self.operation_count}"
        )


def _local_now(clock: Callable[[], datetime] | None) -> datetime:
    value = clock() if clock is not None else datetime.now(tz=SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _operation_manifest(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    kind = str(plan.get("operation_manifest_kind") or "").strip()
    if kind not in {
        "source_collection",
        "job_catalog",
        "job_snapshot",
        "candidate_details",
        "favorite_delivery",
        "favorite_registry_sync",
    }:
        raise ValueError("live authorization operation manifest kind is invalid")
    raw_manifest = plan.get("operation_manifest")
    if not isinstance(raw_manifest, Sequence) or isinstance(raw_manifest, (str, bytes)) or not raw_manifest:
        raise ValueError("live authorization operation manifest must be a non-empty list")
    if kind in {"job_catalog", "job_snapshot"}:
        from boss_hire.job_setup import build_job_read_plan
        if plan.get("plan_kind") != kind:
            raise ValueError("岗位读取类型不一致")
        summary = plan.get("summary")
        if (kind == "job_catalog" and summary is not None) or (kind == "job_snapshot" and not isinstance(summary, dict)):
            raise ValueError("岗位读取绑定无效")
        expected = build_job_read_plan(account_key=str(plan.get("account_key") or ""),
                                      board_date=str(plan.get("board_date") or ""), summary=summary)
        if list(raw_manifest) != expected["operation_manifest"]:
            raise ValueError("岗位读取 manifest 与所选岗位不一致")
        return expected["operation_manifest"]
    recent_view_filter = str(plan.get("recent_view_filter", "include_all") or "").strip()
    if kind == "source_collection" and recent_view_filter not in {
        "include_all",
        "exclude_14d",
    }:
        raise ValueError("source recent_view_filter is invalid")
    has_search_filters = "search_filters" in plan or "search_filter_params" in plan
    if kind == "source_collection" and has_search_filters:
        if "search_filters" not in plan or "search_filter_params" not in plan:
            raise ValueError("source search filter binding is incomplete")
        validate_resolved_filter_payload(
            plan["search_filters"],
            plan["search_filter_params"],
            recent_view_filter,
        )
    search_filter_params = validate_search_filter_params(plan.get("search_filter_params"))
    if kind == "favorite_delivery":
        batch_id = str(plan.get("batch_id") or "").strip()
        candidates = plan.get("candidates")
        if not batch_id or not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise ValueError("favorite delivery plan candidate binding is incomplete")
        expected_manifest = build_favorite_delivery_operation_manifest(
            batch_id=batch_id,
            candidates=candidates,
        )
        if not expected_manifest or list(raw_manifest) != expected_manifest:
            raise ValueError("favorite delivery operation manifest does not match candidates")
        return expected_manifest
    if kind == "favorite_registry_sync":
        if plan.get("plan_kind") != "favorite_registry_sync":
            raise ValueError("favorite sync plan kind is invalid")
        plan_id = str(plan.get("plan_id") or "").strip()
        mode = str(plan.get("mode") or "").strip()
        purpose = str(plan.get("purpose") or "").strip()
        max_pages = plan.get("max_pages")
        if mode not in SYNC_MODES:
            raise ValueError("favorite sync mode is invalid")
        if purpose not in SYNC_PURPOSES:
            raise ValueError("favorite sync purpose is invalid")
        if "checkpoint_digest" not in plan:
            raise ValueError("favorite sync checkpoint binding is missing")
        checkpoint_digest = plan.get("checkpoint_digest")
        if mode == "initialize":
            if checkpoint_digest is not None:
                raise ValueError("initialize favorite sync cannot bind a checkpoint")
        elif re.fullmatch(r"[0-9a-f]{64}", str(checkpoint_digest or "")) is None:
            raise ValueError("incremental favorite sync checkpoint binding is invalid")
        if purpose == "publish":
            if "batch_id" in plan or "batch_digest" in plan:
                raise ValueError("publish favorite sync cannot bind a delivery batch")
        else:
            batch_id = str(plan.get("batch_id") or "").strip()
            batch_digest = str(plan.get("batch_digest") or "").strip()
            if not batch_id or re.fullmatch(r"[0-9a-f]{64}", batch_digest) is None:
                raise ValueError("favorite delivery sync batch binding is invalid")
        expected_manifest = build_favorite_sync_operation_manifest(
            plan_id=plan_id,
            max_pages=max_pages,
        )
        if list(raw_manifest) != expected_manifest:
            raise ValueError("favorite sync operation manifest does not match plan")
        return expected_manifest
    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    source_search_operations: list[tuple[str, int]] = []
    for index, raw_item in enumerate(raw_manifest):
        if not isinstance(raw_item, Mapping):
            raise ValueError(f"live authorization operation {index} must be an object")
        item = dict(raw_item)
        operation_key = str(item.get("operation_key") or "").strip()
        request_class = str(item.get("request_class") or "").strip()
        method = str(item.get("method") or "").strip().upper()
        endpoint_name = str(item.get("endpoint_name") or "").strip()
        binding = item.get("binding")
        if not operation_key or operation_key in seen_keys:
            raise ValueError("live authorization operation keys must be non-empty and unique")
        seen_keys.add(operation_key)
        if method != "GET" or not isinstance(binding, Mapping):
            raise ValueError("live authorization operation method or binding is invalid")
        if kind == "source_collection":
            if request_class == "metadata":
                expected_metadata = {
                    "metadata:open-jobs": ("list_jobs", {"expected_open_job_count": 1}),
                    "metadata:single-open-job-detail": (
                        "job_detail",
                        {"selection": "single_open_job"},
                    ),
                }
                if plan.get("selected_job_id"):
                    selected = str(plan["selected_job_id"])
                    expected_metadata = {
                        "metadata:open-jobs": ("list_jobs", {"selected_job_id": selected}),
                        "metadata:selected-job-detail": ("job_detail", {"job_id": selected}),
                    }
                expected = expected_metadata.get(operation_key)
                if expected is None or endpoint_name != expected[0] or dict(binding) != expected[1]:
                    raise ValueError("source metadata operation is invalid")
            else:
                if request_class != "list" or endpoint_name not in {"recommend_geeks", "search_geeks"}:
                    raise ValueError("source operation endpoint is invalid")
                page = binding.get("page")
                if endpoint_name == "recommend_geeks":
                    if page != 1:
                        raise ValueError("recommendation operation must bind page 1")
                    if operation_key != "source:recommendation:page:1":
                        raise ValueError("recommendation operation key is invalid")
                else:
                    if page not in {1, 2} or isinstance(page, bool):
                        raise ValueError("search operation must bind page 1 or 2")
                    route_id = str(binding.get("route_id") or "").strip()
                    query = str(binding.get("query") or "").strip()
                    job_id = str(binding.get("job_id") or "").strip()
                    bound_recent_view_filter = str(
                        binding.get("recent_view_filter", "include_all") or ""
                    ).strip()
                    if not route_id or not query or not job_id:
                        raise ValueError("search operation binding is incomplete")
                    if bound_recent_view_filter != recent_view_filter:
                        raise ValueError("search operation recent_view_filter does not match plan")
                    if "recent_view_filter" in plan and "recent_view_filter" not in binding:
                        raise ValueError("search operation recent_view_filter binding is missing")
                    bound_search_filter_params = binding.get("search_filter_params")
                    if has_search_filters and bound_search_filter_params is None:
                        raise ValueError("search operation search_filter_params binding is missing")
                    if validate_search_filter_params(bound_search_filter_params) != search_filter_params:
                        raise ValueError("search operation search_filter_params does not match plan")
                    if operation_key != f"source:search:{route_id}:page:{page}":
                        raise ValueError("search operation key does not match route binding")
                    source_search_operations.append((route_id, page))
        else:
            if request_class != "detail" or endpoint_name != "view_geek":
                raise ValueError("candidate detail operation endpoint is invalid")
            candidate_id = str(binding.get("candidate_id") or "").strip()
            geek_id = str(binding.get("encrypt_geek_id") or "").strip()
            job_id = str(binding.get("encrypt_job_id") or "").strip()
            if not candidate_id or not geek_id or not job_id:
                raise ValueError("candidate detail operation binding is incomplete")
            if operation_key != f"detail:{candidate_id}":
                raise ValueError("candidate detail operation key does not match candidate binding")
        result.append(item)
    if kind == "source_collection":
        seen_routes: set[str] = set()
        second_page_prefix_open = True
        index = 0
        while index < len(source_search_operations):
            route_id, page = source_search_operations[index]
            if page != 1 or route_id in seen_routes:
                raise ValueError("search operations must start each unique route at page 1")
            seen_routes.add(route_id)
            has_second_page = (
                index + 1 < len(source_search_operations)
                and source_search_operations[index + 1] == (route_id, 2)
            )
            if has_second_page:
                if not second_page_prefix_open:
                    raise ValueError("second-page search routes must be a leading route prefix")
                index += 2
            else:
                second_page_prefix_open = False
                index += 1
    return result


def _plan_fields(plan: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
    plan_id = str(plan.get("plan_id") or "")
    local_date = str(plan.get("board_date") or "")
    account_key = str(plan.get("account_key") or "")
    if not plan_id or not local_date or not account_key:
        raise ValueError("live authorization plan identity is incomplete")
    operation_count = len(_operation_manifest(plan))
    payload = json.dumps(dict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return plan_id, local_date, account_key, operation_count, digest


class LiveAuthorizationStore:
    def __init__(self, root: Path = FIXED_GUARD_DIR) -> None:
        self.root = Path(root)

    def issue(
        self,
        *,
        plan: Mapping[str, Any],
        session_fingerprint: str,
        confirm_live: str,
        note: str,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> LiveAuthorizationReceipt:
        if not note.strip():
            raise ValueError("live authorization note is required")
        if not session_fingerprint.strip():
            raise ValueError("live authorization session fingerprint is required")
        current = _local_now(now)
        current_date = current.date().isoformat()
        if confirm_live != current_date:
            raise BossLiveAccessDenied(
                f"BOSS live authorization requires explicit Asia/Shanghai date {current_date}"
            )
        plan_id, plan_date, account_key, operation_count, digest = _plan_fields(plan)
        if plan_date != current_date:
            raise BossLiveAccessDenied("live authorization plan date is not current")
        authorization_id = (token_factory or (lambda: secrets.token_urlsafe(18)))()
        if not authorization_id.strip():
            raise ValueError("live authorization id cannot be empty")
        note_digest = hashlib.sha256(note.strip().encode("utf-8")).hexdigest()
        connection = _connect(self.root)
        try:
            try:
                connection.execute(
                    """
                    INSERT INTO live_authorizations(
                        authorization_id, account_key, session_fingerprint, local_date,
                        plan_id, plan_digest, planned_budget, status, created_at, note_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
                    """,
                    (
                        authorization_id,
                        account_key,
                        session_fingerprint,
                        current_date,
                        plan_id,
                        digest,
                        operation_count,
                        current.isoformat(),
                        note_digest,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise BossLiveAccessDenied(
                    "BOSS live authorization already exists for this session, date, and plan"
                ) from exc
        finally:
            connection.close()
        return LiveAuthorizationReceipt(
            authorization_id=authorization_id,
            account_key=account_key,
            session_fingerprint=session_fingerprint,
            local_date=current_date,
            plan_id=plan_id,
            plan_digest=digest,
            operation_count=operation_count,
            status="READY",
        )

    def consume(
        self,
        *,
        authorization_id: str,
        plan: Mapping[str, Any],
        session_fingerprint: str,
        now: Callable[[], datetime] | None = None,
    ) -> LiveAuthorizationReceipt:
        current = _local_now(now)
        current_date = current.date().isoformat()
        plan_id, plan_date, account_key, operation_count, digest = _plan_fields(plan)
        connection = _connect(self.root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT account_key, session_fingerprint, local_date, plan_id,
                       plan_digest, planned_budget, status
                FROM live_authorizations WHERE authorization_id = ?
                """,
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise BossLiveAccessDenied("BOSS live authorization was not found")
            if row[6] != "READY":
                raise BossLiveAccessDenied("BOSS live authorization is already consumed")
            if row[2] != current_date or plan_date != current_date:
                raise BossLiveAccessDenied("BOSS live authorization is not valid for the current date")
            if row[0] != account_key:
                raise BossLiveAccessDenied("BOSS live authorization account does not match the plan")
            if row[1] != session_fingerprint:
                raise BossLiveAccessDenied("BOSS live authorization session does not match")
            if row[3] != plan_id or row[4] != digest or int(row[5]) != operation_count:
                raise BossLiveAccessDenied("BOSS live authorization plan or manifest does not match")
            updated = connection.execute(
                """
                UPDATE live_authorizations
                SET status = 'CONSUMED', consumed_at = ?
                WHERE authorization_id = ? AND status = 'READY'
                """,
                (current.isoformat(), authorization_id),
            )
            if updated.rowcount != 1:
                raise BossLiveAccessDenied("BOSS live authorization could not be consumed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return LiveAuthorizationReceipt(
            authorization_id=authorization_id,
            account_key=account_key,
            session_fingerprint=session_fingerprint,
            local_date=current_date,
            plan_id=plan_id,
            plan_digest=digest,
            operation_count=operation_count,
            status="CONSUMED",
        )
