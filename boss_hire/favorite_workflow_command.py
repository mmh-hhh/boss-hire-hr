from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from boss_hire.favorite_delivery import FavoriteDeliveryLedger
from boss_hire.boss_access import BossLiveAccessDenied
from boss_hire.favorite_sync import validate_favorite_sync_receipt
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.favorite_workflow import (
    LoadedFavoriteWorkflowSession,
    build_favorite_candidate_snapshot,
    build_favorite_final_selection,
    create_favorite_workflow_session,
    favorite_workflow_paths,
    favorite_snapshot_batch,
    load_active_favorite_workflow_session,
    update_favorite_workflow_session,
    _session_lock,
    _load_session_unlocked,
)
from boss_hire.local_security import atomic_write_json, ensure_private_directory
from boss_hire.single_job_run_plan import build_favorite_sync_run_plan
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory


@dataclass(frozen=True)
class FavoriteWorkflowDeliveryInputs:
    """Private adapter files consumed by the existing guarded delivery route."""

    batch_path: Path
    sync_receipt_path: Path
    work_dir: Path


def _session_id(*, account_key: str, job_id: str, rubric_version: str, created_at: str) -> str:
    identity = {
        "account_key": account_key,
        "job_id": job_id,
        "rubric_version": rubric_version,
        "created_at": created_at,
    }
    return "favorite-session-" + content_hash(identity)[:16]


def _ledger_statuses(path: Path) -> dict[str, str]:
    candidates = FavoriteDeliveryLedger(path).to_dict().get("candidates", {})
    if not isinstance(candidates, dict):
        raise ValueError("收藏交付账本 candidates 无效")
    result: dict[str, str] = {}
    for candidate_id, record in candidates.items():
        if not isinstance(record, dict):
            raise ValueError("收藏交付账本记录无效")
        status = str(record.get("status") or "").strip()
        if status:
            result[str(candidate_id)] = status
    return result


def prepare_favorite_workflow(
    *,
    inventory: CandidateInventory,
    account_state_dir: Path,
    account_key: str,
    job_id: str,
    job_title: str,
    rubric_version: str,
    board_date: str,
    created_at: str,
) -> LoadedFavoriteWorkflowSession:
    """Create the local favorite session, or safely resume the active one."""

    paths = favorite_workflow_paths(account_state_dir)
    active = load_active_favorite_workflow_session(paths)
    if active is not None and active.state["status"] not in {"completed", "closed_by_user", "expired"}:
        if any(active.state.get(field) != value for field, value in (
            ("account_key", account_key), ("job_id", job_id), ("rubric_version", rubric_version),
        )):
            raise ValueError("收藏会话与当前岗位/评分版本不一致；请本人先 favorite-close --note <原因>，保留账本")
        return active

    session_id = _session_id(
        account_key=account_key,
        job_id=job_id,
        rubric_version=rubric_version,
        created_at=created_at,
    )
    snapshot = build_favorite_candidate_snapshot(
        inventory=inventory,
        job_id=job_id,
        job_title=job_title,
        rubric_version=rubric_version,
        favorite_candidate_ids=FavoriteRegistry(account_state_dir, account_key=account_key).known_candidate_ids(),
        favorite_statuses=_ledger_statuses(Path(account_state_dir) / "delivery_ledger.json"),
        created_at=created_at,
        snapshot_id=session_id.replace("session", "snapshot", 1),
    )
    return create_favorite_workflow_session(
        paths,
        {
            "schema_version": 1,
            "contract": "favorite_workflow_session",
            "session_id": session_id,
            "account_key": account_key,
            "board_date": board_date,
            "job_id": job_id,
            "job_title": job_title,
            "rubric_version": rubric_version,
            "status": "awaiting_sync_confirmation",
            "created_at": created_at,
            "updated_at": created_at,
            "candidate_snapshot": snapshot,
        },
    )


def build_favorite_workflow_sync_plan(
    session: LoadedFavoriteWorkflowSession,
    *,
    auth_dir: Path,
) -> dict[str, object]:
    """Build the bounded sync plan bound to one hidden candidate snapshot."""

    state = session.state
    if state["status"] not in {"awaiting_sync_confirmation", "blocked_incomplete_sync"}:
        raise ValueError("当前收藏会话不能核对收藏状态")
    registry = FavoriteRegistry(session.paths.root, account_key=str(state["account_key"]))
    checkpoint = registry.checkpoint()
    plan = build_favorite_sync_run_plan(
        board_date=str(state["board_date"]),
        auth_dir=Path(auth_dir),
        mode="incremental" if checkpoint is not None else "initialize",
        purpose="favorite_delivery",
        checkpoint=checkpoint,
        batch=favorite_snapshot_batch(state["candidate_snapshot"]),
    )
    if plan["account_key"] != state["account_key"]:
        raise ValueError("收藏会话与固定认证账号不匹配")
    return {
        **plan,
        "favorite_session_id": state["session_id"],
        "candidate_snapshot_digest": state["candidate_snapshot_digest"],
    }


