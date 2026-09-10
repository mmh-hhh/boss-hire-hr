from __future__ import annotations

import fcntl
import json
import os
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Collection, Iterator, Mapping, Sequence

from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.favorite_sync import validate_favorite_sync_receipt
from boss_hire.local_security import atomic_write_json, ensure_private_directory, ensure_private_file
from boss_hire.state_store import content_hash, now_iso


PLAN_CONTRACT = "boss_favorite_delivery_plan"
LEDGER_CONTRACT = "boss_favorite_delivery_ledger"
SCHEMA_VERSION = 1
CONFIRMED_STATUSES = {"favorite_confirmed", "already_confirmed", "manual_verified"}
BLOCKED_STATUSES = {"write_reserved", "favorite_failed", "favorite_unknown"}
ALLOWED_STATUSES = {
    "not_requested",
    "already_confirmed",
    "write_reserved",
    "favorite_confirmed",
    "favorite_failed",
    "favorite_unknown",
    "manual_verified",
}


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _positive_rank(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} 必须是正整数")
    return value


def _candidate_rows(batch: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if batch.get("contract") != "candidate_shortlist_batch":
        raise ValueError("batch 必须使用 candidate_shortlist_batch 契约")
    batch_id = _text(batch.get("batch_id"), "batch_id")
    rows = batch.get("candidates")
    if not isinstance(rows, list):
        raise ValueError(f"发布批次 {batch_id} candidates 必须是列表")
    actual_count = batch.get("actual_count")
    if actual_count != len(rows):
        raise ValueError("发布批次 actual_count 与 candidates 不一致")
    return [row for row in rows if isinstance(row, Mapping)]


def _selected_candidate(
    row: Mapping[str, Any],
    *,
    status: str,
    retry_definite_failures: bool,
) -> dict[str, Any]:
    candidate_id = _text(row.get("candidate_id"), "candidate_id")
    rank = _positive_rank(row.get("rank"), f"{candidate_id}.rank")
    identifiers = row.get("boss_identifiers")
    if not isinstance(identifiers, Mapping):
        raise ValueError(f"候选人 {candidate_id} 缺少 boss_identifiers")
    encrypt_geek_id = _text(identifiers.get("encryptGeekId"), f"{candidate_id}.encryptGeekId")
    security_id = _text(identifiers.get("securityId"), f"{candidate_id}.securityId")
    encrypt_job_id = _text(identifiers.get("encryptJobId"), f"{candidate_id}.encryptJobId")
    if status in BLOCKED_STATUSES and not (
        retry_definite_failures and status == "favorite_failed"
    ):
        raise ValueError(f"候选人 {candidate_id} 处于 {status}，必须人工处理")
    action = "already_confirmed" if status in CONFIRMED_STATUSES else "favorite"
    display = row.get("display") if isinstance(row.get("display"), Mapping) else {}
    return {
        "candidate_id": candidate_id,
        "rank": rank,
        "name": str(display.get("name") or "").strip(),
        "score": row.get("score"),
        "summary": str(row.get("summary") or "").strip(),
        "encrypt_geek_id": encrypt_geek_id,
        "security_id": security_id,
        "encrypt_job_id": encrypt_job_id,
        "favorite_status": status,
        "action": action,
    }


def build_favorite_delivery_plan(
    batch: Mapping[str, Any],
    *,
    selected_ranks: Sequence[int],
    expected_batch_digest: str | None = None,
    favorite_statuses: Mapping[str, str] | None = None,
    retry_definite_failures: bool = False,
) -> dict[str, Any]:
    """Build an immutable local selection from a published shortlist batch."""

    rows = _candidate_rows(batch)
    batch_digest = content_hash(batch)
    if expected_batch_digest is not None and expected_batch_digest != batch_digest:
        raise ValueError("发布批次摘要不一致")
    if not isinstance(selected_ranks, Sequence) or isinstance(selected_ranks, (str, bytes)):
        raise ValueError("selected_ranks 必须是编号列表")
    normalized_ranks = [
        _positive_rank(value, f"selected_ranks[{index}]")
        for index, value in enumerate(selected_ranks)
    ]
    if not normalized_ranks:
        raise ValueError("至少选择一名候选人")
    if len(set(normalized_ranks)) != len(normalized_ranks):
        raise ValueError("收藏编号不能重复")
    if not isinstance(retry_definite_failures, bool):
        raise ValueError("retry_definite_failures 必须是布尔值")

    rows_by_rank: dict[int, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        rank = _positive_rank(row.get("rank"), f"candidates[{index}].rank")
        if rank in rows_by_rank:
            raise ValueError(f"发布批次候选排名重复：{rank}")
        rows_by_rank[rank] = row
    missing = [rank for rank in normalized_ranks if rank not in rows_by_rank]
    if missing:
        raise ValueError("收藏编号超出发布批次：" + ",".join(map(str, missing)))

    statuses = favorite_statuses or {}
    selected: list[dict[str, Any]] = []
    # BOSS puts the most recently favorited candidate first. Write lower-scored
    # candidates first so rank 1 (the highest score) is the final write.
    for rank in sorted(normalized_ranks, reverse=True):
        row = rows_by_rank[rank]
        candidate_id = _text(row.get("candidate_id"), "candidate_id")
        status = str(statuses.get(candidate_id) or "not_requested").strip()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"候选人 {candidate_id} favorite status 无效：{status}")
        selected.append(
            _selected_candidate(
                row,
                status=status,
                retry_definite_failures=retry_definite_failures,
            )
        )

    not_selected = []
    selected_set = set(normalized_ranks)
    for rank, row in sorted(rows_by_rank.items()):
        if rank in selected_set:
            continue
        display = row.get("display") if isinstance(row.get("display"), Mapping) else {}
        not_selected.append(
            {
                "candidate_id": _text(row.get("candidate_id"), "candidate_id"),
                "rank": rank,
                "name": str(display.get("name") or "").strip(),
            }
        )

    batch_id = _text(batch.get("batch_id"), "batch_id")
    identity = {
        "batch_id": batch_id,
        "batch_digest": batch_digest,
        "candidate_ids": [row["candidate_id"] for row in selected],
        "retry_definite_failures": retry_definite_failures,
        "previous_statuses": [row["favorite_status"] for row in selected],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": PLAN_CONTRACT,
        "plan_id": "favorite-" + content_hash(identity)[:16],
        "batch_id": batch_id,
        "batch_digest": batch_digest,
        "published_at": _text(batch.get("published_at"), "published_at"),
        "job_id": _text(batch.get("job_id"), "job_id"),
        "job_title": _text(batch.get("job_title"), "job_title"),
        "rubric_version": _text(batch.get("rubric_version"), "rubric_version"),
        "retry_definite_failures": retry_definite_failures,
        "published_count": len(rows),
        "selected_count": len(selected),
        "not_selected_count": len(not_selected),
        "candidates": selected,
        "not_selected_candidates": not_selected,
    }


def build_synced_favorite_delivery_plan(
    batch: Mapping[str, Any],
    *,
    selected_ranks: Sequence[int],
    sync_receipt: Mapping[str, Any],
    account_key: str,
    board_date: str,
    favorite_candidate_ids: Collection[str],
    expected_batch_digest: str | None = None,
    favorite_statuses: Mapping[str, str] | None = None,
    retry_definite_failures: bool = False,
) -> dict[str, Any]:
    """Build a favorite plan bound to one complete same-batch sync receipt."""

    stable_account_key = _text(account_key, "account_key")
    stable_board_date = _text(board_date, "board_date")
    batch_id = _text(batch.get("batch_id"), "batch_id")
    batch_digest = content_hash(batch)
    receipt = validate_favorite_sync_receipt(
        sync_receipt,
        account_key=stable_account_key,
        board_date=stable_board_date,
        purpose="favorite_delivery",
        require_complete=True,
        batch_id=batch_id,
        batch_digest=batch_digest,
    )
    if isinstance(favorite_candidate_ids, (str, bytes)):
        raise ValueError("favorite_candidate_ids 必须是稳定候选人 ID 集合")
    stable_favorite_ids = {
        _text(value, f"favorite_candidate_ids[{index}]")
        for index, value in enumerate(favorite_candidate_ids)
    }
    plan = build_favorite_delivery_plan(
        batch,
        selected_ranks=selected_ranks,
        expected_batch_digest=expected_batch_digest,
        favorite_statuses=favorite_statuses,
        retry_definite_failures=retry_definite_failures,
    )
    candidates: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        updated = dict(candidate)
        if updated["encrypt_geek_id"] in stable_favorite_ids:
            updated["favorite_status"] = "already_confirmed"
            updated["action"] = "already_confirmed"
        candidates.append(updated)
    receipt_digest = content_hash(receipt)
    identity = {
        "batch_id": batch_id,
        "batch_digest": batch_digest,
        "sync_receipt_digest": receipt_digest,
        "candidates": [
            {
                "candidate_id": row["candidate_id"],
                "action": row["action"],
                "favorite_status": row["favorite_status"],
            }
            for row in candidates
        ],
        "retry_definite_failures": retry_definite_failures,
    }
    return {
        **plan,
        "plan_id": "favorite-" + content_hash(identity)[:16],
        "account_key": stable_account_key,
        "board_date": stable_board_date,
        "favorite_sync_receipt_id": receipt["plan_id"],
        "favorite_sync_receipt_digest": receipt_digest,
        "favorite_sync_completed_at": receipt["completed_at"],
        "favorite_sync_status": receipt["status"],
        "candidates": candidates,
        "actionable_count": sum(row["action"] == "favorite" for row in candidates),
        "already_confirmed_count": sum(
            row["action"] == "already_confirmed" for row in candidates
        ),
    }


class FavoriteDeliveryLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "contract": LEDGER_CONTRACT,
                "candidates": {},
            }
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"收藏交付账本损坏：{self.path}") from exc
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != SCHEMA_VERSION
            or state.get("contract") != LEDGER_CONTRACT
            or not isinstance(state.get("candidates"), dict)
        ):
            raise RuntimeError("收藏交付账本 schema 无效")
        return state

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        ensure_private_directory(self.path.parent)
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        committed = False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._state = self._load()
            yield
            committed = True
        finally:
            try:
                if committed:
                    atomic_write_json(self.path, self._state, sort_keys=True)
                    ensure_private_file(self.path)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def status(self, candidate_id: str) -> str:
        record = self._state["candidates"].get(_text(candidate_id, "candidate_id"))
        if not isinstance(record, Mapping):
            return "not_requested"
        status = str(record.get("status") or "not_requested")
        if status not in ALLOWED_STATUSES:
            raise RuntimeError(f"收藏交付账本状态无效：{status}")
        return status

    def statuses(self, candidate_ids: Sequence[str]) -> dict[str, str]:
        return {candidate_id: self.status(candidate_id) for candidate_id in candidate_ids}

    def set_status(
        self,
        candidate_id: str,
        status: str,
        *,
        batch_id: str,
        operation_key: str,
        recorded_at: str | None = None,
    ) -> None:
        stable_candidate_id = _text(candidate_id, "candidate_id")
        stable_status = _text(status, "status")
        if stable_status not in ALLOWED_STATUSES - {"not_requested"}:
            raise ValueError(f"收藏状态无效：{stable_status}")
        self._state["candidates"][stable_candidate_id] = {
            "candidate_id": stable_candidate_id,
            "status": stable_status,
            "batch_id": _text(batch_id, "batch_id"),
            "operation_key": _text(operation_key, "operation_key"),
            "recorded_at": recorded_at or now_iso(),
        }

    def reserve_write(
        self,
        candidate_id: str,
        *,
        batch_id: str,
        operation_key: str,
        retry_definite_failures: bool = False,
        recorded_at: str | None = None,
    ) -> None:
        stable_candidate_id = _text(candidate_id, "candidate_id")
        stable_batch_id = _text(batch_id, "batch_id")
        stable_operation_key = _text(operation_key, "operation_key")
        if not isinstance(retry_definite_failures, bool):
            raise ValueError("retry_definite_failures 必须是布尔值")
        with self._locked_state():
            existing = self._state["candidates"].get(stable_candidate_id)
            attempt_history: list[dict[str, Any]] = []
            if isinstance(existing, Mapping):
                status = str(existing.get("status") or "not_requested")
                if status != "not_requested" and not (
                    retry_definite_failures and status == "favorite_failed"
                ):
                    raise RuntimeError(
                        f"candidate {stable_candidate_id} favorite write was already attempted: {status}"
                    )
                raw_history = existing.get("attempt_history") or []
                if not isinstance(raw_history, list):
                    raise RuntimeError("收藏交付账本 attempt_history 无效")
                attempt_history = deepcopy(raw_history)
                if status == "favorite_failed":
                    attempt_history.append(
                        {
                            key: deepcopy(value)
                            for key, value in existing.items()
                            if key != "attempt_history"
                        }
                    )
            reservation = {
                "candidate_id": stable_candidate_id,
                "status": "write_reserved",
                "batch_id": stable_batch_id,
                "operation_key": stable_operation_key,
                "recorded_at": recorded_at or now_iso(),
            }
            if attempt_history:
                reservation["attempt_history"] = attempt_history
            self._state["candidates"][stable_candidate_id] = reservation

    def finalize_write(
        self,
        candidate_id: str,
        status: str,
        *,
        batch_id: str,
        operation_key: str,
        recorded_at: str | None = None,
    ) -> None:
        stable_candidate_id = _text(candidate_id, "candidate_id")
        stable_status = _text(status, "status")
        if stable_status not in {"favorite_confirmed", "favorite_failed", "favorite_unknown"}:
            raise ValueError(f"收藏写入最终状态无效：{stable_status}")
        stable_batch_id = _text(batch_id, "batch_id")
        stable_operation_key = _text(operation_key, "operation_key")
        with self._locked_state():
            existing = self._state["candidates"].get(stable_candidate_id)
            if not isinstance(existing, Mapping) or existing.get("status") != "write_reserved":
                raise RuntimeError(
                    f"candidate {stable_candidate_id} has no matching write reservation"
                )
            if (
                existing.get("batch_id") != stable_batch_id
                or existing.get("operation_key") != stable_operation_key
            ):
                raise RuntimeError(
                    f"candidate {stable_candidate_id} write reservation operation does not match"
                )
            self._state["candidates"][stable_candidate_id] = {
                **dict(existing),
                "status": stable_status,
                "finalized_at": recorded_at or now_iso(),
            }

    def record(self, candidate_id: str) -> dict[str, Any] | None:
        value = self._state["candidates"].get(_text(candidate_id, "candidate_id"))
        return deepcopy(dict(value)) if isinstance(value, Mapping) else None

    def import_records(
        self,
        records: Mapping[str, Mapping[str, Any]],
        *,
        migration_id: str,
        migrated_at: str | None = None,
    ) -> int:
        if not isinstance(records, Mapping):
            raise ValueError("legacy favorite records must be an object")
        stable_migration_id = _text(migration_id, "migration_id")
        stable_migrated_at = migrated_at or now_iso()
        imported_count = 0
        with self._locked_state():
            candidates = self._state["candidates"]
            for raw_candidate_id, raw_record in records.items():
                candidate_id = _text(raw_candidate_id, "candidate_id")
                if not isinstance(raw_record, Mapping):
                    raise ValueError(f"legacy favorite record is invalid: {candidate_id}")
                record = deepcopy(dict(raw_record))
                if _text(record.get("candidate_id"), f"{candidate_id}.candidate_id") != candidate_id:
                    raise ValueError(f"legacy favorite record candidate mismatch: {candidate_id}")
                status = _text(record.get("status"), f"{candidate_id}.status")
                if status not in ALLOWED_STATUSES - {"not_requested"}:
                    raise ValueError(f"legacy favorite status is invalid: {status}")
                existing = candidates.get(candidate_id)
                if isinstance(existing, Mapping):
                    if existing.get("status") != status:
                        raw_history = existing.get("attempt_history") or []
                        if not isinstance(raw_history, list):
                            raise RuntimeError("收藏交付账本 attempt_history 无效")
                        source_identity = (
                            candidate_id,
                            status,
                            record.get("batch_id"),
                            record.get("operation_key"),
                        )
                        if any(
                            isinstance(attempt, Mapping)
                            and (
                                attempt.get("candidate_id"),
                                attempt.get("status"),
                                attempt.get("batch_id"),
                                attempt.get("operation_key"),
                            )
                            == source_identity
                            for attempt in raw_history
                        ):
                            continue
                        raise RuntimeError(
                            f"favorite ledger migration conflict for {candidate_id}: "
                            f"{existing.get('status')} != {status}"
                        )
                    continue
                candidates[candidate_id] = {
                    **record,
                    "candidate_id": candidate_id,
                    "status": status,
                    "migration_id": stable_migration_id,
                    "migrated_at": stable_migrated_at,
                }
                imported_count += 1
        return imported_count

    def save(self) -> None:
        atomic_write_json(self.path, self._state, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._state)


