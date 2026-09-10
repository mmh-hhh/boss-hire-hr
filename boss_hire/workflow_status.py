from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from boss_hire.state_store import content_hash
from boss_hire.single_job_run_plan import (
    build_candidate_detail_run_plan,
    load_single_job_run_plan,
    write_single_job_run_plan,
)
from boss_hire.supply_inventory import CandidateInventory
from boss_hire.workflow_run import (
    LoadedWorkflowRun,
    WorkflowPaths,
    bind_workflow_run_artifacts,
    load_active_workflow_run,
    load_workflow_run,
    save_active_workflow_run,
    transition_workflow_run_state,
)


STATUS_CONTRACT = "boss_hire_workflow_status"


def _candidate_ids_for_job(state: Mapping[str, Any], job_id: str) -> set[str]:
    result: set[str] = set()
    for candidate_id, candidate in (state.get("candidates") or {}).items():
        if not isinstance(candidate, Mapping) or candidate.get("card_status") != "ready":
            continue
        sources = candidate.get("sources") or {}
        if any(
            str(card.get("encryptJobId") or card.get("encrypt_job_id") or "").strip()
            == job_id
            for cards in sources.values()
            if isinstance(cards, list)
            for card in cards
            if isinstance(card, Mapping)
        ):
            result.add(str(candidate_id))
    return result


def build_inventory_funnel(
    inventory: CandidateInventory,
    *,
    job_id: str,
    rubric_version: str,
) -> dict[str, int]:
    state = inventory.to_dict()
    candidate_ids = _candidate_ids_for_job(state, job_id)
    candidates = state["candidates"]
    evaluations = state["evaluations"].get(job_id, {})
    delivered = state["delivered"].get(job_id, {})
    valid_score_ids = {
        candidate_id
        for candidate_id, evaluation in evaluations.items()
        if candidate_id in candidate_ids
        and isinstance(evaluation, Mapping)
        and evaluation.get("rubric_version") == rubric_version
    }
    pending_detail_ids = {
        candidate_id
        for candidate_id in candidate_ids
        if candidates[candidate_id].get("resume_status") == "pending"
    }
    pending_score_ids = {
        candidate_id
        for candidate_id in candidate_ids
        if candidates[candidate_id].get("resume_status") == "ready"
        and candidate_id not in valid_score_ids
    }
    undelivered_ids = valid_score_ids.difference(delivered)
    return {
        "candidate_card_count": len(candidate_ids),
        "pending_detail_count": len(pending_detail_ids),
        "pending_score_count": len(pending_score_ids),
        "valid_score_count": len(valid_score_ids),
        "undelivered_score_count": len(undelivered_ids),
        "delivered_score_count": len(valid_score_ids) - len(undelivered_ids),
    }


def _verify_job_artifacts(inventory: CandidateInventory, run: LoadedWorkflowRun) -> None:
    state = run.state
    if content_hash(state["config"]) != state["config_digest"]:
        raise ValueError("运行配置摘要不一致")
    artifacts = inventory.get_job_artifacts(state["job_id"], state["source_jd_hash"])
    if artifacts is None:
        raise ValueError("当前岗位产物不存在或 JD 摘要不一致")
    rubric = artifacts.get("rubric")
    search_plan = artifacts.get("search_plan")
    if not isinstance(rubric, Mapping) or not isinstance(search_plan, Mapping):
        raise ValueError("当前岗位 rubric/search plan 产物无效")
    if rubric.get("version") != state["rubric_version"]:
        raise ValueError("当前 rubric version 与运行实例不一致")
    if content_hash(rubric) != state["rubric_digest"]:
        raise ValueError("当前 rubric 摘要与运行实例不一致")
    if search_plan.get("version") != state["search_plan_version"]:
        raise ValueError("当前 search plan version 与运行实例不一致")
    if content_hash(search_plan) != state["search_plan_digest"]:
        raise ValueError("当前 search plan 摘要与运行实例不一致")


