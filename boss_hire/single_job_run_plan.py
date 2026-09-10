from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from boss_hire.boss_access import (
    preflight_boss_access,
)
from boss_hire.favorite_registry import CHECKPOINT_CONTRACT
from boss_hire.favorite_sync_contract import FAVORITE_LIST_MAX_PAGES, FAVORITE_LIST_TAG, SYNC_MODES, SYNC_PURPOSES
from boss_hire.local_security import ensure_private_directory, ensure_private_file
from boss_hire.search_filters import resolve_filter_selections, validate_search_filter_params
from boss_hire.state_store import content_hash


RUN_PLAN_CONTRACT = "single_job_live_run_plan"
RECENT_VIEW_FILTERS = {"include_all", "exclude_14d"}


def build_favorite_sync_operation_manifest(
    *,
    plan_id: str,
    max_pages: int = FAVORITE_LIST_MAX_PAGES,
) -> list[dict[str, Any]]:
    stable_plan_id = _text(plan_id, "plan_id")
    if (
        not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or max_pages < 1
        or max_pages > FAVORITE_LIST_MAX_PAGES
    ):
        raise ValueError("favorite sync max_pages must be between 1 and 40")
    return [
        {
            "operation_key": f"favorite-sync:{stable_plan_id}:page:{page}",
            "request_class": "list",
            "method": "GET",
            "endpoint_name": "favorite_list",
            "binding": {"tag": FAVORITE_LIST_TAG, "page": page},
        }
        for page in range(1, max_pages + 1)
    ]


def build_favorite_sync_run_plan(
    *,
    board_date: str,
    auth_dir: Path,
    mode: str,
    purpose: str,
    checkpoint: Mapping[str, Any] | None,
    max_pages: int = FAVORITE_LIST_MAX_PAGES,
    batch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stable_date = _text(board_date, "board_date")
    try:
        date.fromisoformat(stable_date)
    except ValueError as exc:
        raise ValueError("board_date 必须是 YYYY-MM-DD") from exc
    stable_mode = _text(mode, "mode")
    if stable_mode not in SYNC_MODES:
        raise ValueError("favorite sync mode must be initialize or incremental")
    stable_purpose = _text(purpose, "purpose")
    if stable_purpose not in SYNC_PURPOSES:
        raise ValueError("favorite sync purpose must be publish or favorite_delivery")
    access = preflight_boss_access(
        live=False,
        confirm_live=None,
        auth_dir=Path(auth_dir),
        operation_count=max_pages,
    )
    if stable_mode == "incremental":
        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("contract") != CHECKPOINT_CONTRACT
            or checkpoint.get("account_key") != access.account_key
            or checkpoint.get("initialized_complete") is not True
            or not isinstance(checkpoint.get("anchor_group"), list)
        ):
            raise ValueError("incremental favorite sync requires a complete account checkpoint")
    elif checkpoint is not None:
        raise ValueError("initialize favorite sync must not reuse an existing checkpoint")
    checkpoint_digest = content_hash(checkpoint) if checkpoint is not None else None
    batch_binding: dict[str, Any] = {}
    if stable_purpose == "favorite_delivery":
        if not isinstance(batch, Mapping) or batch.get("contract") != "candidate_shortlist_batch":
            raise ValueError("favorite delivery sync requires a candidate shortlist batch")
        batch_binding = {
            "batch_id": _text(batch.get("batch_id"), "batch.batch_id"),
            "batch_digest": content_hash(batch),
        }
    elif batch is not None:
        raise ValueError("publish favorite sync cannot bind a delivery batch")
    identity = {
        "board_date": stable_date,
        "account_key": access.account_key,
        "mode": stable_mode,
        "purpose": stable_purpose,
        "max_pages": max_pages,
        "checkpoint_digest": checkpoint_digest,
        **batch_binding,
    }
    plan_id = "favorite-sync-" + content_hash(identity)[:12]
    operation_manifest = build_favorite_sync_operation_manifest(
        plan_id=plan_id,
        max_pages=max_pages,
    )
    return {
        "schema_version": 2,
        "contract": RUN_PLAN_CONTRACT,
        "plan_kind": "favorite_registry_sync",
        "plan_id": plan_id,
        "board_date": stable_date,
        "account_key": access.account_key,
        "mode": stable_mode,
        "purpose": stable_purpose,
        "max_pages": max_pages,
        "checkpoint_digest": checkpoint_digest,
        **batch_binding,
        "operation_manifest_kind": "favorite_registry_sync",
        "operation_manifest": operation_manifest,
        "data_policy": {
            "candidate_identity": "stable_encrypt_geek_id_only",
            "raw_candidate_data": "not_persisted",
        },
        "execution_policy": {
            "strictly_serial": True,
            "fixed_tag": FAVORITE_LIST_TAG,
            "stop_on_anchor_or_end": True,
            "on_error": "stop_without_retry",
        },
    }