def migrate_favorite_delivery_ledger(
    *,
    source_path: Path,
    target_path: Path,
    registry: FavoriteRegistry,
    migration_id: str,
    stable_candidate_ids: Mapping[str, str] | None = None,
    migrated_at: str | None = None,
) -> dict[str, Any]:
    stable_migration_id = _text(migration_id, "migration_id")
    stable_migrated_at = migrated_at or now_iso()
    source = FavoriteDeliveryLedger(source_path)
    source_records = source.to_dict()["candidates"]
    raw_stable_ids = stable_candidate_ids or {}
    if not isinstance(raw_stable_ids, Mapping):
        raise ValueError("stable_candidate_ids must be an object")
    stable_ids = {
        _text(candidate_id, "stable_candidate_ids candidate_id"): _text(
            stable_id,
            f"stable_candidate_ids[{candidate_id}]",
        )
        for candidate_id, stable_id in raw_stable_ids.items()
    }
    target = FavoriteDeliveryLedger(target_path)
    imported_count = target.import_records(
        source_records,
        migration_id=stable_migration_id,
        migrated_at=stable_migrated_at,
    )

    confirmed_ids: list[str] = []
    manual_ids: list[str] = []
    blocked_ids: list[str] = []
    for candidate_id, record in source_records.items():
        status = str(record.get("status") or "")
        if status == "manual_verified":
            manual_ids.append(candidate_id)
        elif status in CONFIRMED_STATUSES:
            confirmed_ids.append(candidate_id)
        elif status in BLOCKED_STATUSES:
            blocked_ids.append(candidate_id)
    confirmed_registry_ids = sorted(
        {stable_ids[candidate_id] for candidate_id in confirmed_ids if candidate_id in stable_ids}
    )
    manual_registry_ids = sorted(
        {stable_ids[candidate_id] for candidate_id in manual_ids if candidate_id in stable_ids}
    )
    if confirmed_registry_ids:
        registry.record_candidates(
            confirmed_registry_ids,
            source="favorite_confirmed",
            receipt_id=stable_migration_id,
            observed_at=stable_migrated_at,
        )
    if manual_registry_ids:
        registry.record_candidates(
            manual_registry_ids,
            source="manual_verified",
            receipt_id=stable_migration_id,
            observed_at=stable_migrated_at,
        )
    return {
        "contract": "boss_favorite_ledger_migration_receipt",
        "migration_id": stable_migration_id,
        "migrated_at": stable_migrated_at,
        "source_path": str(Path(source_path)),
        "target_path": str(Path(target_path)),
        "source_candidate_count": len(source_records),
        "imported_count": imported_count,
        "confirmed_registry_count": len(confirmed_registry_ids),
        "manual_registry_count": len(manual_registry_ids),
        "unmapped_confirmed_count": len(confirmed_ids) - len(confirmed_registry_ids),
        "unmapped_manual_count": len(manual_ids) - len(manual_registry_ids),
        "blocked_preserved_count": len(blocked_ids),
        "source_deleted": False,
    }