def build_workflow_status(
    paths: WorkflowPaths,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    run = load_workflow_run(paths, run_id) if run_id is not None else load_active_workflow_run(paths)
    if run is None:
        return {
            "schema_version": 1,
            "contract": STATUS_CONTRACT,
            "run_id": None,
            "status": "no_active_run",
            "next_action": "start",
            "next_command": ".venv/bin/python scripts/run_single_job_live.py start --config <名称>",
            "funnel": None,
            "artifacts": {},
            "blocker": None,
            "boss_requests": 0,
        }
    inventory = CandidateInventory.load(paths.inventory_path)
    _verify_job_artifacts(inventory, run)
    state = run.state
    return {
        "schema_version": 1,
        "contract": STATUS_CONTRACT,
        "run_id": state["run_id"],
        "status": state["status"],
        "next_action": state["next_action"],
        "next_command": (
            None
            if state["next_action"] in {"none", "publish_manual"}
            else ".venv/bin/python scripts/run_single_job_live.py continue"
        ),
        "config_name": state["config_name"],
        "board_date": state["board_date"],
        "job_id": state["job_id"],
        "rubric_version": state["rubric_version"],
        "funnel": build_inventory_funnel(
            inventory,
            job_id=state["job_id"],
            rubric_version=state["rubric_version"],
        ),
        "artifacts": state["artifacts"],
        "blocker": state["last_error"] if state["status"] == "blocked" else None,
        "boss_requests": 0,
    }


def prepare_detail_action(
    paths: WorkflowPaths,
    *,
    selection: str | int = "all",
    updated_at: str,
) -> dict[str, Any]:
    run = load_active_workflow_run(paths)
    if run is None:
        raise ValueError("没有活动运行；请先使用 start")
    if run.state["status"] != "source_complete":
        raise ValueError(f"当前状态不能准备详情：{run.state['status']}")
    inventory = CandidateInventory.load(paths.inventory_path)
    _verify_job_artifacts(inventory, run)
    selected = inventory.select_resume_pending(
        job_id=run.state["job_id"],
        selection=selection,
    )
    if selected["selected_count"] == 0:
        next_state = transition_workflow_run_state(
            run.state,
            status="detail_complete",
            updated_at=updated_at,
        )
        saved = save_active_workflow_run(
            paths,
            next_state,
            expected_state_digest=run.state_digest,
        )
        return {
            "schema_version": 1,
            "contract": "boss_hire_detail_action",
            "run_id": run.state["run_id"],
            "status": "skipped_no_pending",
            "selection": selected,
            "candidate_ids": [],
            "next_action": saved.state["next_action"],
            "boss_requests": 0,
        }
    return {
        "schema_version": 1,
        "contract": "boss_hire_detail_action",
        "run_id": run.state["run_id"],
        "status": "awaiting_confirmation",
        "selection": selected,
        "candidate_ids": [row["candidate_id"] for row in selected["candidates"]],
        "next_action": "candidate_details",
        "boss_requests": 0,
    }


def freeze_detail_plan(
    paths: WorkflowPaths,
    *,
    selection: str | int,
    auth_dir: Path,
    updated_at: str,
    action: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], LoadedWorkflowRun]:
    prepared_action = (
        dict(action)
        if action is not None
        else prepare_detail_action(paths, selection=selection, updated_at=updated_at)
    )
    if prepared_action["status"] != "awaiting_confirmation":
        raise ValueError("当前没有需要冻结的候选详情计划")
    run = load_active_workflow_run(paths)
    if run is None or run.state["run_id"] != prepared_action["run_id"]:
        raise ValueError("活动运行在详情计划准备期间发生变化")
    plan = build_candidate_detail_run_plan(
        board_date=run.state["board_date"],
        auth_dir=auth_dir,
        selection=prepared_action["selection"],
    )
    if plan["account_key"] != run.state["account_key"]:
        raise ValueError("详情计划账号与运行实例不一致")
    if plan["job_id"] != run.state["job_id"]:
        raise ValueError("详情计划岗位与运行实例不一致")
    plan_path = run.state_path.parent / "detail_plan.json"
    write_single_job_run_plan(plan, plan_path)
    bound_state = bind_workflow_run_artifacts(
        run.state,
        updated_at=updated_at,
        artifacts={
            "detail_plan": {
                "path": "detail_plan.json",
                "digest": content_hash(plan),
            }
        },
    )
    saved = save_active_workflow_run(
        paths,
        bound_state,
        expected_state_digest=run.state_digest,
    )
    return plan, saved


def verify_frozen_detail_plan(
    paths: WorkflowPaths,
    *,
    auth_dir: Path,
) -> tuple[dict[str, Any], LoadedWorkflowRun]:
    run = load_active_workflow_run(paths)
    if run is None or run.state["status"] != "source_complete":
        raise ValueError("当前状态没有可执行的候选详情计划")
    reference = run.state["artifacts"].get("detail_plan")
    if not isinstance(reference, Mapping):
        raise ValueError("运行实例缺少冻结详情计划")
    plan_path = run.state_path.parent / str(reference["path"])
    plan = load_single_job_run_plan(plan_path)
    inventory = CandidateInventory.load(paths.inventory_path)
    _verify_job_artifacts(inventory, run)
    fresh_selection = inventory.select_resume_pending(
        job_id=run.state["job_id"],
        selection=plan["selection"],
    )
    fresh_plan = build_candidate_detail_run_plan(
        board_date=run.state["board_date"],
        auth_dir=auth_dir,
        selection=fresh_selection,
    )
    if content_hash(fresh_plan) != content_hash(plan):
        raise ValueError("冻结详情计划与当前 pending 候选、日期、账号或岗位不一致")
    return plan, run
