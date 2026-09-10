from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from boss_hire.favorite_delivery import FavoriteDeliveryLedger, PLAN_CONTRACT
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.favorite_sync import collect_favorite_pages, persist_favorite_sync_result
from boss_hire.favorite_sync_contract import FAVORITE_SYNC_RECEIPT_CONTRACT, SYNC_PURPOSES
from boss_hire.local_security import ensure_private_directory, ensure_private_file
from boss_hire.mvp_pipeline import SearchCandidateCard, normalize_search_card
from boss_hire.recruiter_jobs import fetch_selected_open_job, fetch_single_open_job
from boss_hire.search_filters import validate_resolved_filter_payload, validate_search_filter_params
from boss_hire.single_job_run_plan import (
    RECENT_VIEW_FILTERS,
    RUN_PLAN_CONTRACT,
    build_candidate_detail_operation_manifest,
    build_favorite_delivery_operation_manifest,
    build_favorite_sync_operation_manifest,
    build_source_collection_operation_manifest,
)
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory, stable_local_candidate_id


RECOMMENDATION_URL = "https://www.zhipin.com/wapi/zpjob/rec/geek/list"
FAVORITE_RECEIPT_CONTRACT = "boss_favorite_delivery_receipt"


def _write_private_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ensure_private_directory(path.parent)
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise ValueError(f"已有真实运行产物不可覆盖：{path.name}")
        ensure_private_file(path)
        return
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    ensure_private_file(path)


def _recommendation_page(client: Any, job_id: str) -> dict[str, Any]:
    return client._request(
        "GET",
        RECOMMENDATION_URL,
        params={"jobId": job_id, "page": 1},
        extra_headers={"Referer": f"https://www.zhipin.com/web/frame/recommend/?jobid={job_id}&status=9"},
    )


def _single_page_cards(
    response: Any,
    *,
    job_id: str,
    field: str,
    operation: str,
    page: int = 1,
) -> list[SearchCandidateCard]:
    cards, _ = _single_page_cards_with_count(
        response,
        job_id=job_id,
        field=field,
        operation=operation,
        page=page,
    )
    return cards


def _single_page_cards_with_count(
    response: Any,
    *,
    job_id: str,
    field: str,
    operation: str,
    page: int,
) -> tuple[list[SearchCandidateCard], int]:
    """Normalize one explicitly planned page response and retain its row count."""
    if not isinstance(response, dict) or response.get("code") != 0:
        raise RuntimeError(f"{operation} failed on page {page}: {response}")
    data = response.get("zpData")
    rows = data.get(field) or [] if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"{operation} page {page} has invalid {field}: {rows!r}")
    cards: list[SearchCandidateCard] = []
    seen: set[str] = set()
    for rank, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        try:
            card = normalize_search_card(row, page=page, rank=rank, job_id=job_id)
        except ValueError:
            continue
        if card.encrypt_geek_id in seen:
            continue
        seen.add(card.encrypt_geek_id)
        cards.append(card)
    return cards, len(rows)