def build_source_operation_manifest(
    *,
    recommendation_source_enabled: int,
    search_routes: Sequence[Mapping[str, Any]],
    second_page_search_query_count: int = 0,
    recent_view_filter: str = "include_all",
    search_filter_params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if recommendation_source_enabled not in {0, 1}:
        raise ValueError("recommendation_source_enabled 必须是 0 或 1")
    result: list[dict[str, Any]] = []
    if recommendation_source_enabled == 1:
        result.append(
            {
                "operation_key": "source:recommendation:page:1",
                "request_class": "list",
                "method": "GET",
                "endpoint_name": "recommend_geeks",
                "binding": {"page": 1},
            }
        )
    second_page_search_query_count = _non_negative_int(
        second_page_search_query_count,
        "second_page_search_query_count",
    )
    stable_recent_view_filter = _text(recent_view_filter, "recent_view_filter")
    if stable_recent_view_filter not in RECENT_VIEW_FILTERS:
        raise ValueError("recent_view_filter 必须是 include_all 或 exclude_14d")
    stable_search_filter_params = (
        validate_search_filter_params(search_filter_params)
        if search_filter_params is not None
        else None
    )
    seen_routes: set[str] = set()
    for index, route in enumerate(search_routes):
        route_id = _text(route.get("id"), f"search_routes[{index}].id")
        if route_id in seen_routes:
            raise ValueError(f"duplicate search route id: {route_id}")
        seen_routes.add(route_id)
        binding = {
            "route_id": route_id,
            "query": _text(route.get("query"), f"search_routes[{index}].query"),
            "job_id": _text(route.get("job_id"), f"search_routes[{index}].job_id"),
            "recent_view_filter": stable_recent_view_filter,
        }
        if stable_search_filter_params is not None:
            binding["search_filter_params"] = stable_search_filter_params
        pages = (1, 2) if index < second_page_search_query_count else (1,)
        for page in pages:
            result.append(
                {
                    "operation_key": f"source:search:{route_id}:page:{page}",
                    "request_class": "list",
                    "method": "GET",
                    "endpoint_name": "search_geeks",
                    "binding": {**binding, "page": page},
                }
            )
    if not result:
        raise ValueError("source operation manifest cannot be empty")
    return result


def build_source_collection_operation_manifest(
    *,
    recommendation_source_enabled: int,
    selected_job_id: str | None = None,
    search_routes: Sequence[Mapping[str, Any]],
    second_page_search_query_count: int = 0,
    recent_view_filter: str = "include_all",
    search_filter_params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "operation_key": "metadata:open-jobs",
            "request_class": "metadata",
            "method": "GET",
            "endpoint_name": "list_jobs",
            "binding": {"selected_job_id": selected_job_id} if selected_job_id else {"expected_open_job_count": 1},
        },
        {
            "operation_key": "metadata:selected-job-detail" if selected_job_id else "metadata:single-open-job-detail",
            "request_class": "metadata",
            "method": "GET",
            "endpoint_name": "job_detail",
            "binding": {"job_id": selected_job_id} if selected_job_id else {"selection": "single_open_job"},
        },
        *build_source_operation_manifest(
            recommendation_source_enabled=recommendation_source_enabled,
            search_routes=search_routes,
            second_page_search_query_count=second_page_search_query_count,
            recent_view_filter=recent_view_filter,
            search_filter_params=search_filter_params,
        ),
    ]


