from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from boss_hire.local_scoring import DEFAULT_LLM_WORKERS, MAX_LLM_WORKERS
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory


PREVIEW_CONTRACT = "boss_hire_llm_confirmation_preview"
RECEIPT_CONTRACT = "boss_hire_llm_confirmation_receipt"


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _candidate_ids_with_ready_resumes(
    inventory: CandidateInventory,
    *,
    job_id: str,
) -> list[str]:
    state = inventory.to_dict()
    rows = sorted(
        state["candidates"].items(),
        key=lambda item: (item[1].get("discovery_order", 0), item[0]),
    )
    result: list[str] = []
    for candidate_id, candidate in rows:
        if candidate.get("resume_status") != "ready":
            continue
        sources = candidate.get("sources") or {}
        belongs_to_job = any(
            str(card.get("encryptJobId") or card.get("encrypt_job_id") or "").strip()
            == job_id
            for cards in sources.values()
            if isinstance(cards, list)
            for card in cards
            if isinstance(card, Mapping)
        )
        if belongs_to_job:
            result.append(str(candidate_id))
    return result


def build_llm_confirmation_preview(
    *,
    inventory: CandidateInventory,
    job_id: str,
    rubric: Mapping[str, Any],
    model: str,
    workers: int = DEFAULT_LLM_WORKERS,
    created_at: str,
) -> dict[str, Any]:
    stable_job_id = _text(job_id, "job_id")
    stable_model = _text(model, "model")
    stable_created_at = _text(created_at, "created_at")
    if rubric.get("contract") != "continuous_ranking":
        raise ValueError("rubric 必须使用 continuous_ranking 契约")
    rubric_version = _text(rubric.get("version"), "rubric.version")
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= MAX_LLM_WORKERS:
        raise ValueError(f"workers 必须是 1 到 {MAX_LLM_WORKERS} 的整数")
    ready_ids = _candidate_ids_with_ready_resumes(inventory, job_id=stable_job_id)
    pending_rows = inventory.list_score_pending(
        job_id=stable_job_id,
        rubric_version=rubric_version,
    )
    pending_ids = [str(row["candidate_id"]) for row in pending_rows]
    pending_set = set(pending_ids)
    reused_ids = [candidate_id for candidate_id in ready_ids if candidate_id not in pending_set]
    identity = {
        "job_id": stable_job_id,
        "rubric_version": rubric_version,
        "rubric_digest": content_hash(rubric),
        "llm_model": stable_model,
        "workers": workers,
        "ready_candidate_ids_digest": content_hash(ready_ids),
        "reused_candidate_ids_digest": content_hash(reused_ids),
        "pending_candidate_ids_digest": content_hash(pending_ids),
    }
    return {
        "schema_version": 1,
        "contract": PREVIEW_CONTRACT,
        "confirmation_id": "llm-confirm-" + content_hash(identity)[:12],
        **identity,
        "ready_resume_count": len(ready_ids),
        "reuse_count": len(reused_ids),
        "pending_count": len(pending_ids),
        "created_at": stable_created_at,
        "boss_requests": 0,
    }


def build_llm_confirmation_receipt(
    preview: Mapping[str, Any],
    *,
    confirmed_at: str,
) -> dict[str, Any]:
    if preview.get("contract") != PREVIEW_CONTRACT or preview.get("schema_version") != 1:
        raise ValueError("LLM 确认预览 contract 无效")
    receipt = {
        key: deepcopy(value)
        for key, value in preview.items()
        if key not in {"contract", "created_at"}
    }
    receipt.update(
        {
            "contract": RECEIPT_CONTRACT,
            "status": "confirmed",
            "confirmed_at": _text(confirmed_at, "confirmed_at"),
        }
    )
    return receipt


def verify_llm_confirmation_preview(
    preview: Mapping[str, Any],
    *,
    inventory: CandidateInventory,
    job_id: str,
    rubric: Mapping[str, Any],
    model: str,
    workers: int,
    checked_at: str,
) -> None:
    fresh = build_llm_confirmation_preview(
        inventory=inventory,
        job_id=job_id,
        rubric=rubric,
        model=model,
        workers=workers,
        created_at=checked_at,
    )
    if preview.get("confirmation_id") != fresh["confirmation_id"]:
        raise ValueError("LLM 确认范围已变化")
    for field in (
        "job_id",
        "rubric_version",
        "rubric_digest",
        "llm_model",
        "workers",
        "ready_candidate_ids_digest",
        "reused_candidate_ids_digest",
        "pending_candidate_ids_digest",
        "ready_resume_count",
        "reuse_count",
        "pending_count",
    ):
        if preview.get(field) != fresh.get(field):
            raise ValueError(f"LLM 确认字段已变化：{field}")
