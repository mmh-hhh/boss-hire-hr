from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Collection, Mapping

from boss_hire.local_security import ensure_private_directory, ensure_private_file
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory


SHORTLIST_SIZE = 5
SCHEMA_VERSION = 1
SELECTION_SCOPE_UNDELIVERED = "undelivered"
SELECTION_SCOPE_ALL_EVALUATED = "all_evaluated"
SELECTION_SCOPES = frozenset(
    {SELECTION_SCOPE_UNDELIVERED, SELECTION_SCOPE_ALL_EVALUATED}
)
JSON_FILENAME = "candidate_shortlist.json"
CSV_FILENAME = "candidate_shortlist.csv"
DISPLAY_FIELDS = ("name", "current_title", "company", "work_years", "degree")
BOSS_IDENTIFIER_FIELDS = (
    "encryptGeekId",
    "encryptUid",
    "securityId",
    "lid",
    "encryptJobId",
)
CSV_FIELDS = (
    "batch_id",
    "published_at",
    "job_id",
    "job_title",
    "rank",
    "candidate_id",
    "name",
    "current_title",
    "company",
    "score",
    "evidence_coverage",
    "confidence",
    "summary",
    "evidence",
    "gaps",
    "sources",
)


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _source_cards(sources: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for source in sorted(sources):
        cards = sources[source]
        if not isinstance(cards, list):
            continue
        result.extend(card for card in cards if isinstance(card, Mapping))
    return result


def _candidate_row(row: Mapping[str, Any], rank: int) -> dict[str, Any]:
    evaluation = row["evaluation"]
    sources = row.get("sources") or {}
    cards = _source_cards(sources)
    display: dict[str, Any] = {}
    identifiers: dict[str, Any] = {}
    for card in cards:
        for field in DISPLAY_FIELDS:
            if field not in display and card.get(field) not in (None, ""):
                display[field] = deepcopy(card[field])
        for field in BOSS_IDENTIFIER_FIELDS:
            if (
                field != "securityId"
                and field not in identifiers
                and card.get(field) not in (None, "")
            ):
                identifiers[field] = deepcopy(card[field])
    latest_security_id = next(
        (
            card.get("securityId")
            for card in reversed(cards)
            if card.get("securityId") not in (None, "")
        ),
        None,
    )
    if latest_security_id is not None:
        identifiers["securityId"] = deepcopy(latest_security_id)
    coverage = float(evaluation.get("evidence_coverage") or 0)
    confidence = float(evaluation.get("confidence", coverage) or 0)
    return {
        "candidate_id": row["candidate_id"],
        "rank": rank,
        "score": float(evaluation.get("total_score") or 0),
        "summary": str(evaluation.get("summary") or "").strip(),
        "evidence": deepcopy(evaluation.get("evidence") or []),
        "gaps": deepcopy(evaluation.get("gaps") or []),
        "risks": deepcopy(evaluation.get("risks") or []),
        "evidence_coverage": coverage,
        "confidence": max(0.0, min(100.0, confidence)),
        "dimension_scores": deepcopy(evaluation.get("dimension_scores") or []),
        "sources": sorted(sources),
        "display": display,
        "boss_identifiers": identifiers,
    }


def _favorite_candidate_ids(values: Collection[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合")
    result = frozenset(_text(value, "favorite_candidate_ids") for value in values)
    return result


def _stable_boss_candidate_id(row: Mapping[str, Any]) -> str:
    sources = row.get("sources") or {}
    if not isinstance(sources, Mapping):
        raise ValueError("候选人来源数据无效，无法确认稳定候选人 ID")
    candidate_ids = {
        str(card.get("encryptGeekId") or "").strip()
        for card in _source_cards(sources)
        if str(card.get("encryptGeekId") or "").strip()
    }
    if len(candidate_ids) != 1:
        raise ValueError("候选人必须绑定唯一稳定 encryptGeekId 才能发布")
    return next(iter(candidate_ids))


def build_candidate_shortlist(
    *,
    inventory: CandidateInventory,
    job_id: str,
    job_title: str,
    rubric_version: str,
    published_at: str,
    favorite_candidate_ids: Collection[str] = (),
    favorite_sync_status: str = "not_checked",
    favorite_sync_complete: bool = False,
    favorite_sync_receipt_id: str | None = None,
    selection_scope: str = SELECTION_SCOPE_UNDELIVERED,
) -> dict[str, Any]:
    stable_job_id = _text(job_id, "job_id")
    stable_job_title = _text(job_title, "job_title")
    stable_rubric_version = _text(rubric_version, "rubric_version")
    stable_published_at = _text(published_at, "published_at")
    stable_favorite_ids = _favorite_candidate_ids(favorite_candidate_ids)
    stable_sync_status = _text(favorite_sync_status, "favorite_sync_status")
    stable_receipt_id = str(favorite_sync_receipt_id or "").strip() or None
    stable_selection_scope = _text(selection_scope, "selection_scope")
    if stable_selection_scope not in SELECTION_SCOPES:
        raise ValueError("selection_scope 仅支持 undelivered 或 all_evaluated")
    if not isinstance(favorite_sync_complete, bool):
        raise ValueError("favorite_sync_complete 必须是布尔值")
    if stable_selection_scope == SELECTION_SCOPE_ALL_EVALUATED:
        available_count = inventory.evaluated_count(
            stable_job_id,
            rubric_version=stable_rubric_version,
        )
        selected = inventory.select_evaluated(
            stable_job_id,
            max(SHORTLIST_SIZE, available_count),
            rubric_version=stable_rubric_version,
        )
    else:
        available_count = inventory.undelivered_count(
            stable_job_id,
            rubric_version=stable_rubric_version,
        )
        selected = inventory.select_undelivered(
            stable_job_id,
            max(SHORTLIST_SIZE, available_count),
            rubric_version=stable_rubric_version,
        )
    excluded_candidate_ids: list[str] = []
    eligible_candidates: list[Mapping[str, Any]] = []
    for candidate in selected["candidates"]:
        if _stable_boss_candidate_id(candidate) in stable_favorite_ids:
            excluded_candidate_ids.append(str(candidate["candidate_id"]))
            continue
        eligible_candidates.append(candidate)
    eligible_candidates = eligible_candidates[:SHORTLIST_SIZE]
    candidates = [
        _candidate_row(candidate, rank)
        for rank, candidate in enumerate(eligible_candidates, start=1)
    ]
    identity = {
        "job_id": stable_job_id,
        "rubric_version": stable_rubric_version,
        "published_at": stable_published_at,
        "candidate_ids": [row["candidate_id"] for row in candidates],
        "scores": [row["score"] for row in candidates],
        "favorite_sync_status": stable_sync_status,
        "favorite_sync_complete": favorite_sync_complete,
        "favorite_sync_receipt_id": stable_receipt_id,
        "favorite_registry_excluded_candidate_ids": excluded_candidate_ids,
        "selection_scope": stable_selection_scope,
    }
    shortage_count = SHORTLIST_SIZE - len(candidates)
    exclusion_count = len(excluded_candidate_ids)
    coverage_warning = None if favorite_sync_complete else "favorite_registry_sync_incomplete"
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "candidate_shortlist_batch",
        "batch_id": "shortlist-" + content_hash(identity)[:16],
        "published_at": stable_published_at,
        "job_id": stable_job_id,
        "job_title": stable_job_title,
        "rubric_version": stable_rubric_version,
        "selection_scope": stable_selection_scope,
        "requested_count": SHORTLIST_SIZE,
        "actual_count": len(candidates),
        "shortage_count": shortage_count,
        "shortage_reason": (
            "favorite_registry_exclusion_exhausted_inventory"
            if shortage_count and exclusion_count
            else (
                "evaluated_inventory_exhausted"
                if shortage_count
                and stable_selection_scope == SELECTION_SCOPE_ALL_EVALUATED
                else "undelivered_inventory_exhausted" if shortage_count else None
            )
        ),
        "favorite_registry_excluded_count": exclusion_count,
        "favorite_registry_excluded_candidate_ids": excluded_candidate_ids,
        "favorite_sync_status": stable_sync_status,
        "favorite_sync_complete": favorite_sync_complete,
        "favorite_sync_receipt_id": stable_receipt_id,
        "favorite_coverage_warning": coverage_warning,
        "candidates": candidates,
    }


def _json_text(batch: Mapping[str, Any]) -> str:
    return json.dumps(batch, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _csv_text(batch: Mapping[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for candidate in batch.get("candidates") or []:
        display = candidate.get("display") or {}
        writer.writerow(
            {
                "batch_id": batch.get("batch_id"),
                "published_at": batch.get("published_at"),
                "job_id": batch.get("job_id"),
                "job_title": batch.get("job_title"),
                "rank": candidate.get("rank"),
                "candidate_id": candidate.get("candidate_id"),
                "name": display.get("name", ""),
                "current_title": display.get("current_title", ""),
                "company": display.get("company", ""),
                "score": candidate.get("score"),
                "evidence_coverage": candidate.get("evidence_coverage"),
                "confidence": candidate.get("confidence"),
                "summary": candidate.get("summary"),
                "evidence": "；".join(map(str, candidate.get("evidence") or [])),
                "gaps": "；".join(map(str, candidate.get("gaps") or [])),
                "sources": "；".join(map(str, candidate.get("sources") or [])),
            }
        )
    return output.getvalue()


def _write_immutable_text(path: Path, text: str, *, encoding: str) -> None:
    target = Path(path)
    ensure_private_directory(target.parent)
    if target.exists():
        if target.read_text(encoding=encoding) != text:
            raise ValueError(f"不可覆盖已有交付批次文件：{target}")
        ensure_private_file(target)
        return
    created = False
    try:
        with target.open("x", encoding=encoding, newline="") as handle:
            created = True
            handle.write(text)
        ensure_private_file(target)
    except BaseException:
        if created and target.exists():
            target.unlink()
        raise


def write_immutable_json(batch: Mapping[str, Any], path: Path) -> None:
    _write_immutable_text(Path(path), _json_text(batch), encoding="utf-8")


def write_immutable_csv(batch: Mapping[str, Any], path: Path) -> None:
    _write_immutable_text(Path(path), _csv_text(batch), encoding="utf-8-sig")


def _load_existing_batch(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"已有交付批次 JSON 损坏：{path}") from exc
    if not isinstance(batch, dict) or batch.get("contract") != "candidate_shortlist_batch":
        raise ValueError("已有文件不是 candidate_shortlist_batch")
    return batch


def _validate_existing_input(
    batch: Mapping[str, Any],
    *,
    job_id: str,
    job_title: str,
    rubric_version: str,
    published_at: str,
    selection_scope: str,
) -> None:
    expected = {
        "job_id": _text(job_id, "job_id"),
        "job_title": _text(job_title, "job_title"),
        "rubric_version": _text(rubric_version, "rubric_version"),
        "published_at": _text(published_at, "published_at"),
        "selection_scope": _text(selection_scope, "selection_scope"),
    }
    mismatches = [
        field
        for field, value in expected.items()
        if (
            batch.get(field, SELECTION_SCOPE_UNDELIVERED)
            if field == "selection_scope"
            else batch.get(field)
        )
        != value
    ]
    if mismatches:
        raise ValueError("同一输出目录的 publish 输入不一致：" + ", ".join(mismatches))


def _reject_existing_favorite_conflicts(
    batch: Mapping[str, Any],
    favorite_candidate_ids: Collection[str],
) -> None:
    stable_favorite_ids = _favorite_candidate_ids(favorite_candidate_ids)
    for index, row in enumerate(batch.get("candidates") or []):
        if not isinstance(row, Mapping):
            raise ValueError(f"已有交付批次 candidates[{index}] 无效")
        identifiers = row.get("boss_identifiers")
        boss_candidate_id = (
            str(identifiers.get("encryptGeekId") or "").strip()
            if isinstance(identifiers, Mapping)
            else ""
        )
        if not boss_candidate_id:
            raise ValueError("已有交付批次缺少稳定 encryptGeekId，不能安全复用")
        if boss_candidate_id in stable_favorite_ids:
            raise ValueError("已有不可变交付批次包含注册表已收藏候选人，请使用新输出目录")


def publish_candidate_shortlist(
    *,
    inventory_path: Path,
    output_dir: Path,
    job_id: str,
    job_title: str,
    rubric_version: str,
    published_at: str,
    favorite_candidate_ids: Collection[str] = (),
    favorite_sync_status: str = "not_checked",
    favorite_sync_complete: bool = False,
    favorite_sync_receipt_id: str | None = None,
    selection_scope: str = SELECTION_SCOPE_UNDELIVERED,
) -> dict[str, Any]:
    target_dir = Path(output_dir)
    json_path = target_dir / JSON_FILENAME
    csv_path = target_dir / CSV_FILENAME
    inventory = CandidateInventory.load(inventory_path)
    batch = _load_existing_batch(json_path)
    if batch is None:
        if csv_path.exists():
            raise ValueError("交付目录仅存在 CSV，无法确认不可变批次身份")
        batch = build_candidate_shortlist(
            inventory=inventory,
            job_id=job_id,
            job_title=job_title,
            rubric_version=rubric_version,
            published_at=published_at,
            favorite_candidate_ids=favorite_candidate_ids,
            favorite_sync_status=favorite_sync_status,
            favorite_sync_complete=favorite_sync_complete,
            favorite_sync_receipt_id=favorite_sync_receipt_id,
            selection_scope=selection_scope,
        )
    else:
        _validate_existing_input(
            batch,
            job_id=job_id,
            job_title=job_title,
            rubric_version=rubric_version,
            published_at=published_at,
            selection_scope=selection_scope,
        )
        _reject_existing_favorite_conflicts(batch, favorite_candidate_ids)

    write_immutable_json(batch, json_path)
    write_immutable_csv(batch, csv_path)
    if batch.get("selection_scope", SELECTION_SCOPE_UNDELIVERED) == SELECTION_SCOPE_UNDELIVERED:
        inventory.mark_delivered(
            _text(batch.get("batch_id"), "batch_id"),
            _text(batch.get("job_id"), "job_id"),
            [
                _text(row.get("candidate_id"), "candidate_id")
                for row in batch.get("candidates") or []
            ],
        )
        inventory.save(inventory_path)
    summary = {
        "contract": "candidate_shortlist_publish_summary",
        "batch_id": batch["batch_id"],
        "job_id": batch["job_id"],
        "rubric_version": batch["rubric_version"],
        "selection_scope": batch.get("selection_scope", SELECTION_SCOPE_UNDELIVERED),
        "requested_count": batch["requested_count"],
        "actual_count": batch["actual_count"],
        "shortage_count": batch["shortage_count"],
        "shortage_reason": batch["shortage_reason"],
        "favorite_registry_excluded_count": batch.get("favorite_registry_excluded_count", 0),
        "favorite_sync_status": batch.get("favorite_sync_status", "not_checked"),
        "favorite_sync_complete": batch.get("favorite_sync_complete", False),
        "favorite_sync_receipt_id": batch.get("favorite_sync_receipt_id"),
        "favorite_coverage_warning": batch.get("favorite_coverage_warning"),
        "boss_requests": 0,
    }
    return {
        "batch": batch,
        "summary": summary,
        "json_path": json_path,
        "csv_path": csv_path,
        "inventory_path": Path(inventory_path),
    }
