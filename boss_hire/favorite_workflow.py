from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from boss_hire.local_security import atomic_write_json, ensure_private_directory
from boss_hire.shortlist_publish import _candidate_row, _stable_boss_candidate_id
from boss_hire.state_store import content_hash
from boss_hire.supply_inventory import CandidateInventory


SCHEMA_VERSION = 1
SESSION_CONTRACT = "favorite_workflow_session"
ACTIVE_SESSION_CONTRACT = "favorite_workflow_active_session"
SNAPSHOT_CONTRACT = "boss_favorite_candidate_snapshot"
FINAL_SELECTION_CONTRACT = "boss_favorite_final_selection"
ACTIVE_STATUSES = frozenset(
    {
        "draft",
        "awaiting_sync_confirmation",
        "sync_complete",
        "awaiting_delivery_confirmation",
        "blocked_incomplete_sync",
        "blocked_risk",
        "blocked_unknown",
    }
)
TERMINAL_STATUSES = frozenset({"completed", "closed_by_user", "expired"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{7,127}")
_ACCOUNT_KEY = re.compile(r"[0-9a-f]{16}")
_BOARD_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
SHORTLIST_SIZE = 5
LEDGER_EXCLUDED_STATUSES = frozenset(
    {
        "already_confirmed",
        "favorite_confirmed",
        "favorite_failed",
        "favorite_unknown",
        "manual_verified",
        "write_reserved",
    }
)


def _required_text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _safe_id(value: Any, field: str) -> str:
    result = _required_text(value, field)
    if _SAFE_ID.fullmatch(result) is None:
        raise ValueError(f"{field} 不安全")
    return result


def _digest(value: Any, field: str) -> str:
    result = _required_text(value, field)
    if _DIGEST.fullmatch(result) is None:
        raise ValueError(f"{field} 无效")
    return result


@dataclass(frozen=True)
class FavoriteWorkflowPaths:
    root: Path
    active_session_path: Path
    sessions_root: Path


@dataclass(frozen=True)
class LoadedFavoriteWorkflowSession:
    paths: FavoriteWorkflowPaths
    state_path: Path
    snapshot_path: Path
    state: dict[str, Any]
    state_digest: str


def favorite_workflow_paths(account_state_dir: Path) -> FavoriteWorkflowPaths:
    root = Path(account_state_dir).resolve(strict=False)
    return FavoriteWorkflowPaths(
        root=root,
        active_session_path=root / "active_session.json",
        sessions_root=root / "sessions",
    )


def ensure_favorite_workflow_directories(paths: FavoriteWorkflowPaths) -> None:
    ensure_private_directory(paths.root)
    ensure_private_directory(paths.sessions_root)


@contextmanager
def _session_lock(paths: FavoriteWorkflowPaths) -> Iterator[None]:
    ensure_favorite_workflow_directories(paths)
    lock_path = paths.root / ".session.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _session_dir(paths: FavoriteWorkflowPaths, session_id: str) -> Path:
    return paths.sessions_root / _safe_id(session_id, "session_id")


def _state_path(paths: FavoriteWorkflowPaths, session_id: str) -> Path:
    return _session_dir(paths, session_id) / "session.json"


def _snapshot_path(paths: FavoriteWorkflowPaths, session_id: str) -> Path:
    return _session_dir(paths, session_id) / "candidate_snapshot.json"


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} 无法读取") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def _relative_path(*parts: str) -> str:
    return str(PurePosixPath(*parts))


def _normalize_session_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("收藏会话必须是对象")
    state = dict(value)
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("收藏会话 schema_version 无效")
    if state.get("contract") != SESSION_CONTRACT:
        raise ValueError("收藏会话 contract 无效")
    session_id = _safe_id(state.get("session_id"), "session_id")
    account_key = _required_text(state.get("account_key"), "account_key")
    if _ACCOUNT_KEY.fullmatch(account_key) is None:
        raise ValueError("account_key 无效")
    board_date = _required_text(state.get("board_date"), "board_date")
    if _BOARD_DATE.fullmatch(board_date) is None:
        raise ValueError("board_date 无效")
    status = _required_text(state.get("status"), "status")
    if status not in ALL_STATUSES:
        raise ValueError("收藏会话状态无效")
    snapshot = state.get("candidate_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("收藏会话缺少候选快照")
    normalized_snapshot = dict(snapshot)
    if normalized_snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("候选快照 schema_version 无效")
    if normalized_snapshot.get("contract") != SNAPSHOT_CONTRACT:
        raise ValueError("候选快照 contract 无效")
    if _safe_id(normalized_snapshot.get("snapshot_id"), "candidate_snapshot.snapshot_id") != session_id.replace("session", "snapshot", 1):
        raise ValueError("候选快照与收藏会话不匹配")
    if _required_text(normalized_snapshot.get("job_id"), "candidate_snapshot.job_id") != _required_text(state.get("job_id"), "job_id"):
        raise ValueError("候选快照 job_id 不一致")
    if _required_text(normalized_snapshot.get("rubric_version"), "candidate_snapshot.rubric_version") != _required_text(state.get("rubric_version"), "rubric_version"):
        raise ValueError("候选快照 rubric_version 不一致")
    if not isinstance(normalized_snapshot.get("candidates"), list):
        raise ValueError("候选快照 candidates 必须是列表")
    for field in ("job_id", "job_title", "rubric_version", "created_at", "updated_at"):
        _required_text(state.get(field), field)
    snapshot_digest = content_hash(normalized_snapshot)
    normalized = {
        **state,
        "session_id": session_id,
        "account_key": account_key,
        "board_date": board_date,
        "status": status,
        "candidate_snapshot": normalized_snapshot,
        "candidate_snapshot_digest": snapshot_digest,
    }
    final_selection = state.get("final_selection")
    if final_selection is not None:
        normalized_selection = _normalize_final_selection(
            final_selection,
            snapshot=normalized_snapshot,
            snapshot_digest=snapshot_digest,
        )
        selection_digest = content_hash(normalized_selection)
        stored_digest = state.get("final_selection_digest")
        if stored_digest is not None and _digest(stored_digest, "final_selection_digest") != selection_digest:
            raise ValueError("最终待收藏名单摘要不一致")
        normalized["final_selection"] = normalized_selection
        normalized["final_selection_digest"] = selection_digest
    else:
        normalized.pop("final_selection", None)
        normalized.pop("final_selection_digest", None)
    return normalized


def _active_pointer(*, session_id: str, state_digest: str) -> dict[str, Any]:
    stable_session_id = _safe_id(session_id, "session_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": ACTIVE_SESSION_CONTRACT,
        "session_id": stable_session_id,
        "state_path": _relative_path("sessions", stable_session_id, "session.json"),
        "state_digest": _digest(state_digest, "state_digest"),
    }


def _validate_active_pointer(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("contract") != ACTIVE_SESSION_CONTRACT:
        raise ValueError("活动收藏会话指针无效")
    session_id = _safe_id(value.get("session_id"), "active_session.session_id")
    expected_path = _relative_path("sessions", session_id, "session.json")
    if value.get("state_path") != expected_path:
        raise ValueError("活动收藏会话路径不一致")
    return _active_pointer(session_id=session_id, state_digest=_digest(value.get("state_digest"), "active_session.state_digest"))


def _load_session_unlocked(paths: FavoriteWorkflowPaths, session_id: str) -> LoadedFavoriteWorkflowSession:
    stable_session_id = _safe_id(session_id, "session_id")
    state_path = _state_path(paths, stable_session_id)
    snapshot_path = _snapshot_path(paths, stable_session_id)
    state = _normalize_session_state(_read_json_object(state_path, "收藏会话"))
    if state["session_id"] != stable_session_id:
        raise ValueError("收藏会话 ID 不一致")
    snapshot = _read_json_object(snapshot_path, "候选快照")
    if content_hash(snapshot) != state["candidate_snapshot_digest"] or snapshot != state["candidate_snapshot"]:
        raise ValueError("候选快照与收藏会话摘要不一致")
    return LoadedFavoriteWorkflowSession(
        paths=paths,
        state_path=state_path,
        snapshot_path=snapshot_path,
        state=state,
        state_digest=content_hash(state),
    )


def load_active_favorite_workflow_session(paths: FavoriteWorkflowPaths) -> LoadedFavoriteWorkflowSession | None:
    with _session_lock(paths):
        if not paths.active_session_path.exists():
            return None
        pointer = _validate_active_pointer(_read_json_object(paths.active_session_path, "活动收藏会话指针"))
        loaded = _load_session_unlocked(paths, pointer["session_id"])
        if loaded.state_digest != pointer["state_digest"]:
            raise ValueError("活动收藏会话与状态摘要不一致")
        return loaded


def create_favorite_workflow_session(
    paths: FavoriteWorkflowPaths,
    state: Mapping[str, Any],
) -> LoadedFavoriteWorkflowSession:
    normalized = _normalize_session_state(state)
    with _session_lock(paths):
        if paths.active_session_path.exists():
            active_pointer = _validate_active_pointer(_read_json_object(paths.active_session_path, "活动收藏会话指针"))
            active = _load_session_unlocked(paths, active_pointer["session_id"])
            if active.state_digest != active_pointer["state_digest"]:
                raise ValueError("活动收藏会话与状态摘要不一致")
            if active.state["status"] not in TERMINAL_STATUSES:
                raise ValueError("已有活动收藏会话，必须先继续或关闭")
        state_path = _state_path(paths, normalized["session_id"])
        snapshot_path = _snapshot_path(paths, normalized["session_id"])
        if state_path.exists() or state_path.parent.exists():
            raise ValueError("收藏会话已存在")
        ensure_private_directory(state_path.parent)
        atomic_write_json(snapshot_path, normalized["candidate_snapshot"], sort_keys=True)
        atomic_write_json(state_path, normalized, sort_keys=True)
        state_digest = content_hash(normalized)
        atomic_write_json(
            paths.active_session_path,
            _active_pointer(session_id=normalized["session_id"], state_digest=state_digest),
            sort_keys=True,
        )
        return _load_session_unlocked(paths, normalized["session_id"])


def update_favorite_workflow_session(
    loaded: LoadedFavoriteWorkflowSession,
    state: Mapping[str, Any],
    *,
    require_unattempted: bool = False,
) -> LoadedFavoriteWorkflowSession:
    normalized = _normalize_session_state(state)
    if normalized["session_id"] != loaded.state["session_id"]:
        raise ValueError("不能改变收藏会话 ID")
    if normalized["candidate_snapshot_digest"] != loaded.state["candidate_snapshot_digest"]:
        raise ValueError("不能修改已冻结候选快照")
    if loaded.state.get("final_selection_digest") != normalized.get("final_selection_digest") and loaded.state.get(
        "final_selection_digest"
    ) is not None:
        raise ValueError("不能修改已冻结最终待收藏名单")
    with _session_lock(loaded.paths):
        current = _load_session_unlocked(loaded.paths, normalized["session_id"])
        if current.state_digest != loaded.state_digest:
            raise ValueError("收藏会话已变化，拒绝覆盖")
        if require_unattempted and (current.state_path.parent / "delivery").exists():
            raise ValueError("本会话已有交付材料；请人工核对，不能通过关闭重试")
        atomic_write_json(current.state_path, normalized, sort_keys=True)
        state_digest = content_hash(normalized)
        atomic_write_json(
            loaded.paths.active_session_path,
            _active_pointer(session_id=normalized["session_id"], state_digest=state_digest),
            sort_keys=True,
        )
        return _load_session_unlocked(loaded.paths, normalized["session_id"])


def build_favorite_candidate_snapshot(
    *,
    inventory: CandidateInventory,
    job_id: str,
    job_title: str,
    rubric_version: str,
    favorite_candidate_ids: Any,
    favorite_statuses: Mapping[str, str],
    created_at: str,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Freeze a local favorite candidate list without consuming publish history."""

    if not isinstance(inventory, CandidateInventory):
        raise ValueError("inventory 必须是 CandidateInventory")
    stable_job_id = _required_text(job_id, "job_id")
    stable_job_title = _required_text(job_title, "job_title")
    stable_rubric_version = _required_text(rubric_version, "rubric_version")
    stable_created_at = _required_text(created_at, "created_at")
    if isinstance(favorite_candidate_ids, (str, bytes)):
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合")
    try:
        stable_favorite_ids = {_required_text(value, "favorite_candidate_ids") for value in favorite_candidate_ids}
    except TypeError as exc:
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合") from exc
    if not isinstance(favorite_statuses, Mapping):
        raise ValueError("favorite_statuses 必须是对象")

    evaluated_count = inventory.evaluated_count(
        stable_job_id,
        rubric_version=stable_rubric_version,
    )
    selected_rows: list[Mapping[str, Any]]
    if evaluated_count:
        selected_rows = inventory.select_evaluated(
            stable_job_id,
            evaluated_count,
            rubric_version=stable_rubric_version,
        )["candidates"]
    else:
        selected_rows = []
    registry_excluded_candidate_ids: list[str] = []
    ledger_excluded_candidate_ids: list[str] = []
    eligible: list[Mapping[str, Any]] = []
    for candidate in selected_rows:
        candidate_id = _required_text(candidate.get("candidate_id"), "candidate_id")
        if _stable_boss_candidate_id(candidate) in stable_favorite_ids:
            registry_excluded_candidate_ids.append(candidate_id)
            continue
        status = str(favorite_statuses.get(candidate_id) or "not_requested").strip()
        if status in LEDGER_EXCLUDED_STATUSES:
            ledger_excluded_candidate_ids.append(candidate_id)
            continue
        eligible.append(candidate)

    rows: list[dict[str, Any]] = []
    for rank, candidate in enumerate(eligible, start=1):
        row = _candidate_row(candidate, rank)
        identifiers = row["boss_identifiers"]
        for field in ("encryptGeekId", "encryptJobId", "securityId"):
            if not str(identifiers.get(field) or "").strip():
                raise ValueError(f"候选人 {row['candidate_id']} 缺少 {field}，不能加入待收藏名单")
        rows.append(row)

    identity = {
        "job_id": stable_job_id,
        "rubric_version": stable_rubric_version,
        "created_at": stable_created_at,
        "candidate_ids": [row["candidate_id"] for row in rows],
        "scores": [row["score"] for row in rows],
        "registry_excluded_candidate_ids": registry_excluded_candidate_ids,
        "ledger_excluded_candidate_ids": ledger_excluded_candidate_ids,
    }
    stable_snapshot_id = (
        _safe_id(snapshot_id, "snapshot_id")
        if snapshot_id is not None
        else "favorite-snapshot-" + content_hash(identity)[:16]
    )
    shortage_count = max(0, SHORTLIST_SIZE - len(rows))
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": SNAPSHOT_CONTRACT,
        "snapshot_id": stable_snapshot_id,
        "created_at": stable_created_at,
        "job_id": stable_job_id,
        "job_title": stable_job_title,
        "rubric_version": stable_rubric_version,
        "selection_scope": "all_evaluated",
        "requested_count": SHORTLIST_SIZE,
        "actual_count": len(rows),
        "shortage_count": shortage_count,
        "shortage_reason": "evaluated_inventory_exhausted" if shortage_count else None,
        "registry_excluded_candidate_ids": registry_excluded_candidate_ids,
        "ledger_excluded_candidate_ids": ledger_excluded_candidate_ids,
        "candidates": rows,
    }


def _normalize_final_selection(
    value: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    snapshot_digest: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("最终待收藏名单必须是对象")
    selection = dict(value)
    if selection.get("schema_version") != SCHEMA_VERSION or selection.get("contract") != FINAL_SELECTION_CONTRACT:
        raise ValueError("最终待收藏名单 contract 无效")
    if _safe_id(selection.get("snapshot_id"), "final_selection.snapshot_id") != _safe_id(
        snapshot.get("snapshot_id"), "candidate_snapshot.snapshot_id"
    ):
        raise ValueError("最终待收藏名单与候选快照不匹配")
    if _digest(selection.get("candidate_snapshot_digest"), "final_selection.candidate_snapshot_digest") != snapshot_digest:
        raise ValueError("最终待收藏名单与候选快照摘要不匹配")
    candidates = selection.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("最终待收藏名单 candidates 必须是列表")
    if selection.get("actual_count") != len(candidates) or len(candidates) > SHORTLIST_SIZE:
        raise ValueError("最终待收藏名单 actual_count 无效")
    if selection.get("requested_count") != SHORTLIST_SIZE:
        raise ValueError("最终待收藏名单 requested_count 无效")
    shortage_count = SHORTLIST_SIZE - len(candidates)
    if selection.get("shortage_count") != shortage_count:
        raise ValueError("最终待收藏名单 shortage_count 无效")
    if selection.get("shortage_reason") != ("ranked_pool_exhausted" if shortage_count else None):
        raise ValueError("最终待收藏名单 shortage_reason 无效")
    _required_text(selection.get("selected_at"), "final_selection.selected_at")
    snapshot_rows = snapshot.get("candidates")
    if not isinstance(snapshot_rows, list):
        raise ValueError("候选快照 candidates 必须是列表")
    rows_by_rank = {row.get("rank"): row for row in snapshot_rows if isinstance(row, Mapping)}
    if len(rows_by_rank) != len(snapshot_rows):
        raise ValueError("候选快照 rank 无效")
    seen_ranks: set[int] = set()
    selected_ranks: list[int] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("最终待收藏名单候选人无效")
        rank = candidate.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0 or rank in seen_ranks:
            raise ValueError("最终待收藏名单 rank 无效")
        if rows_by_rank.get(rank) != candidate:
            raise ValueError("最终待收藏名单必须是候选快照的精确子集")
        seen_ranks.add(rank)
        selected_ranks.append(rank)
    if selected_ranks != sorted(selected_ranks):
        raise ValueError("最终待收藏名单必须保持候选快照顺序")
    for field in ("registry_excluded_candidate_ids", "ledger_excluded_candidate_ids"):
        if not isinstance(selection.get(field), list) or not all(isinstance(item, str) for item in selection[field]):
            raise ValueError(f"最终待收藏名单 {field} 无效")
    return selection


def build_favorite_final_selection(
    snapshot: Mapping[str, Any],
    *,
    favorite_candidate_ids: Any,
    favorite_statuses: Mapping[str, str],
    selected_at: str,
) -> dict[str, Any]:
    """Freeze the highest-ranked actionable candidates from one immutable pool."""

    batch = favorite_snapshot_batch(snapshot)
    if isinstance(favorite_candidate_ids, (str, bytes)):
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合")
    try:
        stable_favorite_ids = {_required_text(value, "favorite_candidate_ids") for value in favorite_candidate_ids}
    except TypeError as exc:
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合") from exc
    if not isinstance(favorite_statuses, Mapping):
        raise ValueError("favorite_statuses 必须是对象")

    registry_excluded_candidate_ids: list[str] = []
    ledger_excluded_candidate_ids: list[str] = []
    selected: list[dict[str, Any]] = []
    for candidate in batch["candidates"]:
        candidate_id = _required_text(candidate.get("candidate_id"), "candidate_id")
        identifiers = candidate.get("boss_identifiers")
        if not isinstance(identifiers, Mapping):
            raise ValueError(f"候选人 {candidate_id} 缺少 boss_identifiers")
        if _required_text(identifiers.get("encryptGeekId"), f"{candidate_id}.encryptGeekId") in stable_favorite_ids:
            registry_excluded_candidate_ids.append(candidate_id)
            continue
        if str(favorite_statuses.get(candidate_id) or "not_requested").strip() in LEDGER_EXCLUDED_STATUSES:
            ledger_excluded_candidate_ids.append(candidate_id)
            continue
        if len(selected) < SHORTLIST_SIZE:
            selected.append(dict(candidate))

    shortage_count = SHORTLIST_SIZE - len(selected)
    selection = {
        "schema_version": SCHEMA_VERSION,
        "contract": FINAL_SELECTION_CONTRACT,
        "snapshot_id": _safe_id(snapshot.get("snapshot_id"), "candidate_snapshot.snapshot_id"),
        "candidate_snapshot_digest": content_hash(snapshot),
        "selected_at": _required_text(selected_at, "selected_at"),
        "requested_count": SHORTLIST_SIZE,
        "actual_count": len(selected),
        "shortage_count": shortage_count,
        "shortage_reason": "ranked_pool_exhausted" if shortage_count else None,
        "registry_excluded_candidate_ids": registry_excluded_candidate_ids,
        "ledger_excluded_candidate_ids": ledger_excluded_candidate_ids,
        "candidates": selected,
    }
    return _normalize_final_selection(selection, snapshot=snapshot, snapshot_digest=content_hash(snapshot))


def favorite_snapshot_batch(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the hidden snapshot to the existing immutable sync/delivery binding."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("候选快照必须是对象")
    if snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("contract") != SNAPSHOT_CONTRACT:
        raise ValueError("候选快照 contract 无效")
    snapshot_id = _safe_id(snapshot.get("snapshot_id"), "snapshot_id")
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("候选快照 candidates 必须是列表")
    actual_count = snapshot.get("actual_count")
    if actual_count != len(candidates):
        raise ValueError("候选快照 actual_count 不一致")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "candidate_shortlist_batch",
        "batch_id": "favorite-" + snapshot_id,
        "published_at": _required_text(snapshot.get("created_at"), "candidate_snapshot.created_at"),
        "job_id": _required_text(snapshot.get("job_id"), "candidate_snapshot.job_id"),
        "job_title": _required_text(snapshot.get("job_title"), "candidate_snapshot.job_title"),
        "rubric_version": _required_text(snapshot.get("rubric_version"), "candidate_snapshot.rubric_version"),
        "actual_count": actual_count,
        "candidates": [dict(candidate) for candidate in candidates],
    }