def _collect_single_job_cards(
    client: Any,
    *,
    job_id: str,
    recommendation_source_enabled: int,
    search_routes: Sequence[Mapping[str, Any]],
    second_page_search_query_count: int = 0,
    recent_view_filter: str = "include_all",
    search_filter_params: Mapping[str, Any] | None = None,
    page_observer: Callable[[Mapping[str, Any], Sequence[SearchCandidateCard]], None] | None = None,
) -> tuple[
    list[SearchCandidateCard],
    dict[str, set[str]],
    dict[str, set[str]],
    list[dict[str, Any]],
]:
    if (
        not isinstance(recommendation_source_enabled, int)
        or isinstance(recommendation_source_enabled, bool)
        or recommendation_source_enabled not in {0, 1}
    ):
        raise ValueError("recommendation_source_enabled 必须是 0 或 1")
    if not isinstance(search_routes, Sequence) or isinstance(search_routes, (str, bytes)):
        raise ValueError("search_routes 必须是有序列表")
    if recommendation_source_enabled == 0 and not search_routes:
        raise ValueError("至少启用一个候选来源")
    if (
        not isinstance(second_page_search_query_count, int)
        or isinstance(second_page_search_query_count, bool)
        or second_page_search_query_count < 0
        or second_page_search_query_count > len(search_routes)
    ):
        raise ValueError("second_page_search_query_count 必须在已选搜索路线范围内")
    stable_recent_view_filter = str(recent_view_filter or "").strip()
    if stable_recent_view_filter not in RECENT_VIEW_FILTERS:
        raise ValueError("recent_view_filter 必须是 include_all 或 exclude_14d")
    if not search_routes and stable_recent_view_filter != "include_all":
        raise ValueError("未启用搜索时 recent_view_filter 必须是 include_all")
    stable_search_filter_params = validate_search_filter_params(search_filter_params)
    if not search_routes and stable_search_filter_params:
        raise ValueError("未启用搜索时不能设置搜索筛选条件")

    cards_by_id: dict[str, SearchCandidateCard] = {}
    sources: dict[str, set[str]] = {}
    routes: dict[str, set[str]] = {}
    page_observations: list[dict[str, Any]] = []

    def merge_page(
        page_cards: Sequence[SearchCandidateCard],
        *,
        source: str,
        route_id: str | None = None,
    ) -> None:
        for card in page_cards:
            cards_by_id.setdefault(card.encrypt_geek_id, card)
            sources.setdefault(card.encrypt_geek_id, set()).add(source)
            if route_id is not None:
                routes.setdefault(card.encrypt_geek_id, set()).add(route_id)

    if recommendation_source_enabled == 1:
        with client.operation("source:recommendation:page:1"):
            response = _recommendation_page(client, job_id)
        page_cards, raw_row_count = _single_page_cards_with_count(
            response,
            job_id=job_id,
            field="geekList",
            operation="recommendations",
            page=1,
        )
        observation = {
            "source": "recommendation",
            "route_id": None,
            "query": None,
            "page": 1,
            "recent_view_filter": None,
            "raw_row_count": raw_row_count,
            "page_unique_candidate_ids": [card.encrypt_geek_id for card in page_cards],
        }
        if page_observer is not None:
            page_observer(observation, page_cards)
        merge_page(
            page_cards,
            source="recommendation",
        )
        page_observations.append(observation)

    seen_route_ids: set[str] = set()
    for index, search_route in enumerate(search_routes):
        if not isinstance(search_route, Mapping):
            raise ValueError(f"search_routes[{index}] 必须是对象")
        route_id = str(search_route.get("id") or "").strip()
        query = str(search_route.get("query") or "").strip()
        route_job_id = str(search_route.get("job_id") or job_id).strip()
        if not route_id or not query:
            raise ValueError(f"search_routes[{index}] 必须包含 id 和 query")
        if route_id in seen_route_ids:
            raise ValueError(f"重复 search route id：{route_id}")
        if route_job_id != job_id:
            raise ValueError(f"search route {route_id} 与当前岗位不一致")
        seen_route_ids.add(route_id)
        pages = (1, 2) if index < second_page_search_query_count else (1,)
        for page in pages:
            with client.operation(f"source:search:{route_id}:page:{page}"):
                response = client.search_geeks(
                    query,
                    page=page,
                    job_id=job_id,
                    recent_view_filter=stable_recent_view_filter,
                    **stable_search_filter_params,
                )
            page_cards, raw_row_count = _single_page_cards_with_count(
                response,
                job_id=job_id,
                field="geeks",
                operation=f"search_geeks[{route_id}]",
                page=page,
            )
            observation = {
                "source": "search",
                "route_id": route_id,
                "query": query,
                "page": page,
                "recent_view_filter": stable_recent_view_filter,
                "search_filter_params": stable_search_filter_params,
                "raw_row_count": raw_row_count,
                "page_unique_candidate_ids": [card.encrypt_geek_id for card in page_cards],
            }
            if page_observer is not None:
                page_observer(observation, page_cards)
            merge_page(
                page_cards,
                source="search",
                route_id=route_id,
            )
            page_observations.append(observation)

    return list(cards_by_id.values()), sources, routes, page_observations


def collect_single_job_cards(
    client: Any,
    *,
    job_id: str,
    recommendation_source_enabled: int,
    search_routes: Sequence[Mapping[str, Any]],
    second_page_search_query_count: int = 0,
    recent_view_filter: str = "include_all",
    search_filter_params: Mapping[str, Any] | None = None,
) -> tuple[list[SearchCandidateCard], dict[str, set[str]], dict[str, set[str]]]:
    """Collect the frozen page plan while preserving the public return shape."""
    cards, sources, routes, _ = _collect_single_job_cards(
        client,
        job_id=job_id,
        recommendation_source_enabled=recommendation_source_enabled,
        search_routes=search_routes,
        second_page_search_query_count=second_page_search_query_count,
        recent_view_filter=recent_view_filter,
        search_filter_params=search_filter_params,
    )
    return cards, sources, routes


