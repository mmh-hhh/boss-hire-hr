"""Read-only, per-run supply and scoring reports.

The report deliberately reads only frozen workflow artifacts and the local
inventory.  It is not a workflow entry point: it never creates a BOSS client
or an LLM client.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from boss_hire.boss_live_authorization import favorite_account_state_dir
from boss_hire.favorite_registry import read_known_candidate_ids
from boss_hire.ranking_contract import ranking_key
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory
from boss_hire.workflow_run import LoadedWorkflowRun, WorkflowPaths, load_workflow_run


REPORT_CONTRACT = "boss_hire_single_job_run_report"
_BANDS = (("lt_20", None, 20), ("20_to_40", 20, 40), ("40_to_60", 40, 60), ("gte_60", 60, None))


def _object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} 不允许使用符号链接")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 {label}：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return value


def _contained_file(root: Path, path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} 不允许使用符号链接")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} 不存在：{path}") from exc
    if resolved.parent != root and root not in resolved.parents:
        raise ValueError(f"{label} 越出运行目录")
    return resolved


def _artifact(run: LoadedWorkflowRun, name: str, *, required: bool = True) -> dict[str, Any] | None:
    reference = run.state["artifacts"].get(name)
    if reference is None:
        if required:
            raise ValueError(f"运行缺少 {name} 产物")
        return None
    if not isinstance(reference, Mapping):
        raise ValueError(f"运行 {name} 产物引用无效")
    relative = str(reference.get("path") or "").strip()
    if not relative:
        raise ValueError(f"运行 {name} 产物路径无效")
    root = run.state_path.parent.resolve(strict=True)
    artifact = _object(_contained_file(root, root / relative, f"运行产物 {name}"), f"运行产物 {name}")
    if content_hash(artifact) != reference.get("digest"):
        raise ValueError(f"运行产物 {name} 摘要不一致")
    return artifact


def _candidate_ids_for_job(state: Mapping[str, Any], job_id: str) -> set[str]:
    result: set[str] = set()
    for candidate_id, candidate in (state.get("candidates") or {}).items():
        if not isinstance(candidate, Mapping):
            continue
        if any(
            str(card.get("encryptJobId") or card.get("encrypt_job_id") or "").strip() == job_id
            for cards in (candidate.get("sources") or {}).values()
            if isinstance(cards, list)
            for card in cards
            if isinstance(card, Mapping)
        ):
            result.add(str(candidate_id))
    return result


def _valid_scores(state: Mapping[str, Any], job_id: str, rubric_version: str) -> dict[str, dict[str, Any]]:
    values = (state.get("evaluations") or {}).get(job_id, {})
    if not isinstance(values, Mapping):
        raise ValueError("本地库存 evaluations 无效")
    return {
        str(candidate_id): dict(evaluation)
        for candidate_id, evaluation in values.items()
        if isinstance(evaluation, Mapping) and evaluation.get("rubric_version") == rubric_version
    }


def _id_list(value: Any, label: str) -> set[str]:
    if not isinstance(value, list) or any(not str(item).strip() for item in value):
        raise ValueError(f"{label} 必须是非空 ID 列表")
    ids = {str(item) for item in value}
    if len(ids) != len(value):
        raise ValueError(f"{label} 不允许重复 ID")
    return ids


def _source_ids(source: Mapping[str, Any]) -> set[str]:
    ids = _id_list(
        [
            row.get("candidate_id") if isinstance(row, Mapping) else ""
            for row in source.get("candidates") or []
        ],
        "source_collection.candidates",
    )
    if len(ids) != int(source.get("candidate_count") or 0):
        raise ValueError("source_collection candidate_count 与候选人不一致")
    return ids


def _viewed_counts(source: Mapping[str, Any], candidate_ids: set[str]) -> dict[str, int]:
    counts = {"viewed_true": 0, "viewed_false": 0, "viewed_unknown": 0}
    for row in source.get("candidates") or []:
        if not isinstance(row, Mapping) or str(row.get("candidate_id") or "") not in candidate_ids:
            continue
        card = row.get("card")
        if not isinstance(card, Mapping) or not isinstance(card.get("viewed"), bool):
            counts["viewed_unknown"] += 1
        elif card["viewed"]:
            counts["viewed_true"] += 1
        else:
            counts["viewed_false"] += 1
    return counts


def _score_distribution(evaluations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    scores = [float(row["total_score"]) for row in evaluations.values() if isinstance(row.get("total_score"), (int, float)) and not isinstance(row.get("total_score"), bool)]
    bands: dict[str, int] = {}
    for label, lower, upper in _BANDS:
        bands[label] = sum(
            1
            for score in scores
            if (lower is None or score >= lower) and (upper is None or score < upper)
        )
    return {
        "count": len(scores),
        "minimum": min(scores) if scores else None,
        "maximum": max(scores) if scores else None,
        "average": round(statistics.mean(scores), 2) if scores else None,
        "median": round(statistics.median(scores), 2) if scores else None,
        "bands": bands,
    }


def _score_quality(evaluations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    coverage = [
        float(row["evidence_coverage"])
        for row in evaluations.values()
        if isinstance(row.get("evidence_coverage"), (int, float)) and not isinstance(row.get("evidence_coverage"), bool)
    ]
    dimensions: dict[str, dict[str, Any]] = {}
    for evaluation in evaluations.values():
        for row in evaluation.get("dimension_scores") or []:
            if not isinstance(row, Mapping) or not str(row.get("id") or "").strip():
                continue
            dimension = dimensions.setdefault(
                str(row["id"]), {"name": str(row.get("name") or ""), "count": 0, "levels": Counter(), "scores": []}
            )
            dimension["count"] += 1
            dimension["levels"][str(row.get("level") or "unknown")] += 1
            if isinstance(row.get("score"), (int, float)) and not isinstance(row.get("score"), bool):
                dimension["scores"].append(float(row["score"]))
    return {
        "evidence_coverage": {
            "count": len(coverage),
            "average": round(statistics.mean(coverage), 2) if coverage else None,
            "median": round(statistics.median(coverage), 2) if coverage else None,
        },
        "dimensions": [
            {
                "id": dimension_id,
                "name": value["name"],
                "count": value["count"],
                "level_counts": dict(sorted(value["levels"].items())),
                "average_score": round(statistics.mean(value["scores"]), 2) if value["scores"] else None,
            }
            for dimension_id, value in sorted(dimensions.items())
        ],
    }


def _attempt_summaries(run: LoadedWorkflowRun, *, job_id: str, rubric_version: str) -> list[dict[str, Any]]:
    root = run.state_path.parent.resolve(strict=True)
    attempts_root = root / "scoring" / "attempts"
    if not attempts_root.exists():
        return []
    if attempts_root.is_symlink() or not attempts_root.is_dir():
        raise ValueError("评分 attempts 目录无效")
    summaries: list[dict[str, Any]] = []
    for attempt in sorted(attempts_root.iterdir(), key=lambda item: item.name):
        if attempt.is_symlink() or not attempt.is_dir():
            raise ValueError("评分 attempt 目录无效")
        summary_path = attempt / "score_summary.json"
        if not summary_path.exists():
            receipt = _object(
                _contained_file(root, attempt / "confirmation_receipt.json", f"评分 attempt {attempt.name} 确认回执"),
                f"评分 attempt {attempt.name} 确认回执",
            )
            if (
                receipt.get("contract") != "boss_hire_llm_confirmation_receipt"
                or receipt.get("status") != "confirmed"
                or receipt.get("job_id") != job_id
                or receipt.get("rubric_version") != rubric_version
            ):
                raise ValueError(f"评分 attempt {attempt.name} 确认回执与本运行不一致")
            summaries.append(
                {
                    "attempt_id": attempt.name,
                    "status": "interrupted",
                    "summary": None,
                    "receipt": receipt,
                    "scored_ids": set(),
                    "failed_ids": set(),
                }
            )
            continue
        path = _contained_file(root, summary_path, f"评分 attempt {attempt.name}")
        summary = _object(path, f"评分 attempt {attempt.name}")
        if (
            summary.get("contract") != "local_candidate_scoring"
            or summary.get("job_id") != job_id
            or summary.get("rubric_version") != rubric_version
        ):
            raise ValueError(f"评分 attempt {attempt.name} 与本运行不一致")
        scored_ids = _id_list(summary.get("scored_candidate_ids"), f"评分 attempt {attempt.name}.scored_candidate_ids")
        failures = summary.get("failures")
        if not isinstance(failures, list):
            raise ValueError(f"评分 attempt {attempt.name}.failures 必须是列表")
        failed_ids = _id_list([row.get("candidate_id") if isinstance(row, Mapping) else "" for row in failures], f"评分 attempt {attempt.name}.failures") if failures else set()
        summaries.append(
            {
                "attempt_id": attempt.name,
                "status": "recorded",
                "summary": summary,
                "receipt": None,
                "scored_ids": scored_ids,
                "failed_ids": failed_ids,
            }
        )
    return summaries


def _observation_rows(
    source: Mapping[str, Any],
    source_ids: set[str],
) -> list[tuple[Mapping[str, Any], set[str], int]] | None:
    observations = source.get("source_observations")
    if not isinstance(observations, Mapping):
        return None
    pages = observations.get("pages")
    if not isinstance(pages, list):
        raise ValueError("source_collection.source_observations.pages 无效")
    raw_total = 0
    unique_total = 0
    union_ids: set[str] = set()
    rows: list[tuple[Mapping[str, Any], set[str], int]] = []
    for page in pages:
        if not isinstance(page, Mapping):
            raise ValueError("source_collection.source_observations.pages 条目无效")
        source_name = page.get("source")
        page_number = page.get("page", 1)
        route_id = page.get("route_id")
        if page_number not in {1, 2} or isinstance(page_number, bool):
            raise ValueError("source_collection.source_observations page 无效")
        if source_name == "recommendation":
            if page_number != 1 or route_id is not None:
                raise ValueError("source_collection recommendation observation 无效")
        elif source_name == "search":
            if not str(route_id or "").strip():
                raise ValueError("source_collection search observation 缺少 route_id")
        else:
            raise ValueError("source_collection observation source 无效")
        ids = (
            _id_list(
                page.get("page_unique_candidate_ids"),
                "source_observations.page_unique_candidate_ids",
            )
            if page.get("page_unique_candidate_ids")
            else set()
        )
        raw_count = page.get("raw_row_count")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < len(ids):
            raise ValueError("source_collection.source_observations 原始行数无效")
        raw_total += raw_count
        unique_total += len(ids)
        union_ids.update(ids)
        rows.append((page, ids, raw_count))
    recorded_overlap = observations.get(
        "cross_observation_overlap_count",
        observations.get("cross_first_page_overlap_count"),
    )
    if (
        observations.get("page_count") != len(pages)
        or observations.get("raw_row_count") != raw_total
        or observations.get("page_unique_candidate_count") != unique_total
        or recorded_overlap != unique_total - len(source_ids)
        or union_ids != source_ids
    ):
        raise ValueError("source_collection.source_observations 汇总不一致")
    return rows


def _search_page1_ids(source: Mapping[str, Any]) -> set[str]:
    source_ids = _source_ids(source)
    rows = _observation_rows(source, source_ids)
    if rows is None:
        raise ValueError("来源对照需要带逐页证据的 source_collection")
    return set().union(
        *(
            ids
            for page, ids, _ in rows
            if page.get("source") == "search" and page.get("page", 1) == 1
        )
    )


def _source_comparison(
    *,
    current_run: LoadedWorkflowRun,
    current_source: Mapping[str, Any],
    baseline_run: LoadedWorkflowRun,
    baseline_source: Mapping[str, Any],
) -> dict[str, Any]:
    if current_run.state["job_id"] != baseline_run.state["job_id"]:
        raise ValueError("来源对照运行必须属于同一岗位")
    current_plan = _artifact(current_run, "source_plan") or {}
    baseline_plan = _artifact(baseline_run, "source_plan") or {}
    current_routes = current_plan.get("selected_search_routes") or []
    baseline_routes = baseline_plan.get("selected_search_routes") or []
    if not current_routes:
        raise ValueError("来源对照运行必须包含搜索路线")
    if current_routes != baseline_routes:
        raise ValueError("来源对照运行必须冻结相同搜索路线")
    current_ids = _search_page1_ids(current_source)
    baseline_ids = _search_page1_ids(baseline_source)
    current_only = current_ids - baseline_ids
    baseline_viewed_true = {
        str(row.get("candidate_id"))
        for row in baseline_source.get("candidates") or []
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or "") in baseline_ids
        and isinstance(row.get("card"), Mapping)
        and row["card"].get("viewed") is True
    }
    baseline_viewed_false = {
        str(row.get("candidate_id"))
        for row in baseline_source.get("candidates") or []
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or "") in baseline_ids
        and isinstance(row.get("card"), Mapping)
        and row["card"].get("viewed") is False
    }
    return {
        "status": "recorded",
        "baseline_run_id": baseline_run.state["run_id"],
        "current_recent_view_filter": current_source.get("recent_view_filter", "include_all"),
        "baseline_recent_view_filter": baseline_source.get("recent_view_filter", "include_all"),
        "current_page1_unique": len(current_ids),
        "baseline_page1_unique": len(baseline_ids),
        "page1_overlap": len(current_ids & baseline_ids),
        "current_only": len(current_only),
        "baseline_only": len(baseline_ids - current_ids),
        "overlap_with_baseline_viewed_true": len(current_ids & baseline_viewed_true),
        "overlap_with_baseline_viewed_false": len(current_ids & baseline_viewed_false),
        "baseline_viewed": _viewed_counts(baseline_source, baseline_ids),
        "current_viewed": _viewed_counts(current_source, current_ids),
        "current_only_viewed": _viewed_counts(current_source, current_only),
    }


def _supply_section(run: LoadedWorkflowRun, source: Mapping[str, Any], detail: Mapping[str, Any] | None, inventory_state: Mapping[str, Any]) -> dict[str, Any]:
    source_ids = _source_ids(source)
    source_plan = _artifact(run, "source_plan") or {}
    route_queries = {
        str(row.get("id")): str(row.get("query") or "")
        for row in source_plan.get("selected_search_routes") or []
        if isinstance(row, Mapping) and str(row.get("id") or "").strip()
    }
    route_memberships: Counter[str] = Counter()
    route_exclusive: Counter[str] = Counter()
    recommendation_memberships = 0
    for row in source.get("candidates") or []:
        if not isinstance(row, Mapping):
            continue
        route_ids = [str(item) for item in row.get("search_route_ids") or [] if str(item).strip()]
        route_memberships.update(route_ids)
        if len(route_ids) == 1:
            route_exclusive[route_ids[0]] += 1
        if "recommendation" in (row.get("sources") or []):
            recommendation_memberships += 1

    baseline = source.get("inventory_baseline")
    if isinstance(baseline, Mapping) and isinstance(baseline.get("known_job_candidate_ids_before_source"), list):
        before_ids = _id_list(baseline["known_job_candidate_ids_before_source"], "inventory_baseline.known_job_candidate_ids_before_source") if baseline["known_job_candidate_ids_before_source"] else set()
        new_ids = source_ids - before_ids
        inventory_baseline = {"job_inventory_before_source": len(before_ids), "job_inventory_after_source": len(before_ids | source_ids), "new_to_local_inventory": len(new_ids), "prior_known_in_source": len(source_ids & before_ids), "status": "recorded"}
    else:
        before_ids = set()
        new_ids = set()
        inventory_baseline = {"job_inventory_before_source": "unavailable", "job_inventory_after_source": "unavailable", "new_to_local_inventory": "unavailable", "prior_known_in_source": "unavailable", "status": "unavailable_legacy_source_artifact"}

    page_rows = _observation_rows(source, source_ids)
    if page_rows is not None:
        page_id_sets = [ids for _, ids, _ in page_rows]
        route_rows = [
            {
                "source": page.get("source"), "route_id": page.get("route_id"),
                "query": page.get("query") or (route_queries.get(str(page["route_id"])) if page.get("route_id") is not None else None),
                "page": page.get("page", 1),
                "raw_page_rows": raw_count, "page_local_unique": len(ids),
                "cross_observation_unique_membership": len(ids),
                "exclusive_contribution": sum(
                    1
                    for candidate_id in ids
                    if sum(candidate_id in page_ids for page_ids in page_id_sets) == 1
                ),
            }
            for page, ids, raw_count in page_rows
        ]
        observations = source["source_observations"]
        measurements = {
            "raw_page_rows": observations.get("raw_row_count"),
            "page_local_unique_memberships": observations.get("page_unique_candidate_count"),
            "cross_observation_overlap": observations.get(
                "cross_observation_overlap_count",
                observations.get("cross_first_page_overlap_count"),
            ),
            "unique_in_run": len(source_ids),
            "measurement_status": "recorded",
        }
        page1_ids = set().union(*(ids for page, ids, _ in page_rows if page.get("page", 1) == 1))
        page2_ids = set().union(*(ids for page, ids, _ in page_rows if page.get("page", 1) == 2))
        own_page1_by_route = {
            str(page.get("route_id")): ids
            for page, ids, _ in page_rows
            if page.get("source") == "search" and page.get("page", 1) == 1
        }
        own_page1_overlap: set[str] = set()
        route_candidate_ids: dict[str, set[str]] = {}
        for page, ids, _ in page_rows:
            route_id = str(page.get("route_id") or "").strip()
            if page.get("source") != "search" or not route_id:
                continue
            route_candidate_ids.setdefault(route_id, set()).update(ids)
            if page.get("page", 1) == 2:
                own_page1_overlap.update(ids & own_page1_by_route.get(route_id, set()))
        page2_incremental_ids = page2_ids - page1_ids
        page_summary = {
            "status": "recorded" if page2_ids else "not_requested",
            "page1_unique": len(page1_ids),
            "page2_unique": len(page2_ids),
            "page2_overlap_with_own_page1": len(own_page1_overlap),
            "page2_overlap_with_any_page1": len(page2_ids & page1_ids),
            "page2_incremental_to_all_page1": len(page2_incremental_ids),
            "page2_new_to_local_inventory": len(page2_ids - before_ids) if inventory_baseline["status"] == "recorded" else "unavailable",
            "page2_incremental_new_to_local_inventory": len(page2_incremental_ids - before_ids) if inventory_baseline["status"] == "recorded" else "unavailable",
        }
        candidate_route_counts = Counter(
            candidate_id
            for ids in route_candidate_ids.values()
            for candidate_id in ids
        )
        route_order = list(route_queries)
        route_order.extend(sorted(set(route_candidate_ids) - set(route_order)))
        route_contributions = []
        for route_id in route_order:
            ids = route_candidate_ids.get(route_id, set())
            route_new_ids = ids & new_ids
            page1_route_ids = set().union(*(
                page_ids
                for page, page_ids, _ in page_rows
                if page.get("source") == "search"
                and str(page.get("route_id") or "") == route_id
                and page.get("page", 1) == 1
            ))
            page2_route_ids = set().union(*(
                page_ids
                for page, page_ids, _ in page_rows
                if page.get("source") == "search"
                and str(page.get("route_id") or "") == route_id
                and page.get("page", 1) == 2
            ))
            route_contributions.append(
                {
                    "route_id": route_id,
                    "query": route_queries.get(route_id),
                    "page1_unique": len(page1_route_ids),
                    "page2_unique": len(page2_route_ids),
                    "unique_membership": len(ids),
                    "new_to_local_inventory": len(route_new_ids) if inventory_baseline["status"] == "recorded" else "unavailable",
                    "exclusive_new_to_local_inventory": sum(
                        candidate_route_counts[candidate_id] == 1 for candidate_id in route_new_ids
                    ) if inventory_baseline["status"] == "recorded" else "unavailable",
                    "shared_new_to_local_inventory_credit": round(
                        sum(1 / candidate_route_counts[candidate_id] for candidate_id in route_new_ids),
                        6,
                    ) if inventory_baseline["status"] == "recorded" else "unavailable",
                    "page2_incremental_to_all_page1": len(page2_route_ids - page1_ids),
                    "page2_incremental_new_to_local_inventory": len((page2_route_ids - page1_ids) - before_ids) if inventory_baseline["status"] == "recorded" else "unavailable",
                }
            )
    else:
        route_rows = [
            {"source": "search", "route_id": route_id, "query": route_queries.get(route_id), "page": "unavailable", "raw_page_rows": "unavailable", "page_local_unique": count, "cross_observation_unique_membership": count, "exclusive_contribution": route_exclusive[route_id]}
            for route_id, count in sorted(route_memberships.items())
        ]
        if recommendation_memberships:
            route_rows.append({"source": "recommendation", "route_id": None, "query": None, "page": "unavailable", "raw_page_rows": "unavailable", "page_local_unique": recommendation_memberships, "cross_observation_unique_membership": recommendation_memberships, "exclusive_contribution": "unavailable"})
        memberships = sum(route_memberships.values()) + recommendation_memberships
        measurements = {"raw_page_rows": "unavailable", "page_local_unique_memberships": memberships, "cross_observation_overlap": memberships - len(source_ids), "unique_in_run": len(source_ids), "measurement_status": "legacy_derived_membership"}
        page_summary = {"status": "unavailable_legacy_source_artifact"}
        route_contributions = []

    detail_section: dict[str, Any] = {"status": "not_completed"}
    if detail is not None:
        fetched = _id_list(detail.get("fetched_candidate_ids"), "detail_collection.fetched_candidate_ids") if detail.get("fetched_candidate_ids") else set()
        cached = _id_list(detail.get("cached_candidate_ids"), "detail_collection.cached_candidate_ids") if detail.get("cached_candidate_ids") else set()
        selected = int(detail.get("selected_count") or 0)
        detail_section = {"selected": selected, "fetched": len(fetched), "cached": len(cached), "not_completed": max(0, selected - len(fetched) - len(cached)), "fetched_from_new_local_supply": len(fetched & new_ids) if inventory_baseline["status"] == "recorded" else "unavailable", "status": "recorded"}
    return {"plan": {"recommendation_source_enabled": source.get("recommendation_source_enabled"), "requested_search_query_count": source.get("requested_search_query_count"), "selected_search_query_count": source.get("selected_search_query_count"), "search_query_shortfall": source.get("search_query_shortfall"), "requested_second_page_search_query_count": source.get("requested_second_page_search_query_count", 0), "selected_second_page_search_query_count": source.get("selected_second_page_search_query_count", 0), "second_page_search_query_shortfall": source.get("second_page_search_query_shortfall", 0), "recent_view_filter": source.get("recent_view_filter", "include_all"), "search_filters": source.get("search_filters") or [], "search_filter_params": source.get("search_filter_params") or {}}, "routes": route_rows, "route_contributions": route_contributions, "source_funnel": measurements, "viewed": _viewed_counts(source, source_ids), "page_summary": page_summary, "inventory_baseline": inventory_baseline, "details": detail_section, "current_job_inventory_count": len(_candidate_ids_for_job(inventory_state, run.state["job_id"]))}


def _score_section(run: LoadedWorkflowRun, source: Mapping[str, Any], inventory_state: Mapping[str, Any]) -> dict[str, Any]:
    attempts = _attempt_summaries(run, job_id=run.state["job_id"], rubric_version=run.state["rubric_version"])
    successful_ids = set().union(*(row["scored_ids"] for row in attempts)) if attempts else set()
    failed_ids = set().union(*(row["failed_ids"] for row in attempts)) if attempts else set()
    successful_evaluations = _valid_scores(inventory_state, run.state["job_id"], run.state["rubric_version"])
    baseline = source.get("inventory_baseline")
    baseline_reuse: int | str = "unavailable"
    source_ids = _id_list([row.get("candidate_id") if isinstance(row, Mapping) else "" for row in source.get("candidates") or []], "source_collection.candidates")
    baseline_ids: set[str] | None = None
    if isinstance(baseline, Mapping) and baseline.get("rubric_version") == run.state["rubric_version"] and isinstance(baseline.get("valid_score_candidate_ids_before_source"), list):
        baseline_ids = _id_list(baseline["valid_score_candidate_ids_before_source"], "inventory_baseline.valid_score_candidate_ids_before_source") if baseline["valid_score_candidate_ids_before_source"] else set()
        baseline_reuse = len(source_ids & baseline_ids)
    if baseline_ids is None:
        new_evaluations = {
            candidate_id: successful_evaluations[candidate_id]
            for candidate_id in successful_ids
            if candidate_id in successful_evaluations
        }
    else:
        new_evaluations = {
            candidate_id: evaluation
            for candidate_id, evaluation in successful_evaluations.items()
            if candidate_id in source_ids and candidate_id not in baseline_ids
        }
    missing_current = sorted(successful_ids - set(successful_evaluations))
    page2_incremental_new_ids: set[str] = set()
    page_rows = _observation_rows(source, source_ids)
    if page_rows is not None and isinstance(baseline, Mapping) and isinstance(baseline.get("known_job_candidate_ids_before_source"), list):
        before_ids = _id_list(baseline["known_job_candidate_ids_before_source"], "inventory_baseline.known_job_candidate_ids_before_source") if baseline["known_job_candidate_ids_before_source"] else set()
        page1_ids = set().union(*(ids for page, ids, _ in page_rows if page.get("page", 1) == 1))
        page2_ids = set().union(*(ids for page, ids, _ in page_rows if page.get("page", 1) == 2))
        page2_incremental_new_ids = (page2_ids - page1_ids) - before_ids
    page2_incremental_evaluations = {
        candidate_id: successful_evaluations[candidate_id]
        for candidate_id in page2_incremental_new_ids
        if candidate_id in successful_evaluations
    }
    ordered_pool = sorted(
        successful_evaluations,
        key=lambda candidate_id: ranking_key(dict(successful_evaluations[candidate_id]), candidate_id),
    )
    return {
        "attempt_health": {
            "attempt_count": len(attempts),
            "interrupted_attempt_count": sum(row["status"] == "interrupted" for row in attempts),
            "models": sorted({str((row["summary"] or row["receipt"]).get("llm_model") or "") for row in attempts}),
            "workers": sorted({(row["summary"] or row["receipt"]).get("workers") for row in attempts}),
            "invocation_attempted": sum(int(row["summary"].get("attempted_count") or 0) for row in attempts if row["summary"] is not None),
            "invocation_succeeded": sum(int(row["summary"].get("scored_count") or 0) for row in attempts if row["summary"] is not None),
            "invocation_failed": sum(int(row["summary"].get("failed_count") or 0) for row in attempts if row["summary"] is not None),
            "unique_newly_successful": len(successful_ids),
            "current_newly_persisted": len(new_evaluations),
            "unique_failed_without_success": len(failed_ids - successful_ids),
            "new_score_missing_from_current_inventory": missing_current,
            "attempts": [
                {
                    "attempt_id": row["attempt_id"],
                    "status": row["status"],
                    "model": (row["summary"] or row["receipt"]).get("llm_model"),
                    "workers": (row["summary"] or row["receipt"]).get("workers"),
                    "attempted": row["summary"].get("attempted_count") if row["summary"] is not None else "unavailable_interrupted",
                    "succeeded": row["summary"].get("scored_count") if row["summary"] is not None else "unavailable_interrupted",
                    "failed": row["summary"].get("failed_count") if row["summary"] is not None else "unavailable_interrupted",
                    "backpressure_stopped": row["summary"].get("backpressure_stopped") if row["summary"] is not None else "unavailable_interrupted",
                }
                for row in attempts
            ],
        },
        "baseline_score_reuse_in_source": baseline_reuse,
        "new_scores": {"distribution": _score_distribution(new_evaluations), "quality": _score_quality(new_evaluations)},
        "page2_incremental_new_scores": {
            "candidate_count": len(page2_incremental_new_ids),
            "scored_count": len(page2_incremental_evaluations),
            "distribution": _score_distribution(page2_incremental_evaluations),
            "quality": _score_quality(page2_incremental_evaluations),
            "ranking_impact": {
                "top10_count": len(set(ordered_pool[:10]) & page2_incremental_new_ids),
                "top20_count": len(set(ordered_pool[:20]) & page2_incremental_new_ids),
                "scored_pool_count": len(ordered_pool),
            },
            "status": "recorded" if page_rows is not None else "unavailable_legacy_source_artifact",
        },
        "pool_scores": {"distribution": _score_distribution(successful_evaluations), "quality": _score_quality(successful_evaluations)},
        "_new_evaluations": new_evaluations,
    }


def _local_favorite_status(candidate: Mapping[str, Any], favorite_candidate_ids: frozenset[str]) -> str:
    source_ids = {
        str(card.get("encryptGeekId") or card.get("encrypt_geek_id") or "").strip()
        for cards in (candidate.get("sources") or {}).values()
        if isinstance(cards, list)
        for card in cards
        if isinstance(card, Mapping)
        and str(card.get("encryptGeekId") or card.get("encrypt_geek_id") or "").strip()
    }
    if len(source_ids) != 1:
        return "unknown_unmapped" if not source_ids else "unknown_ambiguous"
    return "favorited_local" if next(iter(source_ids)) in favorite_candidate_ids else "not_recorded_local"


def _candidate_rows(
    evaluations: Mapping[str, Mapping[str, Any]],
    inventory_state: Mapping[str, Any],
    limit: int,
    *,
    favorite_candidate_ids: frozenset[str],
) -> list[dict[str, Any]]:
    candidates = inventory_state.get("candidates") or {}
    ordered = sorted(evaluations, key=lambda candidate_id: ranking_key(dict(evaluations[candidate_id]), candidate_id))[:limit]
    result = []
    for candidate_id in ordered:
        candidate = candidates.get(candidate_id) if isinstance(candidates, Mapping) else {}
        source_cards = [card for cards in (candidate.get("sources") or {}).values() if isinstance(cards, list) for card in cards if isinstance(card, Mapping)] if isinstance(candidate, Mapping) else []
        card = source_cards[0] if source_cards else {}
        evaluation = evaluations[candidate_id]
        dimensions = [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "level": row.get("level"),
                "score": row.get("score"),
                "weight": row.get("weight"),
                "critical": row.get("critical"),
            }
            for row in evaluation.get("dimension_scores") or []
            if isinstance(row, Mapping)
        ]
        result.append(
            {
                "candidate_id": candidate_id,
                "total_score": evaluation.get("total_score"),
                "evidence_coverage": evaluation.get("evidence_coverage"),
                "dimension_scores": dimensions,
                "current_title": card.get("current_title"),
                "work_years": card.get("work_years"),
                "degree": card.get("degree"),
                "local_favorite_status": _local_favorite_status(candidate, favorite_candidate_ids)
                if isinstance(candidate, Mapping)
                else "unknown_unmapped",
            }
        )
    return result


def build_single_job_report(
    *,
    paths: WorkflowPaths,
    run_id: str,
    candidates: int | None = None,
    candidate_scope: str = "new",
    include_candidates: bool = False,
    favorite_registry_root: Path | None = None,
    compare_source_run_id: str | None = None,
) -> dict[str, Any]:
    if candidates is not None and candidates <= 0:
        raise ValueError("candidates 必须是正整数")
    if candidate_scope not in {"new", "pool"}:
        raise ValueError("candidate_scope 必须是 new 或 pool")
    run = load_workflow_run(paths, run_id)
    source = _artifact(run, "source_collection")
    detail = _artifact(run, "detail_collection", required=False)
    if source is None or source.get("contract") != "single_job_source_collection":
        raise ValueError("source_collection 契约无效")
    if detail is not None and detail.get("contract") != "candidate_detail_collection":
        raise ValueError("detail_collection 契约无效")
    inventory_state = CandidateInventory.load(paths.inventory_path).to_dict()
    registry_root = (
        Path(favorite_registry_root)
        if favorite_registry_root is not None
        else favorite_account_state_dir(str(run.state["account_key"]))
    )
    favorite_candidate_ids = read_known_candidate_ids(
        registry_root,
        account_key=str(run.state["account_key"]),
    )
    scores = _score_section(run, source, inventory_state)
    pool_evaluations = _valid_scores(inventory_state, run.state["job_id"], run.state["rubric_version"])
    report = {"schema_version": 1, "contract": REPORT_CONTRACT, "run_id": run.state["run_id"], "job_id": run.state["job_id"], "rubric_version": run.state["rubric_version"], "status": run.state["status"], "supply": _supply_section(run, source, detail, inventory_state), "scores": {key: value for key, value in scores.items() if key != "_new_evaluations"}, "favorite_status_source": {"kind": "local_registry_only", "boss_sync_performed": False, "registered_candidate_count": len(favorite_candidate_ids)}, "boss_requests": 0, "llm_requests": 0}
    if compare_source_run_id is not None:
        baseline_run = load_workflow_run(paths, compare_source_run_id)
        baseline_source = _artifact(baseline_run, "source_collection")
        if baseline_source is None or baseline_source.get("contract") != "single_job_source_collection":
            raise ValueError("对照运行 source_collection 契约无效")
        report["source_comparison"] = _source_comparison(
            current_run=run,
            current_source=source,
            baseline_run=baseline_run,
            baseline_source=baseline_source,
        )
    if candidates is not None or include_candidates:
        evaluations = scores["_new_evaluations"] if candidate_scope == "new" else pool_evaluations
        report["candidate_scope"] = "new_scores" if candidate_scope == "new" else "same_job_rubric_pool"
        report["candidates"] = _candidate_rows(
            evaluations,
            inventory_state,
            len(evaluations) if candidates is None else candidates,
            favorite_candidate_ids=favorite_candidate_ids,
        )
    return report


def format_single_job_report(report: Mapping[str, Any], *, section: str) -> str:
    lines = [f"运行 {report['run_id']} · {report['status']}"]
    if section in {"all", "supply"}:
        supply = report["supply"]
        funnel = supply["source_funnel"]
        page_summary = supply["page_summary"]
        baseline = supply["inventory_baseline"]
        details = supply["details"]
        viewed = supply["viewed"]
        lines.extend(["供给漏斗", f"过滤：{supply['plan']['recent_view_filter']}；卡片 viewed=true {viewed['viewed_true']}、false {viewed['viewed_false']}、未知 {viewed['viewed_unknown']}", f"来源：各页原始行 {funnel['raw_page_rows']}；页内去重后观察 {funnel['page_local_unique_memberships']}；跨页面/路线重叠 {funnel['cross_observation_overlap']}；本轮唯一 {funnel['unique_in_run']}", f"本地库存：本轮前 {baseline['job_inventory_before_source']}；新增供给 {baseline['new_to_local_inventory']}；已存在重叠 {baseline['prior_known_in_source']}", f"详情：选择 {details.get('selected', 0)}；新抓取 {details.get('fetched', 0)}；缓存复用 {details.get('cached', 0)}；未完成 {details.get('not_completed', 0)}"])
        if supply["plan"]["search_filters"]:
            lines.append(
                "搜索条件："
                + "；".join(
                    f"{row['field_label']}={','.join(row['option_labels'])}"
                    for row in supply["plan"]["search_filters"]
                )
            )
        if page_summary.get("status") == "recorded":
            lines.append(
                f"第二页：唯一 {page_summary['page2_unique']}；与任一首页重叠 {page_summary['page2_overlap_with_any_page1']}；"
                f"相对全部首页增量 {page_summary['page2_incremental_to_all_page1']}；其中库存新候选 {page_summary['page2_incremental_new_to_local_inventory']}"
            )
        comparison = report.get("source_comparison")
        if isinstance(comparison, Mapping):
            lines.append(
                f"首页对照：基线 {comparison['baseline_page1_unique']}，本轮 {comparison['current_page1_unique']}；"
                f"重叠 {comparison['page1_overlap']}，本轮替换新增 {comparison['current_only']}；"
                f"仍命中基线 viewed=true {comparison['overlap_with_baseline_viewed_true']}、"
                f"viewed=false {comparison['overlap_with_baseline_viewed_false']}"
            )
    if section in {"all", "scores"}:
        scores = report["scores"]
        health = scores["attempt_health"]
        new = scores["new_scores"]["distribution"]
        page2 = scores["page2_incremental_new_scores"]
        pool = scores["pool_scores"]["distribution"]
        lines.extend(["评分", f"调用：attempt {health['attempt_count']}；中断 {health['interrupted_attempt_count']}；有总结成功 {health['invocation_succeeded']}；有总结失败 {health['invocation_failed']}；当前持久化新评分 {health['current_newly_persisted']}", f"本轮新评分：{new['count']} 人，范围 {new['minimum']}–{new['maximum']}，均值 {new['average']}，中位数 {new['median']}，分段 {new['bands']}", f"同 JD/rubric 总池：{pool['count']} 人，范围 {pool['minimum']}–{pool['maximum']}，均值 {pool['average']}，中位数 {pool['median']}，分段 {pool['bands']}"])
        if page2.get("status") == "recorded" and page2["candidate_count"]:
            distribution = page2["distribution"]
            impact = page2["ranking_impact"]
            lines.append(
                f"第二页净新增评分：{page2['scored_count']}/{page2['candidate_count']} 人，"
                f"中位数 {distribution['median']}；进入总池 Top10 {impact['top10_count']} 人、Top20 {impact['top20_count']} 人"
            )
    if report.get("candidates"):
        lines.append(
            str(report.get("candidate_heading") or "")
            or ("本轮新评分 Top：" if report.get("candidate_scope") == "new_scores" else "同 JD/rubric 总池 Top：")
        )
        favorite_labels = {
            "favorited_local": "本地收藏注册表：已收藏",
            "not_recorded_local": "本地收藏注册表：未记录",
            "unknown_unmapped": "本地收藏注册表：无法映射",
            "unknown_ambiguous": "本地收藏注册表：映射不唯一",
        }
        lines.extend(
            f"{index}. {row['total_score']} · {row.get('current_title') or '未提供职位'} · "
            f"{row.get('work_years') or '未提供年限'} · "
            f"{favorite_labels.get(row.get('local_favorite_status'), '本地收藏注册表：未知')}"
            for index, row in enumerate(report["candidates"], 1)
        )
        lines.append("收藏状态仅来自本地注册表快照；本报告未同步或查询 BOSS。")
    lines.append("本报告仅读取本地冻结产物与库存：BOSS 0 次，LLM 0 次。")
    return "\n".join(lines)
