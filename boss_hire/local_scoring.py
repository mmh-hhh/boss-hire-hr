from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Mapping

from boss_hire.local_security import atomic_write_json, ensure_private_directory
from boss_hire.single_job_llm import (
    JsonLlm,
    LlmHttpError,
    evaluate_redacted_candidate,
    redact_resume_for_llm,
)
from boss_hire.supply_inventory import CandidateInventory


DEFAULT_LLM_WORKERS = 4
MAX_LLM_WORKERS = 8


def _selection_limit(selection: str | int) -> tuple[str, int | None]:
    if selection == "all":
        return "all", None
    try:
        limit = int(selection)
    except (TypeError, ValueError) as exc:
        raise ValueError("评分选择必须是 all 或正整数") from exc
    if isinstance(selection, bool) or limit <= 0 or str(selection).strip() != str(limit):
        raise ValueError("评分选择必须是 all 或正整数")
    return str(limit), limit


def _worker_limit(workers: int) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise ValueError(f"评分并发数必须是 1 到 {MAX_LLM_WORKERS} 的整数")
    if not 1 <= workers <= MAX_LLM_WORKERS:
        raise ValueError(f"评分并发数必须是 1 到 {MAX_LLM_WORKERS} 的整数")
    return workers


def _is_rate_limited(error: Exception) -> bool:
    return "HTTP 429" in str(error)


def score_inventory_resumes(
    *,
    inventory_path: Path,
    job_id: str,
    rubric: Mapping[str, Any],
    llm: JsonLlm,
    output_dir: Path,
    selection: str | int = "all",
    workers: int = DEFAULT_LLM_WORKERS,
) -> dict[str, Any]:
    if rubric.get("contract") != "continuous_ranking":
        raise ValueError("rubric 必须使用 continuous_ranking 契约")
    rubric_version = str(rubric.get("version") or "").strip()
    if not rubric_version:
        raise ValueError("rubric 缺少 version")
    stable_job_id = str(job_id or "").strip()
    if not stable_job_id:
        raise ValueError("job_id 不能为空")
    selection_label, limit = _selection_limit(selection)
    worker_limit = _worker_limit(workers)
    inventory = CandidateInventory.load(inventory_path)
    pending = inventory.list_score_pending(
        job_id=stable_job_id,
        rubric_version=rubric_version,
    )
    selected = pending if limit is None else pending[:limit]
    prepared = [
        (
            str(row["candidate_id"]),
            redact_resume_for_llm(row["resume"], str(row["candidate_id"])),
        )
        for row in selected
    ]
    scored_by_id: dict[str, Mapping[str, Any]] = {}
    failures_by_id: dict[str, str] = {}
    http_errors_by_id: dict[str, dict[str, Any]] = {}
    backpressure_stopped = False
    attempted_count = 0

    # Workers only make local LLM calls. Inventory mutation and persistence remain
    # on this thread so every completed evaluation is atomically checkpointed.
    with ThreadPoolExecutor(max_workers=worker_limit) as executor:
        pending_iterator = iter(prepared)
        futures: dict[Future[dict[str, Any]], str] = {}

        def submit_until_full() -> None:
            while len(futures) < worker_limit:
                try:
                    candidate_id, redacted = next(pending_iterator)
                except StopIteration:
                    return
                future = executor.submit(evaluate_redacted_candidate, llm, redacted, rubric)
                futures[future] = candidate_id

        submit_until_full()
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                candidate_id = futures.pop(future)
                attempted_count += 1
                try:
                    evaluation = future.result()
                except (RuntimeError, ValueError) as exc:
                    failures_by_id[candidate_id] = str(exc)
                    if isinstance(exc, LlmHttpError):
                        http_errors_by_id[candidate_id] = dict(exc.public_details)
                    backpressure_stopped = backpressure_stopped or _is_rate_limited(exc)
                    continue
                inventory.record_evaluation(stable_job_id, candidate_id, evaluation)
                inventory.save(inventory_path)
                scored_by_id[candidate_id] = evaluation
            if not backpressure_stopped:
                submit_until_full()

    scored_candidate_ids = [candidate_id for candidate_id, _ in prepared if candidate_id in scored_by_id]
    failures = []
    for candidate_id, _ in prepared:
        if candidate_id not in failures_by_id:
            continue
        failure: dict[str, Any] = {"candidate_id": candidate_id, "error": failures_by_id[candidate_id]}
        if candidate_id in http_errors_by_id:
            failure["http_error"] = http_errors_by_id[candidate_id]
        failures.append(failure)
    inventory.save(inventory_path)
    remaining = inventory.list_score_pending(
        job_id=stable_job_id,
        rubric_version=rubric_version,
    )
    summary = {
        "schema_version": 1,
        "contract": "local_candidate_scoring",
        "job_id": stable_job_id,
        "rubric_version": rubric_version,
        "llm_model": llm.model,
        "selection": selection_label,
        "workers": worker_limit,
        "pending_before": len(pending),
        "selected_count": len(selected),
        "attempted_count": attempted_count,
        "scored_count": len(scored_candidate_ids),
        "failed_count": len(failures),
        "remaining_pending_count": len(remaining),
        "backpressure_stopped": backpressure_stopped,
        "scored_candidate_ids": scored_candidate_ids,
        "failures": failures,
        "boss_requests": 0,
    }
    target = Path(output_dir)
    ensure_private_directory(target)
    summary_path = target / "score_summary.json"
    atomic_write_json(summary_path, summary, sort_keys=True)
    return {"summary": summary, "summary_path": summary_path, "inventory_path": Path(inventory_path)}