def confirm_favorite_sync(
    session: LoadedFavoriteWorkflowSession,
    *,
    input_fn: Callable[[str], str],
) -> None:
    """Require the business-facing confirmation before any auth/client setup."""

    if session.state["status"] not in {"awaiting_sync_confirmation", "blocked_incomplete_sync"}:
        raise ValueError("当前收藏会话不能核对收藏状态")
    try:
        confirmation = input_fn("输入“确认生成未收藏Top5”继续：")
    except EOFError as exc:
        raise BossLiveAccessDenied("收藏状态核对确认未完成") from exc
    if confirmation != "确认生成未收藏Top5":
        raise BossLiveAccessDenied("收藏状态核对确认文本不匹配")


def record_favorite_workflow_sync(
    session: LoadedFavoriteWorkflowSession,
    *,
    plan: Mapping[str, object],
    receipt: Mapping[str, object],
    updated_at: str,
) -> LoadedFavoriteWorkflowSession:
    state = session.state
    if plan.get("favorite_session_id") != state["session_id"]:
        raise ValueError("同步计划与收藏会话不匹配")
    if plan.get("candidate_snapshot_digest") != state["candidate_snapshot_digest"]:
        raise ValueError("同步计划与候选快照不匹配")
    batch = favorite_snapshot_batch(state["candidate_snapshot"])
    validated = validate_favorite_sync_receipt(
        receipt,
        account_key=str(state["account_key"]),
        board_date=str(state["board_date"]),
        purpose="favorite_delivery",
        batch_id=str(batch["batch_id"]),
        batch_digest=content_hash(batch),
    )
    final_selection = None
    if validated["complete"]:
        final_selection = build_favorite_final_selection(
            state["candidate_snapshot"],
            favorite_candidate_ids=FavoriteRegistry(
                session.paths.root,
                account_key=str(state["account_key"]),
            ).known_candidate_ids(),
            favorite_statuses=_ledger_statuses(session.paths.root / "delivery_ledger.json"),
            selected_at=updated_at,
        )
        status = "awaiting_delivery_confirmation" if final_selection["actual_count"] else "completed"
    else:
        status = "blocked_risk" if validated["status"] == "risk_stopped" else "blocked_incomplete_sync"
    next_state = {
        **state,
        "status": status,
        "updated_at": updated_at,
        "sync_plan": dict(plan),
        "sync_receipt": validated,
    }
    if final_selection is not None:
        next_state["final_selection"] = final_selection
    return update_favorite_workflow_session(
        session,
        next_state,
    )


def materialize_favorite_workflow_delivery_inputs(session: LoadedFavoriteWorkflowSession) -> FavoriteWorkflowDeliveryInputs:
    # The same lock protects local close and the first creation of delivery inputs.
    with _session_lock(session.paths):
        current = _load_session_unlocked(session.paths, str(session.state["session_id"]))
        if current.state_digest != session.state_digest:
            raise ValueError("收藏会话已变化；未准备交付材料")
        return _materialize_favorite_workflow_delivery_inputs(current)