def _source_card(card: SearchCandidateCard, route_ids: set[str]) -> dict[str, Any]:
    return {
        "name": card.name,
        "current_title": card.current_position,
        "work_years": card.work_year,
        "degree": card.degree,
        "encryptGeekId": card.encrypt_geek_id,
        "securityId": card.security_id,
        "encryptJobId": card.encrypt_job_id,
        "searchRouteIds": sorted(route_ids),
    }


def _candidate_ids_for_job(inventory: CandidateInventory, job_id: str) -> set[str]:
    result: set[str] = set()
    for candidate_id, candidate in inventory.to_dict().get("candidates", {}).items():
        if not isinstance(candidate, Mapping):
            continue
        sources = candidate.get("sources") or {}
        if any(
            str(card.get("encryptJobId") or card.get("encrypt_job_id") or "").strip() == job_id
            for cards in sources.values()
            if isinstance(cards, list)
            for card in cards
            if isinstance(card, Mapping)
        ):
            result.add(str(candidate_id))
    return result


def _valid_score_ids(inventory: CandidateInventory, job_id: str, rubric_version: str) -> set[str]:
    state = inventory.to_dict()
    evaluations = state.get("evaluations", {}).get(job_id, {})
    return {
        str(candidate_id)
        for candidate_id, evaluation in evaluations.items()
        if isinstance(evaluation, Mapping) and evaluation.get("rubric_version") == rubric_version
    }