def build_candidate_detail_operation_manifest(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not candidates:
        raise ValueError("candidate detail operation manifest cannot be empty")
    result: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for index, candidate in enumerate(candidates):
        candidate_id = _text(candidate.get("candidate_id"), f"candidates[{index}].candidate_id")
        if candidate_id in seen_candidates:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen_candidates.add(candidate_id)
        binding = {
            "candidate_id": candidate_id,
            "encrypt_geek_id": _text(
                candidate.get("encrypt_geek_id"),
                f"candidates[{index}].encrypt_geek_id",
            ),
            "encrypt_job_id": _text(
                candidate.get("encrypt_job_id"),
                f"candidates[{index}].encrypt_job_id",
            ),
        }
        security_id = str(candidate.get("security_id") or "").strip()
        if security_id:
            binding["security_id"] = security_id
        result.append(
            {
                "operation_key": f"detail:{candidate_id}",
                "request_class": "detail",
                "method": "GET",
                "endpoint_name": "view_geek",
                "binding": binding,
            }
        )
    return result


def build_favorite_delivery_operation_manifest(
    *,
    batch_id: str,
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    stable_batch_id = _text(batch_id, "batch_id")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("favorite delivery candidates 必须是列表")
    result: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"candidates[{index}] 必须是对象")
        candidate_id = _text(candidate.get("candidate_id"), f"candidates[{index}].candidate_id")
        if candidate_id in seen_candidates:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        seen_candidates.add(candidate_id)
        action = _text(candidate.get("action"), f"candidates[{index}].action")
        if action == "already_confirmed":
            continue
        if action != "favorite":
            raise ValueError(f"candidates[{index}].action 无效：{action}")
        rank = candidate.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            raise ValueError(f"candidates[{index}].rank 必须是正整数")
        binding = {
            "batch_id": stable_batch_id,
            "candidate_id": candidate_id,
            "rank": rank,
            "encrypt_geek_id": _text(
                candidate.get("encrypt_geek_id"),
                f"candidates[{index}].encrypt_geek_id",
            ),
            "encrypt_job_id": _text(
                candidate.get("encrypt_job_id"),
                f"candidates[{index}].encrypt_job_id",
            ),
            "security_id": _text(
                candidate.get("security_id"),
                f"candidates[{index}].security_id",
            ),
        }
        operation_prefix = f"favorite:{stable_batch_id}:{candidate_id}"
        result.extend(
            [
                {
                    "operation_key": f"{operation_prefix}:write",
                    "request_class": "write",
                    "method": "POST",
                    "endpoint_name": "favorite_candidate",
                    "binding": dict(binding),
                },
                {
                    "operation_key": f"{operation_prefix}:verify",
                    "request_class": "detail",
                    "method": "GET",
                    "endpoint_name": "favorite_status",
                    "binding": dict(binding),
                },
            ]
        )
    return result


def build_candidate_detail_run_plan(
    *,
    board_date: str,
    auth_dir: Path,
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    stable_date = _text(board_date, "board_date")
    try:
        date.fromisoformat(stable_date)
    except ValueError as exc:
        raise ValueError("board_date 必须是 YYYY-MM-DD") from exc
    if not isinstance(selection, Mapping):
        raise ValueError("详情选择结果必须是对象")
    job_id = _text(selection.get("job_id"), "selection.job_id")
    selection_label = _text(selection.get("selection"), "selection.selection")
    counts: dict[str, int] = {}
    for field in ("pending_count", "selected_count", "remaining_pending_count"):
        counts[field] = _non_negative_int(selection.get(field), f"selection.{field}")
    candidates = selection.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("详情选择没有待抓候选人")
    if counts["selected_count"] != len(candidates):
        raise ValueError("详情选择 selected_count 与候选清单不一致")
    if counts["pending_count"] != counts["selected_count"] + counts["remaining_pending_count"]:
        raise ValueError("详情选择数量汇总不一致")
    if any(str(row.get("encrypt_job_id") or "").strip() != job_id for row in candidates if isinstance(row, Mapping)):
        raise ValueError("详情候选清单包含其他岗位")
    operation_manifest = build_candidate_detail_operation_manifest(candidates)
    access = preflight_boss_access(
        live=False,
        confirm_live=None,
        auth_dir=Path(auth_dir),
        operation_count=len(operation_manifest),
    )
    identity = {
        "board_date": stable_date,
        "account_key": access.account_key,
        "job_id": job_id,
        "selection": selection_label,
        "candidates": candidates,
        "operation_manifest": operation_manifest,
    }
    return {
        "schema_version": 2,
        "contract": RUN_PLAN_CONTRACT,
        "plan_kind": "candidate_details",
        "plan_id": "candidate-details-" + content_hash(identity)[:12],
        "board_date": stable_date,
        "account_key": access.account_key,
        "job_id": job_id,
        "selection": selection_label,
        **counts,
        "candidates": [dict(row) for row in candidates],
        "operation_manifest_kind": "candidate_details",
        "operation_manifest": operation_manifest,
        "data_policy": {
            "raw_candidate_data": "private_local_only",
            "ordinary_logs": "no_raw_resume_or_credentials",
        },
        "execution_policy": {
            "explicit_live_switch": True,
            "plan_role": "audit_record_only",
            "on_boss_risk": "stop_without_retry",
        },
    }


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


@dataclass(frozen=True)
class SingleJobRunConfig:
    """The only operator-controlled source collection settings."""

    recommendation_source_enabled: int
    top_priority_search_query_count: int
    second_page_search_query_count: int = 0
    recent_view_filter: str = "include_all"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SingleJobRunConfig":
        if not isinstance(value, Mapping):
            raise ValueError("单 JD 运行配置必须是对象")
        allowed_fields = {
            "recommendation_source_enabled",
            "top_priority_search_query_count",
            "second_page_search_query_count",
            "recent_view_filter",
        }
        unknown_fields = sorted(set(value) - allowed_fields)
        if unknown_fields:
            raise ValueError(f"单 JD 运行配置包含未知字段：{', '.join(unknown_fields)}")
        recommendation_source_enabled = value.get("recommendation_source_enabled")
        if (
            not isinstance(recommendation_source_enabled, int)
            or isinstance(recommendation_source_enabled, bool)
            or recommendation_source_enabled not in {0, 1}
        ):
            raise ValueError("recommendation_source_enabled 必须是 0 或 1")
        top_priority_search_query_count = _non_negative_int(
            value.get("top_priority_search_query_count"),
            "top_priority_search_query_count",
        )
        second_page_search_query_count = _non_negative_int(
            value.get("second_page_search_query_count", 0),
            "second_page_search_query_count",
        )
        if second_page_search_query_count > top_priority_search_query_count:
            raise ValueError(
                "second_page_search_query_count 不能超过 top_priority_search_query_count"
            )
        if recommendation_source_enabled == 0 and top_priority_search_query_count == 0:
            raise ValueError("至少启用一个候选来源")
        recent_view_filter = str(value.get("recent_view_filter", "include_all") or "").strip()
        if recent_view_filter not in RECENT_VIEW_FILTERS:
            raise ValueError("recent_view_filter 必须是 include_all 或 exclude_14d")
        if top_priority_search_query_count == 0 and recent_view_filter != "include_all":
            raise ValueError("未启用搜索时 recent_view_filter 必须是 include_all")
        return cls(
            recommendation_source_enabled=recommendation_source_enabled,
            top_priority_search_query_count=top_priority_search_query_count,
            second_page_search_query_count=second_page_search_query_count,
            recent_view_filter=recent_view_filter,
        )


def read_single_job_run_config(path: Path) -> SingleJobRunConfig:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取单 JD 运行配置：{path}") from exc
    return SingleJobRunConfig.from_mapping(value)


def load_single_job_run_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取单 JD 运行计划：{path}") from exc
    if not isinstance(value, dict) or value.get("contract") != RUN_PLAN_CONTRACT:
        raise ValueError("文件不是单 JD 运行计划")
    if not str(value.get("plan_id") or "").strip():
        raise ValueError("单 JD 运行计划缺少 plan_id")
    return value


def build_single_job_run_plan(
    *,
    board_date: str,
    config: SingleJobRunConfig,
    auth_dir: Path,
    search_job_id: str | None = None,
    selected_job_id: str | None = None,
    persisted_search_plan: Mapping[str, Any] | None = None,
    search_filter_inputs: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a reviewable source plan without constructing a BOSS client."""
    if selected_job_id is not None:
        selected_job_id = _text(selected_job_id, "selected_job_id")
        if search_job_id and search_job_id != selected_job_id:
            raise ValueError("搜索岗位与所选岗位不一致")
    stable_date = _text(board_date, "board_date")
    try:
        date.fromisoformat(stable_date)
    except ValueError as exc:
        raise ValueError("board_date 必须是 YYYY-MM-DD") from exc
    resolved_filters = resolve_filter_selections(search_filter_inputs)
    resolved_kwargs = dict(resolved_filters["sdk_kwargs"])
    explicit_recent_view_filter = resolved_kwargs.pop("recent_view_filter", None)
    search_filter_params = validate_search_filter_params(resolved_kwargs)
    recent_view_filter = config.recent_view_filter
    if explicit_recent_view_filter is not None:
        if recent_view_filter != "include_all" and recent_view_filter != explicit_recent_view_filter:
            raise ValueError("运行配置与本次近14天查看筛选冲突")
        recent_view_filter = explicit_recent_view_filter
    if search_filter_params and config.top_priority_search_query_count == 0:
        raise ValueError("未启用搜索时不能设置搜索筛选条件")
    selected_search_routes: list[dict[str, str]] = []
    search_plan_binding: dict[str, Any] = {}
    if config.top_priority_search_query_count > 0:
        if not isinstance(persisted_search_plan, Mapping) or not search_job_id:
            raise ValueError("搜索来源需要当前持久化搜索计划")
        if persisted_search_plan.get("contract") != "generic_search_plan":
            raise ValueError("当前持久化搜索计划 contract 无效")
        version = _text(persisted_search_plan.get("version"), "search_plan.version")
        source_jd_hash = _text(
            persisted_search_plan.get("source_jd_hash"),
            "search_plan.source_jd_hash",
        )
        routes = persisted_search_plan.get("routes")
        if not isinstance(routes, list):
            raise ValueError("当前持久化搜索计划 routes 无效")
        schema_version = persisted_search_plan.get("schema_version", 1)
        if schema_version in {2, 3}:
            priorities = [
                route.get("priority") if isinstance(route, Mapping) else None
                for route in routes
            ]
            if (
                not all(
                    isinstance(priority, int)
                    and not isinstance(priority, bool)
                    and priority > 0
                    for priority in priorities
                )
                or len(set(priorities)) != len(priorities)
                or set(priorities) != set(range(1, len(priorities) + 1))
            ):
                raise ValueError(f"当前 V{schema_version} 搜索计划 priority 无效")
            routes = sorted(routes, key=lambda route: route["priority"])
            if schema_version == 3:
                target_route_count = persisted_search_plan.get("target_route_count")
                generation_shortfall = persisted_search_plan.get("generation_shortfall")
                if (
                    not isinstance(target_route_count, int)
                    or isinstance(target_route_count, bool)
                    or not 8 <= target_route_count <= 12
                    or not isinstance(generation_shortfall, int)
                    or isinstance(generation_shortfall, bool)
                    or generation_shortfall < 0
                    or generation_shortfall != target_route_count - len(routes)
                ):
                    raise ValueError("当前 V3 搜索计划 target/shortfall 无效")
                for index, route in enumerate(routes):
                    if not isinstance(route, Mapping):
                        raise ValueError(f"search_plan.routes[{index}] 无效")
                    tokens = route.get("tokens")
                    signature = route.get("signature")
                    if (
                        not isinstance(tokens, list)
                        or not 2 <= len(tokens) <= 3
                        or not all(
                            isinstance(token, str)
                            and token.strip() == token
                            and token
                            and not any(character.isspace() for character in token)
                            for token in tokens
                        )
                    ):
                        raise ValueError(f"V3 search_plan.routes[{index}].tokens 无效")
                    if route.get("query") != " ".join(tokens):
                        raise ValueError(f"V3 search_plan.routes[{index}].query 与 tokens 不一致")
                    if (
                        not isinstance(signature, list)
                        or len(signature) != len(tokens)
                        or not all(isinstance(item, str) and item.strip() for item in signature)
                    ):
                        raise ValueError(f"V3 search_plan.routes[{index}].signature 无效")
        elif schema_version != 1:
            raise ValueError("当前持久化搜索计划 schema_version 无效")
        stable_search_job_id = _text(search_job_id, "search_job_id")
        for index, route in enumerate(routes[: config.top_priority_search_query_count]):
            if not isinstance(route, Mapping):
                raise ValueError(f"search_plan.routes[{index}] 无效")
            selected_search_routes.append(
                {
                    "id": _text(route.get("id"), f"search_plan.routes[{index}].id"),
                    "query": _text(route.get("query"), f"search_plan.routes[{index}].query"),
                    "job_id": stable_search_job_id,
                }
            )
        search_plan_binding = {
            "search_job_id": stable_search_job_id,
            "search_plan_version": version,
            "search_plan_source_jd_hash": source_jd_hash,
        }
    source_operation_manifest = build_source_collection_operation_manifest(
        selected_job_id=selected_job_id,
        recommendation_source_enabled=config.recommendation_source_enabled,
        search_routes=selected_search_routes,
        second_page_search_query_count=config.second_page_search_query_count,
        recent_view_filter=recent_view_filter,
        search_filter_params=search_filter_params if search_filter_inputs else None,
    )
    # This is deliberately frozen: it only derives the anonymized account receipt.
    access = preflight_boss_access(
        live=False,
        confirm_live=None,
        auth_dir=Path(auth_dir),
        operation_count=len(source_operation_manifest),
    )
    selected_search_query_count = len(selected_search_routes)
    selected_second_page_search_query_count = min(
        config.second_page_search_query_count,
        selected_search_query_count,
    )
    identity = {
        "board_date": stable_date,
        "account_key": access.account_key,
        "recommendation_source_enabled": config.recommendation_source_enabled,
        "top_priority_search_query_count": config.top_priority_search_query_count,
        "second_page_search_query_count": config.second_page_search_query_count,
        "recent_view_filter": recent_view_filter,
        "selected_search_routes": selected_search_routes,
        **search_plan_binding,
        "operation_manifest": source_operation_manifest,
    }
    if search_filter_inputs:
        identity.update(
            search_filters=resolved_filters["selections"],
            search_filter_params=search_filter_params,
        )
    if selected_job_id:
        identity["selected_job_id"] = selected_job_id
    plan_id = "single-job-" + content_hash(identity)[:12]
    result = {
        "schema_version": 2,
        "contract": RUN_PLAN_CONTRACT,
        "plan_id": plan_id,
        "board_date": stable_date,
        "account_key": access.account_key,
        "expected_open_job_count": 1,
        "recommendation_source_enabled": config.recommendation_source_enabled,
        "top_priority_search_query_count": config.top_priority_search_query_count,
        "second_page_search_query_count": config.second_page_search_query_count,
        "recent_view_filter": recent_view_filter,
        "selected_search_query_count": selected_search_query_count,
        "search_query_shortfall": max(
            config.top_priority_search_query_count - selected_search_query_count,
            0,
        ),
        "selected_second_page_search_query_count": selected_second_page_search_query_count,
        "second_page_search_query_shortfall": max(
            config.second_page_search_query_count - selected_second_page_search_query_count,
            0,
        ),
        "selected_search_routes": selected_search_routes,
        **search_plan_binding,
        "operation_manifest_kind": "source_collection",
        "operation_manifest": source_operation_manifest,
        "data_policy": {
            "raw_candidate_data": "private_local_only",
            "ordinary_logs": "no_raw_resume_or_credentials",
        },
        "execution_policy": {
            "explicit_live_switch": True,
            "plan_role": "audit_record_only",
            "on_open_job_count_not_one": "stop",
            "on_boss_risk": "stop_without_retry",
        },
    }
    if search_filter_inputs:
        result.update(
            search_filters=resolved_filters["selections"],
            search_filter_params=search_filter_params,
        )
    if selected_job_id:
        result["selected_job_id"] = selected_job_id
        result.pop("expected_open_job_count", None)
        result["execution_policy"].pop("on_open_job_count_not_one", None)
    return result


def write_single_job_run_plan(plan: Mapping[str, Any], path: Path) -> None:
    """Persist an immutable, private plan without exposing credentials or candidate data."""
    if plan.get("contract") != RUN_PLAN_CONTRACT:
        raise ValueError("文件不是单 JD 运行计划")
    target = Path(path)
    payload = json.dumps(dict(plan), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ensure_private_directory(target.parent)
    if target.exists():
        if target.read_text(encoding="utf-8") != payload:
            raise ValueError("同一路径的单 JD 运行计划不可覆盖")
        ensure_private_file(target)
        return
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise ValueError("同一路径的单 JD 运行计划不可覆盖") from exc
    ensure_private_file(target)
