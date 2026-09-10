from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from boss_hire.local_security import atomic_write_json, ensure_private_directory, ensure_private_file
from boss_hire.state_store import now_iso


SCHEMA_VERSION = 1
REGISTRY_CONTRACT = "boss_favorite_registry"
CHECKPOINT_CONTRACT = "boss_favorite_sync_checkpoint"
ALLOWED_SOURCES = {
    "favorite_confirmed",
    "legacy_ledger_import",
    "list_sync",
    "manual_verified",
}
COMPLETE_SYNC_STATUSES = {"anchor_reached", "end_reached"}


def _text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} cannot be empty")
    return result


def _candidate_ids(values: Sequence[str], field: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    result = [_text(value, f"{field}[{index}]") for index, value in enumerate(values)]
    if len(set(result)) != len(result):
        raise ValueError(f"{field} cannot contain duplicate candidate ids")
    return result


class FavoriteRegistry:
    def __init__(self, root: Path, *, account_key: str) -> None:
        self.root = Path(root)
        self.account_key = _text(account_key, "account_key")
        self.registry_path = self.root / "registry.json"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.lock_path = self.root / ".state.lock"
        self._validate_existing_files()

    def _empty_registry(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "contract": REGISTRY_CONTRACT,
            "account_key": self.account_key,
            "candidates": {},
        }

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return self._empty_registry()
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"favorite registry is invalid: {self.registry_path}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("contract") != REGISTRY_CONTRACT
            or value.get("account_key") != self.account_key
            or not isinstance(value.get("candidates"), dict)
        ):
            raise RuntimeError("favorite registry schema or account binding is invalid")
        return value

    def _load_checkpoint(self) -> dict[str, Any] | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            value = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"favorite checkpoint is invalid: {self.checkpoint_path}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("contract") != CHECKPOINT_CONTRACT
            or value.get("account_key") != self.account_key
            or value.get("sync_status") not in COMPLETE_SYNC_STATUSES
            or not isinstance(value.get("anchor_group"), list)
        ):
            raise RuntimeError("favorite checkpoint schema or account binding is invalid")
        _candidate_ids(value["anchor_group"], "checkpoint.anchor_group")
        return value

    def _validate_existing_files(self) -> None:
        self._load_registry()
        self._load_checkpoint()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        ensure_private_directory(self.root)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def record_candidates(
        self,
        candidate_ids: Sequence[str],
        *,
        source: str,
        receipt_id: str,
        observed_at: str | None = None,
    ) -> int:
        stable_ids = _candidate_ids(candidate_ids, "candidate_ids")
        stable_source = _text(source, "source")
        if stable_source not in ALLOWED_SOURCES:
            raise ValueError(f"unsupported favorite registry source: {stable_source}")
        stable_receipt_id = _text(receipt_id, "receipt_id")
        stable_observed_at = observed_at or now_iso()
        new_count = 0
        with self._locked():
            state = self._load_registry()
            candidates = state["candidates"]
            for candidate_id in stable_ids:
                existing = candidates.get(candidate_id)
                if existing is None:
                    candidates[candidate_id] = {
                        "candidate_id": candidate_id,
                        "sources": [stable_source],
                        "first_seen_at": stable_observed_at,
                        "last_seen_at": stable_observed_at,
                        "first_receipt_id": stable_receipt_id,
                        "last_receipt_id": stable_receipt_id,
                    }
                    new_count += 1
                    continue
                if not isinstance(existing, Mapping):
                    raise RuntimeError(f"favorite registry candidate is invalid: {candidate_id}")
                sources = existing.get("sources")
                if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                    raise RuntimeError(f"favorite registry candidate sources are invalid: {candidate_id}")
                candidates[candidate_id] = {
                    **dict(existing),
                    "candidate_id": candidate_id,
                    "sources": sorted(set(sources) | {stable_source}),
                    "last_seen_at": stable_observed_at,
                    "last_receipt_id": stable_receipt_id,
                }
            atomic_write_json(self.registry_path, state, sort_keys=True)
            ensure_private_file(self.registry_path)
        return new_count

    def contains(self, candidate_id: str) -> bool:
        stable_candidate_id = _text(candidate_id, "candidate_id")
        with self._locked():
            return stable_candidate_id in self._load_registry()["candidates"]

    @contextmanager
    def locked_contains(self, candidate_id: str) -> Iterator[bool]:
        """Check one stable ID while preventing concurrent registry mutation."""

        stable_candidate_id = _text(candidate_id, "candidate_id")
        with self._locked():
            yield stable_candidate_id in self._load_registry()["candidates"]

    def known_candidate_ids(self) -> frozenset[str]:
        with self._locked():
            return frozenset(self._load_registry()["candidates"])

    def record(self, candidate_id: str) -> dict[str, Any] | None:
        stable_candidate_id = _text(candidate_id, "candidate_id")
        with self._locked():
            value = self._load_registry()["candidates"].get(stable_candidate_id)
            return deepcopy(dict(value)) if isinstance(value, Mapping) else None

    def checkpoint(self) -> dict[str, Any] | None:
        with self._locked():
            value = self._load_checkpoint()
            return deepcopy(value) if value is not None else None

    def save_complete_checkpoint(
        self,
        *,
        anchor_group: Sequence[str],
        receipt_id: str,
        sync_status: str,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        stable_anchor = _candidate_ids(anchor_group, "anchor_group")
        if len(stable_anchor) > 10:
            raise ValueError("anchor_group cannot contain more than 10 candidate ids")
        stable_status = _text(sync_status, "sync_status")
        if stable_status not in COMPLETE_SYNC_STATUSES:
            raise ValueError("only a complete favorite sync may advance the checkpoint")
        checkpoint = {
            "schema_version": SCHEMA_VERSION,
            "contract": CHECKPOINT_CONTRACT,
            "account_key": self.account_key,
            "initialized_complete": True,
            "anchor_group": stable_anchor,
            "receipt_id": _text(receipt_id, "receipt_id"),
            "sync_status": stable_status,
            "completed_at": completed_at or now_iso(),
        }
        with self._locked():
            atomic_write_json(self.checkpoint_path, checkpoint, sort_keys=True)
            ensure_private_file(self.checkpoint_path)
        return deepcopy(checkpoint)

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            return {
                "registry": deepcopy(self._load_registry()),
                "checkpoint": deepcopy(self._load_checkpoint()),
            }


def read_known_candidate_ids(root: Path, *, account_key: str) -> frozenset[str]:
    """Read the local registry without creating a lock or any account-state files."""
    stable_account_key = _text(account_key, "account_key")
    registry_path = Path(root) / "registry.json"
    if not registry_path.exists():
        return frozenset()
    if registry_path.is_symlink():
        raise RuntimeError(f"favorite registry is invalid: {registry_path}")
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"favorite registry is invalid: {registry_path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("contract") != REGISTRY_CONTRACT
        or value.get("account_key") != stable_account_key
        or not isinstance(value.get("candidates"), dict)
    ):
        raise RuntimeError("favorite registry schema or account binding is invalid")
    return frozenset(str(candidate_id) for candidate_id in value["candidates"])