def run_single_job_source_collection(
    *,
    plan: Mapping[str, Any],
    client: Any,
    work_dir: Path,
    inventory_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if (
        plan.get("contract") != RUN_PLAN_CONTRACT
        or plan.get("schema_version") != 2
        or (not plan.get("selected_job_id") and plan.get("expected_open_job_count") != 1)
    ):
        raise ValueError("无效的单 JD 来源采集计划")
    recommendation_source_enabled = plan.get("recommendation_source_enabled")
    selected_search_routes = plan.get("selected_search_routes")
    if (
        not isinstance(recommendation_source_enabled, int)
        or isinstance(recommendation_source_enabled, bool)
        or recommendation_source_enabled not in {0, 1}
    ):
        raise ValueError("运行计划 recommendation_source_enabled 无效")
    if not isinstance(selected_search_routes, list):
        raise ValueError("运行计划 selected_search_routes 无效")
    requested_second_page_count = plan.get("second_page_search_query_count", 0)
    selected_second_page_count = plan.get("selected_second_page_search_query_count", 0)
    second_page_shortfall = plan.get("second_page_search_query_shortfall", 0)
    requested_search_count = plan.get("top_priority_search_query_count")
    recent_view_filter = str(plan.get("recent_view_filter", "include_all") or "").strip()
    has_search_filters = "search_filters" in plan or "search_filter_params" in plan
    if has_search_filters:
        if "search_filters" not in plan or "search_filter_params" not in plan:
            raise ValueError("运行计划搜索筛选冻结数据不完整")
        validate_resolved_filter_payload(
            plan["search_filters"],
            plan["search_filter_params"],
            recent_view_filter,
        )
    search_filter_params = validate_search_filter_params(plan.get("search_filter_params"))
    if recent_view_filter not in RECENT_VIEW_FILTERS:
        raise ValueError("运行计划 recent_view_filter 无效")
    if not selected_search_routes and recent_view_filter != "include_all":
        raise ValueError("未启用搜索时运行计划 recent_view_filter 必须是 include_all")
    if (
        not isinstance(requested_search_count, int)
        or isinstance(requested_search_count, bool)
        or requested_search_count < 0
        or not isinstance(requested_second_page_count, int)
        or isinstance(requested_second_page_count, bool)
        or requested_second_page_count < 0
        or requested_second_page_count > requested_search_count
        or not isinstance(selected_second_page_count, int)
        or isinstance(selected_second_page_count, bool)
        or selected_second_page_count != min(requested_second_page_count, len(selected_search_routes))
        or second_page_shortfall != requested_second_page_count - selected_second_page_count
    ):
        raise ValueError("运行计划第二页搜索路线计数无效")
    expected_manifest = build_source_collection_operation_manifest(
        selected_job_id=plan.get("selected_job_id"),
        recommendation_source_enabled=recommendation_source_enabled,
        search_routes=selected_search_routes,
        second_page_search_query_count=selected_second_page_count,
        recent_view_filter=str(plan.get("recent_view_filter", "include_all")),
        search_filter_params=search_filter_params if has_search_filters else None,
    )
    if plan.get("operation_manifest") != expected_manifest:
        raise ValueError("运行计划 operation_manifest 与来源选择不一致")

    target = Path(work_dir)
    ensure_private_directory(target)
    timestamp = generated_at or datetime.now().astimezone().isoformat()
    page_evidence_count = 0

    def persist_page_evidence(
        observation: Mapping[str, Any],
        page_cards: Sequence[SearchCandidateCard],
    ) -> None:
        nonlocal page_evidence_count
        page_evidence_count += 1
        evidence = {
            "schema_version": 1,
            "contract": "single_job_source_page",
            "plan_id": plan["plan_id"],
            "observed_at": timestamp,
            "sequence": page_evidence_count,
            "source": observation["source"],
            "route_id": observation["route_id"],
            "query": observation["query"],
            "page": observation["page"],
            "recent_view_filter": observation["recent_view_filter"],
            "raw_row_count": observation["raw_row_count"],
            "page_unique_candidate_count": len(page_cards),
            "candidates": [card.to_dict() for card in page_cards],
        }
        if observation["source"] == "search" and has_search_filters:
            evidence["search_filter_params"] = observation["search_filter_params"]
        _write_private_immutable_json(
            target / "source_pages" / f"{page_evidence_count:03d}.json",
            evidence,
        )

    job = (fetch_selected_open_job(client, str(plan["selected_job_id"]))
           if plan.get("selected_job_id") else fetch_single_open_job(client))
    if plan.get("selected_job_jd_hash"):
        from boss_hire.state_store import jd_hash
        if jd_hash(job.to_jd_text()) != plan["selected_job_jd_hash"]:
            raise ValueError("所选 JD 已变化；请重新准备岗位材料，未读取候选来源")
    if selected_search_routes:
        bound_job_id = str(plan.get("search_job_id") or "").strip()
        if not bound_job_id or bound_job_id != job.encrypt_job_id:
            raise ValueError("当前开放岗位与授权 search_job_id 不一致")
    cards, card_sources, card_routes, page_observations = _collect_single_job_cards(
        client,
        job_id=job.encrypt_job_id,
        recommendation_source_enabled=recommendation_source_enabled,
        search_routes=selected_search_routes,
        second_page_search_query_count=selected_second_page_count,
        recent_view_filter=recent_view_filter,
        search_filter_params=search_filter_params,
        page_observer=persist_page_evidence,
    )
    stable_inventory_path = Path(inventory_path) if inventory_path is not None else target.parent / "candidate_inventory.json"
    inventory = CandidateInventory.load(stable_inventory_path)
    known_job_candidate_ids_before_source = _candidate_ids_for_job(inventory, job.encrypt_job_id)
    job_artifacts = inventory.get_job_artifacts(
        job.encrypt_job_id,
        str(plan.get("search_plan_source_jd_hash") or plan.get("selected_job_jd_hash") or ""),
    )
    baseline_rubric = job_artifacts.get("rubric") if isinstance(job_artifacts, Mapping) else None
    baseline_rubric_version = (
        str(baseline_rubric.get("version") or "") if isinstance(baseline_rubric, Mapping) else ""
    )
    valid_score_candidate_ids_before_source = _valid_score_ids(
        inventory,
        job.encrypt_job_id,
        baseline_rubric_version,
    )
    persisted_cards = []
    for card in cards:
        candidate_id = stable_local_candidate_id(card.encrypt_geek_id)
        route_ids = card_routes.get(card.encrypt_geek_id) or set()
        for source in sorted(card_sources.get(card.encrypt_geek_id) or set()):
            inventory.record_source_card(
                candidate_id,
                source,
                _source_card(card, route_ids),
            )
        persisted_cards.append(
            {
                "candidate_id": candidate_id,
                "card": card.to_dict(),
                "sources": sorted(card_sources.get(card.encrypt_geek_id) or set()),
                "search_route_ids": sorted(route_ids),
                "status": inventory.candidate_status(candidate_id),
            }
        )
    inventory.save(stable_inventory_path)
    page_unique_count = sum(len(row["page_unique_candidate_ids"]) for row in page_observations)
    stable_page_observations = [
        {
            "source": row["source"],
            "route_id": row["route_id"],
            "query": row["query"],
            "page": row["page"],
            "recent_view_filter": row["recent_view_filter"],
            "raw_row_count": row["raw_row_count"],
            "page_unique_candidate_ids": [
                stable_local_candidate_id(candidate_id) for candidate_id in row["page_unique_candidate_ids"]
            ],
        }
        for row in page_observations
    ]
    if has_search_filters:
        for row in stable_page_observations:
            if row["source"] == "search":
                row["search_filter_params"] = search_filter_params
    artifact = {
        "schema_version": 2,
        "contract": "single_job_source_collection",
        "plan_id": plan["plan_id"],
        "collected_at": timestamp,
        "job": job.to_dict(),
        "recommendation_source_enabled": recommendation_source_enabled,
        "requested_search_query_count": plan["top_priority_search_query_count"],
        "selected_search_query_count": plan["selected_search_query_count"],
        "search_query_shortfall": plan["search_query_shortfall"],
        "requested_second_page_search_query_count": requested_second_page_count,
        "selected_second_page_search_query_count": selected_second_page_count,
        "second_page_search_query_shortfall": second_page_shortfall,
        "recent_view_filter": recent_view_filter,
        "candidate_count": len(persisted_cards),
        "candidates": persisted_cards,
        "inventory_baseline": {
            "rubric_version": baseline_rubric_version,
            "known_job_candidate_ids_before_source": sorted(known_job_candidate_ids_before_source),
            "valid_score_candidate_ids_before_source": sorted(valid_score_candidate_ids_before_source),
        },
        "source_observations": {
            "page_count": len(stable_page_observations),
            "raw_row_count": sum(row["raw_row_count"] for row in stable_page_observations),
            "page_unique_candidate_count": page_unique_count,
            "cross_observation_overlap_count": page_unique_count - len(persisted_cards),
            "pages": stable_page_observations,
        },
    }
    if has_search_filters:
        artifact.update(
            search_filters=plan["search_filters"],
            search_filter_params=search_filter_params,
        )
    artifact_path = target / "source_collection.json"
    _write_private_immutable_json(artifact_path, artifact)
    return {
        "job": job,
        "candidate_cards": len(cards),
        "artifact_path": artifact_path,
        "inventory_path": stable_inventory_path,
        "artifact": artifact,
    }


def run_candidate_detail_collection(
    *,
    plan: Mapping[str, Any],
    client: Any,
    parse_resume: Callable[[dict[str, Any]], Mapping[str, Any]],
    work_dir: Path,
    inventory_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if (
        plan.get("contract") != RUN_PLAN_CONTRACT
        or plan.get("schema_version") != 2
        or plan.get("plan_kind") != "candidate_details"
        or plan.get("operation_manifest_kind") != "candidate_details"
    ):
        raise ValueError("无效的候选详情运行计划")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("候选详情运行计划缺少候选清单")
    expected_manifest = build_candidate_detail_operation_manifest(candidates)
    if plan.get("operation_manifest") != expected_manifest:
        raise ValueError("候选详情 operation_manifest 与候选清单不一致")
    job_id = str(plan.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("候选详情运行计划缺少 job_id")

    target = Path(work_dir)
    ensure_private_directory(target)
    stable_inventory_path = Path(inventory_path) if inventory_path is not None else target.parent / "candidate_inventory.json"
    inventory = CandidateInventory.load(stable_inventory_path)
    cached_candidate_ids: list[str] = []
    fetched_candidate_ids: list[str] = []
    try:
        for candidate, operation in zip(candidates, expected_manifest, strict=True):
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if inventory.get_resume(candidate_id) is not None:
                cached_candidate_ids.append(candidate_id)
                continue
            candidate_dir = target / "candidates" / candidate_id
            with client.operation(operation["operation_key"]):
                raw = client.view_geek(
                    str(candidate["encrypt_geek_id"]),
                    str(candidate["encrypt_job_id"]),
                    security_id=str(candidate.get("security_id") or ""),
                )
            resume = parse_resume(raw)
            if not isinstance(resume, Mapping):
                raise RuntimeError("候选人详情解析结果必须是对象")
            _write_private_immutable_json(candidate_dir / "raw_response.json", raw)
            _write_private_immutable_json(candidate_dir / "resume.json", dict(resume))
            inventory.ensure_resume(candidate_id, lambda resume=resume: dict(resume))
            inventory.save(stable_inventory_path)
            fetched_candidate_ids.append(candidate_id)
    finally:
        inventory.save(stable_inventory_path)

    timestamp = generated_at or datetime.now().astimezone().isoformat()
    artifact = {
        "schema_version": 1,
        "contract": "candidate_detail_collection",
        "plan_id": plan["plan_id"],
        "collected_at": timestamp,
        "job_id": job_id,
        "selection": plan["selection"],
        "selected_count": len(candidates),
        "detail_request_count": len(fetched_candidate_ids),
        "cached_resume_count": len(cached_candidate_ids),
        "fetched_candidate_ids": fetched_candidate_ids,
        "cached_candidate_ids": cached_candidate_ids,
        "remaining_pending_count": len(inventory.list_resume_pending(job_id=job_id)),
    }
    artifact_path = target / "detail_collection.json"
    _write_private_immutable_json(artifact_path, artifact)
    return {
        "artifact": artifact,
        "artifact_path": artifact_path,
        "inventory_path": stable_inventory_path,
    }


def run_favorite_registry_sync(
    *,
    plan: Mapping[str, Any],
    client: Any,
    registry: FavoriteRegistry,
    work_dir: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if (
        plan.get("contract") != RUN_PLAN_CONTRACT
        or plan.get("schema_version") != 2
        or plan.get("plan_kind") != "favorite_registry_sync"
        or plan.get("operation_manifest_kind") != "favorite_registry_sync"
    ):
        raise ValueError("无效的收藏状态同步计划")
    plan_id = str(plan.get("plan_id") or "").strip()
    account_key = str(plan.get("account_key") or "").strip()
    mode = str(plan.get("mode") or "").strip()
    purpose = str(plan.get("purpose") or "").strip()
    max_pages = plan.get("max_pages")
    if not plan_id or account_key != registry.account_key:
        raise ValueError("收藏状态同步计划账号绑定无效")
    if purpose not in SYNC_PURPOSES:
        raise ValueError("收藏状态同步计划用途无效")
    expected_manifest = build_favorite_sync_operation_manifest(
        plan_id=plan_id,
        max_pages=max_pages,
    )
    if plan.get("operation_manifest") != expected_manifest:
        raise ValueError("收藏状态同步 operation_manifest 与分页预算不一致")

    checkpoint = registry.checkpoint()
    if mode == "incremental":
        if (
            checkpoint is None
            or content_hash(checkpoint) != plan.get("checkpoint_digest")
            or not isinstance(checkpoint.get("anchor_group"), list)
        ):
            raise ValueError("增量收藏同步 checkpoint 已变化或无效")
        anchor_group = tuple(checkpoint["anchor_group"])
    elif mode == "initialize":
        if plan.get("checkpoint_digest") is not None:
            raise ValueError("初始化收藏同步不能绑定 checkpoint")
        anchor_group = ()
    else:
        raise ValueError("收藏状态同步模式无效")
    if purpose == "favorite_delivery":
        if not str(plan.get("batch_id") or "").strip() or not str(
            plan.get("batch_digest") or ""
        ).strip():
            raise ValueError("收藏交付同步缺少发布批次绑定")

    def fetch_page(page: int) -> Mapping[str, Any]:
        operation = expected_manifest[page - 1]
        with client.operation(str(operation["operation_key"])):
            response = client.favorite_list(page=page)
        if not isinstance(response, Mapping):
            raise RuntimeError("收藏列表响应必须是对象")
        return response

    collection = collect_favorite_pages(
        fetch_page=fetch_page,
        mode=mode,
        anchor_group=anchor_group,
        max_pages=max_pages,
    )
    timestamp = generated_at or datetime.now().astimezone().isoformat()
    persistence = persist_favorite_sync_result(
        registry=registry,
        result=collection,
        receipt_id=plan_id,
        observed_at=timestamp,
    )
    receipt = {
        "schema_version": 1,
        "contract": FAVORITE_SYNC_RECEIPT_CONTRACT,
        "plan_id": plan_id,
        "account_key": account_key,
        "board_date": plan.get("board_date"),
        "mode": mode,
        "purpose": purpose,
        "batch_id": plan.get("batch_id"),
        "batch_digest": plan.get("batch_digest"),
        "completed_at": timestamp,
        "status": collection.status,
        "complete": collection.complete,
        "pages_read": collection.pages_read,
        "max_pages": max_pages,
        "observed_count": persistence.observed_count,
        "new_candidate_count": persistence.new_candidate_count,
        "checkpoint_advanced": persistence.checkpoint_advanced,
        "first_page_ids": list(collection.first_page_ids),
        "error": collection.error,
    }
    receipt_path = Path(work_dir) / "favorite_sync_receipt.json"
    _write_private_immutable_json(receipt_path, receipt)
    return {"receipt": receipt, "receipt_path": receipt_path}


def run_favorite_delivery(
    *,
    plan: Mapping[str, Any],
    ledger: FavoriteDeliveryLedger,
    execute_candidate: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]],
        Mapping[str, Any],
    ],
    work_dir: Path,
    registry: FavoriteRegistry | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if (
        plan.get("contract") != PLAN_CONTRACT
        or plan.get("operation_manifest_kind") != "favorite_delivery"
    ):
        raise ValueError("无效的 BOSS 收藏交付计划")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("BOSS 收藏交付计划缺少候选子集")
    batch_id = str(plan.get("batch_id") or "").strip()
    expected_manifest = build_favorite_delivery_operation_manifest(
        batch_id=batch_id,
        candidates=candidates,
    )
    if plan.get("operation_manifest") != expected_manifest:
        raise ValueError("收藏交付 operation_manifest 与候选子集不一致")
    if plan.get("favorite_sync_receipt_id") and registry is None:
        raise ValueError("同步约束收藏计划必须使用账号级收藏注册表")
    retry_definite_failures = plan.get("retry_definite_failures", False)
    if not isinstance(retry_definite_failures, bool):
        raise ValueError("收藏交付计划 retry_definite_failures 无效")

    operation_index = 0
    results: list[dict[str, Any]] = []
    timestamp = generated_at or datetime.now().astimezone().isoformat()
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        encrypt_geek_id = str(candidate.get("encrypt_geek_id") or "").strip()
        rank = candidate.get("rank")
        if candidate.get("action") == "already_confirmed":
            results.append(
                {
                    "candidate_id": candidate_id,
                    "rank": rank,
                    "status": "already_confirmed",
                }
            )
            continue

        pair = expected_manifest[operation_index : operation_index + 2]
        if len(pair) != 2:
            raise ValueError(f"候选人 {candidate_id} 缺少收藏写入与回读 operation")
        write_operation, verify_operation = pair
        operation_index += 2
        if (
            write_operation["binding"].get("candidate_id") != candidate_id
            or verify_operation["binding"].get("candidate_id") != candidate_id
        ):
            raise ValueError(f"候选人 {candidate_id} operation binding 不一致")

        write_key = str(write_operation["operation_key"])
        if registry is not None:
            with registry.locked_contains(encrypt_geek_id) as already_favorited:
                if already_favorited:
                    results.append(
                        {
                            "candidate_id": candidate_id,
                            "rank": rank,
                            "status": "already_confirmed",
                        }
                    )
                    continue
                ledger.reserve_write(
                    candidate_id,
                    batch_id=batch_id,
                    operation_key=write_key,
                    retry_definite_failures=retry_definite_failures,
                    recorded_at=timestamp,
                )
        else:
            ledger.reserve_write(
                candidate_id,
                batch_id=batch_id,
                operation_key=write_key,
                retry_definite_failures=retry_definite_failures,
                recorded_at=timestamp,
            )
        try:
            raw_result = execute_candidate(candidate, write_operation, verify_operation)
            if not isinstance(raw_result, Mapping):
                raise RuntimeError("收藏候选执行结果必须是对象")
            status = str(raw_result.get("status") or "").strip()
            if status not in {"favorite_confirmed", "favorite_failed", "favorite_unknown"}:
                raise RuntimeError(f"收藏候选执行状态无效：{status or '<empty>'}")
            result = {
                "candidate_id": candidate_id,
                "rank": rank,
                "status": status,
            }
            for field in ("reason", "http_status", "response_code"):
                if raw_result.get(field) is not None:
                    result[field] = raw_result[field]
        except Exception as exc:  # noqa: BLE001 - a reserved write is uncertain after executor failure
            status = "favorite_unknown"
            result = {
                "candidate_id": candidate_id,
                "rank": rank,
                "status": status,
                "reason": type(exc).__name__,
            }
        ledger.finalize_write(
            candidate_id,
            status,
            batch_id=batch_id,
            operation_key=write_key,
            recorded_at=timestamp,
        )
        if status == "favorite_confirmed" and registry is not None:
            registry.record_candidates(
                [encrypt_geek_id],
                source="favorite_confirmed",
                receipt_id=str(plan.get("plan_id") or write_key),
                observed_at=timestamp,
            )
        results.append(result)
        if status != "favorite_confirmed":
            break

    receipt = {
        "schema_version": 1,
        "contract": FAVORITE_RECEIPT_CONTRACT,
        "plan_id": plan.get("plan_id"),
        "batch_id": batch_id,
        "completed_at": timestamp,
        "selected_count": len(candidates),
        "confirmed_count": sum(row["status"] == "favorite_confirmed" for row in results),
        "already_confirmed_count": sum(
            row["status"] == "already_confirmed" for row in results
        ),
        "failed_count": sum(row["status"] == "favorite_failed" for row in results),
        "unknown_count": sum(row["status"] == "favorite_unknown" for row in results),
        "not_selected_count": int(plan.get("not_selected_count") or 0),
        "not_selected_candidates": [
            {**dict(row), "status": "not_selected"}
            for row in plan.get("not_selected_candidates") or []
            if isinstance(row, Mapping)
        ],
        "stopped_early": len(results) < len(candidates)
        or any(row["status"] in {"favorite_failed", "favorite_unknown"} for row in results),
        "results": results,
    }
    receipt_path = Path(work_dir) / "favorite_delivery_receipt.json"
    _write_private_immutable_json(receipt_path, receipt)
    return {"receipt": receipt, "receipt_path": receipt_path}


def execute_favorite_candidate(
    *,
    client: Any,
    candidate: Mapping[str, Any],
    write_operation: Mapping[str, Any],
    verify_operation: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    encrypt_geek_id = str(candidate.get("encrypt_geek_id") or "").strip()
    encrypt_job_id = str(candidate.get("encrypt_job_id") or "").strip()
    security_id = str(candidate.get("security_id") or "").strip()
    if not all((candidate_id, encrypt_geek_id, encrypt_job_id, security_id)):
        raise ValueError("收藏候选缺少精确 BOSS 标识")
    expected_binding = {
        "candidate_id": candidate_id,
        "encrypt_geek_id": encrypt_geek_id,
        "encrypt_job_id": encrypt_job_id,
        "security_id": security_id,
    }
    for operation, method, endpoint_name in (
        (write_operation, "POST", "favorite_candidate"),
        (verify_operation, "GET", "favorite_status"),
    ):
        binding = operation.get("binding")
        if (
            operation.get("method") != method
            or operation.get("endpoint_name") != endpoint_name
            or not isinstance(binding, Mapping)
            or any(binding.get(field) != value for field, value in expected_binding.items())
        ):
            raise ValueError(f"候选人 {candidate_id} 收藏 operation 无效")

    try:
        with client.operation(str(write_operation["operation_key"])):
            write_response = client.favorite_candidate(
                encrypt_geek_id=encrypt_geek_id,
                security_id=security_id,
            )
    except Exception as exc:  # noqa: BLE001 - write outcome classification is fail closed
        result = {
            "status": (
                "favorite_failed"
                if bool(getattr(exc, "definite_rejection", False))
                else "favorite_unknown"
            ),
            "reason": str(getattr(exc, "outcome", None) or type(exc).__name__),
        }
        for field in ("http_status", "response_code"):
            value = getattr(exc, field, None)
            if value is not None:
                result[field] = value
        return result
    if not isinstance(write_response, Mapping) or write_response.get("code") != 0:
        return {
            "status": "favorite_failed",
            "reason": "write_rejected",
            "response_code": write_response.get("code")
            if isinstance(write_response, Mapping)
            else None,
        }

    try:
        with client.operation(str(verify_operation["operation_key"])):
            readback = client.favorite_status(
                encrypt_geek_id=encrypt_geek_id,
                encrypt_job_id=encrypt_job_id,
                security_id=security_id,
            )
    except Exception as exc:  # noqa: BLE001 - successful write without readback is unknown
        return {
            "status": "favorite_unknown",
            "reason": str(getattr(exc, "outcome", None) or type(exc).__name__),
        }
    data = readback.get("zpData") if isinstance(readback, Mapping) else None
    if isinstance(data, Mapping) and data.get("alreadyInterested") == 1:
        return {"status": "favorite_confirmed"}
    return {"status": "favorite_unknown", "reason": "readback_not_authoritative"}