def _materialize_favorite_workflow_delivery_inputs(
    session: LoadedFavoriteWorkflowSession,
) -> FavoriteWorkflowDeliveryInputs:
    """Write private, session-bound adapter files for the existing delivery owner."""

    state = session.state
    if state.get("status") != "awaiting_delivery_confirmation":
        raise ValueError("当前收藏会话不能执行收藏")
    receipt = state.get("sync_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("收藏会话缺少完整的收藏状态核对结果")
    batch = favorite_snapshot_batch(state["candidate_snapshot"])
    validated = validate_favorite_sync_receipt(
        receipt,
        account_key=str(state["account_key"]),
        board_date=str(state["board_date"]),
        purpose="favorite_delivery",
        require_complete=True,
        batch_id=str(batch["batch_id"]),
        batch_digest=content_hash(batch),
    )
    inputs_dir = session.state_path.parent / "delivery_inputs"
    ensure_private_directory(inputs_dir)
    batch_path = inputs_dir / "candidate_snapshot_batch.json"
    receipt_path = inputs_dir / "favorite_sync_receipt.json"
    atomic_write_json(batch_path, batch, sort_keys=True)
    atomic_write_json(receipt_path, validated, sort_keys=True)
    return FavoriteWorkflowDeliveryInputs(
        batch_path=batch_path,
        sync_receipt_path=receipt_path,
        work_dir=session.state_path.parent / "delivery",
    )


def record_favorite_workflow_delivery(
    session: LoadedFavoriteWorkflowSession,
    *,
    receipt: Mapping[str, object],
    updated_at: str,
) -> LoadedFavoriteWorkflowSession:
    """Persist the guarded delivery result without reopening stopped candidates."""

    if session.state.get("status") != "awaiting_delivery_confirmation":
        raise ValueError("当前收藏会话不能完成收藏")
    if receipt.get("schema_version") != 1 or receipt.get("contract") != "boss_favorite_delivery_receipt":
        raise ValueError("收藏交付回执无效")
    batch = favorite_snapshot_batch(session.state["candidate_snapshot"])
    if receipt.get("batch_id") != batch["batch_id"]:
        raise ValueError("收藏交付回执与收藏会话不匹配")
    final_selection = session.state.get("final_selection")
    if not isinstance(final_selection, Mapping):
        raise ValueError("收藏会话缺少最终待收藏名单")
    selected_count = receipt.get("selected_count")
    counts = {
        "favorite_confirmed": receipt.get("confirmed_count"),
        "already_confirmed": receipt.get("already_confirmed_count"),
        "favorite_failed": receipt.get("failed_count"),
        "favorite_unknown": receipt.get("unknown_count"),
    }
    if (
        not isinstance(selected_count, int)
        or isinstance(selected_count, bool)
        or selected_count < 1
        or selected_count > final_selection["actual_count"]
        or any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts.values())
    ):
        raise ValueError("收藏交付回执计数无效")
    results = receipt.get("results")
    if not isinstance(results, list) or len(results) > selected_count:
        raise ValueError("收藏交付回执结果无效")
    allowed = {
        (candidate["candidate_id"], candidate["rank"])
        for candidate in final_selection["candidates"]
    }
    result_keys: list[tuple[object, object]] = []
    result_statuses: list[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("收藏交付回执候选结果无效")
        key = (result.get("candidate_id"), result.get("rank"))
        status = str(result.get("status") or "")
        if key not in allowed or status not in counts or key in result_keys:
            raise ValueError("收藏交付回执不是最终待收藏名单的精确子集")
        result_keys.append(key)
        result_statuses.append(status)
    if [rank for _, rank in result_keys] != sorted((rank for _, rank in result_keys), reverse=True):
        raise ValueError("收藏交付回执顺序与最终待收藏名单不一致")
    if any(result_statuses.count(status) != count for status, count in counts.items()):
        raise ValueError("收藏交付回执计数与候选结果不一致")
    failed_count = counts["favorite_failed"]
    unknown_count = counts["favorite_unknown"]
    status = "blocked_unknown" if failed_count or unknown_count else "completed"
    return update_favorite_workflow_session(
        session,
        {
            **session.state,
            "status": status,
            "updated_at": updated_at,
            "delivery_receipt": dict(receipt),
            "delivery_receipt_digest": content_hash(receipt),
        },
    )


def expire_favorite_workflow_session(
    session: LoadedFavoriteWorkflowSession,
    *,
    updated_at: str,
) -> LoadedFavoriteWorkflowSession:
    """Make a prior-day session terminal before any new normal favorite stage."""

    if session.state.get("status") in {"completed", "closed_by_user", "expired"}:
        return session
    return update_favorite_workflow_session(
        session,
        {
            **session.state,
            "status": "expired",
            "updated_at": updated_at,
        },
    )


def close_favorite_workflow(session: LoadedFavoriteWorkflowSession, *, note: str, updated_at: str) -> LoadedFavoriteWorkflowSession:
    """Close an unattempted local session; never change delivery history or circuit."""
    if not note.strip():
        raise ValueError("关闭收藏会话需要说明原因")
    if (session.state_path.parent / "delivery").exists():
        raise ValueError("本会话已有交付材料，可能已尝试写入；请人工核对，不能通过关闭重试")
    if session.state["status"] not in {"awaiting_sync_confirmation", "awaiting_delivery_confirmation", "blocked_incomplete_sync", "expired"}:
        raise ValueError("当前收藏会话不允许本地关闭；保留原回执与停止状态")
    return update_favorite_workflow_session(session, {
        **session.state, "status": "closed_by_user", "updated_at": updated_at, "close_note": note.strip(),
    }, require_unattempted=True)
