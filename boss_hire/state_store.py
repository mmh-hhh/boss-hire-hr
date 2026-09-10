from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from boss_hire.local_security import atomic_write_json


SCHEMA_VERSION = 1
CARD_VOLATILE_KEYS = {
    "securityId",
    "sourcePage",
    "sourceRank",
    "page",
    "rank",
    "traceId",
    "traceid",
    "lastLoginTime",
    "lastActiveTime",
    "activeTime",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _canonical(value: Any, *, excluded_keys: set[str] | None = None) -> Any:
    excluded = excluded_keys or set()
    if isinstance(value, dict):
        return {
            str(key): _canonical(child, excluded_keys=excluded)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in excluded
        }
    if isinstance(value, list):
        return [_canonical(item, excluded_keys=excluded) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item, excluded_keys=excluded) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def content_hash(value: Any, *, excluded_keys: set[str] | None = None) -> str:
    payload = json.dumps(
        _canonical(value, excluded_keys=excluded_keys),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def jd_hash(jd_text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in jd_text.strip().splitlines())
    return content_hash(normalized)


def card_hash(card: dict[str, Any]) -> str:
    return content_hash(card, excluded_keys=CARD_VOLATILE_KEYS)


def resume_hash(resume: dict[str, Any]) -> str:
    return content_hash(resume)


def evaluation_fingerprint(
    *,
    jd_digest: str,
    rubric: dict[str, Any],
    prompt: str,
    model: str,
    resume_digest: str,
) -> str:
    return content_hash(
        {
            "jd_hash": jd_digest,
            "rubric": rubric,
            "prompt": prompt,
            "model": model,
            "resume_hash": resume_digest,
        }
    )


def stable_candidate_id(card: dict[str, Any]) -> str:
    nested = card.get("geekCard") if isinstance(card.get("geekCard"), dict) else {}
    candidate_id = str(card.get("encryptGeekId") or nested.get("encryptGeekId") or "").strip()
    if not candidate_id:
        raise ValueError("candidate card is missing stable encryptGeekId")
    return candidate_id


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": {},
        "jobs": {},
        "boss_access": {"stops": {}, "last_stop_run_id": None},
    }


class StateStore:
    def __init__(self, path: Path, *, clock: Callable[[], str] = now_iso) -> None:
        self.path = path
        self.clock = clock
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid state JSON: {self.path}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported state schema: {data!r}")
        if not isinstance(data.get("candidates"), dict) or not isinstance(data.get("jobs"), dict):
            raise RuntimeError(f"invalid state shape: {data!r}")
        boss_access = data.setdefault("boss_access", {"stops": {}, "last_stop_run_id": None})
        if not isinstance(boss_access, dict):
            raise RuntimeError(f"invalid boss_access state: {boss_access!r}")
        boss_access.setdefault("stops", {})
        boss_access.setdefault("last_stop_run_id", None)
        if not isinstance(boss_access["stops"], dict):
            raise RuntimeError(f"invalid boss_access stops: {boss_access['stops']!r}")
        return data

    def save(self) -> None:
        atomic_write_json(self.path, self.data, sort_keys=True)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data)

    def record_job(self, job_id: str, jd_digest: str) -> str:
        jobs = self.data["jobs"]
        previous = jobs.get(job_id)
        current_time = self.clock()
        if previous is None:
            jobs[job_id] = {
                "jd_hash": jd_digest,
                "first_seen_at": current_time,
                "last_seen_at": current_time,
                "candidates": {},
            }
            return "new"
        previous["last_seen_at"] = current_time
        if previous.get("jd_hash") == jd_digest:
            return "unchanged"
        previous["jd_hash"] = jd_digest
        return "jd_changed"

    def classify_card(self, job_id: str, candidate_id: str, digest: str) -> str:
        job = self._job(job_id)
        previous = job["candidates"].get(candidate_id)
        if previous is None:
            return "new"
        if previous.get("card_hash") == digest:
            return "unchanged"
        return "card_changed"

    def record_card(
        self,
        job_id: str,
        candidate_id: str,
        digest: str,
        *,
        screening_status: str | None = None,
    ) -> None:
        job = self._job(job_id)
        current_time = self.clock()
        candidate = job["candidates"].setdefault(
            candidate_id,
            {
                "first_seen_at": current_time,
                "evaluation_fingerprint": None,
                "evaluation_path": None,
            },
        )
        candidate["card_hash"] = digest
        candidate["last_seen_at"] = current_time
        if screening_status:
            candidate["screening_status"] = screening_status

    def screening_status(self, job_id: str, candidate_id: str) -> str | None:
        value = self._job(job_id)["candidates"].get(candidate_id) or {}
        status = value.get("screening_status")
        return str(status) if status else None

    def classify_source_card(
        self,
        job_id: str,
        candidate_id: str,
        source: str,
        digest: str,
    ) -> str:
        record = self._job(job_id)["candidates"].get(candidate_id) or {}
        sources = record.get("sources") if isinstance(record, dict) else {}
        source_record = sources.get(source) if isinstance(sources, dict) else None
        if not isinstance(source_record, dict):
            return "new"
        if source_record.get("card_hash") == digest:
            return "unchanged"
        return "card_changed"

    def record_source_card(
        self,
        job_id: str,
        candidate_id: str,
        source: str,
        digest: str,
        *,
        screening_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not source:
            raise ValueError("candidate source is required")
        job = self._job(job_id)
        current_time = self.clock()
        candidate = job["candidates"].setdefault(
            candidate_id,
            {
                "first_seen_at": current_time,
                "evaluation_fingerprint": None,
                "evaluation_path": None,
            },
        )
        sources = candidate.setdefault("sources", {})
        source_record = sources.setdefault(source, {"first_seen_at": current_time})
        source_record["card_hash"] = digest
        source_record["last_seen_at"] = current_time
        if screening_status:
            source_record["screening_status"] = screening_status
        if metadata:
            source_record.update(deepcopy(metadata))
        candidate["card_hash"] = digest
        candidate["last_seen_at"] = current_time
        if screening_status:
            candidate["screening_status"] = screening_status

    def source_card_record(self, job_id: str, candidate_id: str, source: str) -> dict[str, Any] | None:
        candidate = self._job(job_id)["candidates"].get(candidate_id) or {}
        sources = candidate.get("sources") if isinstance(candidate, dict) else {}
        value = sources.get(source) if isinstance(sources, dict) else None
        return deepcopy(value) if isinstance(value, dict) else None

    def candidate_sources(self, job_id: str, candidate_id: str) -> list[str]:
        candidate = self._job(job_id)["candidates"].get(candidate_id) or {}
        sources = candidate.get("sources") if isinstance(candidate, dict) else {}
        return [str(source) for source in sources] if isinstance(sources, dict) else []

    def record_resume(self, candidate_id: str, digest: str, path: str) -> str:
        candidates = self.data["candidates"]
        current_time = self.clock()
        previous = candidates.get(candidate_id)
        if previous is None:
            candidates[candidate_id] = {
                "resume_hash": digest,
                "resume_path": path,
                "favorite_status": "unknown",
                "first_seen_at": current_time,
                "last_seen_at": current_time,
            }
            return "new"
        previous["last_seen_at"] = current_time
        previous["resume_path"] = path
        if previous.get("resume_hash") == digest:
            return "unchanged"
        previous["resume_hash"] = digest
        return "resume_changed"

    def resume_record(self, candidate_id: str) -> dict[str, Any] | None:
        value = self.data["candidates"].get(candidate_id)
        return deepcopy(value) if isinstance(value, dict) else None

    def evaluation_reusable(self, job_id: str, candidate_id: str, fingerprint: str) -> bool:
        record = self._job(job_id)["candidates"].get(candidate_id) or {}
        return bool(
            record.get("evaluation_fingerprint") == fingerprint
            and record.get("evaluation_path")
        )

    def record_evaluation(
        self,
        job_id: str,
        candidate_id: str,
        *,
        fingerprint: str,
        path: str,
    ) -> None:
        job = self._job(job_id)
        if candidate_id not in job["candidates"]:
            raise KeyError(f"candidate {candidate_id} has no card state for job {job_id}")
        record = job["candidates"][candidate_id]
        record["evaluation_fingerprint"] = fingerprint
        record["evaluation_path"] = path
        record["evaluated_at"] = self.clock()

    def set_favorite_status(self, candidate_id: str, status: str) -> None:
        record = self.data["candidates"].get(candidate_id)
        if not isinstance(record, dict):
            raise KeyError(f"candidate {candidate_id} has no global state")
        record["favorite_status"] = status
        record["favorite_checked_at"] = self.clock()

    def favorite_status(self, candidate_id: str) -> str:
        record = self.data["candidates"].get(candidate_id) or {}
        return str(record.get("favorite_status") or "unknown")

    def record_boss_stop(
        self,
        *,
        run_id: str,
        reason: str,
        checkpoint_path: str,
        request_count: int,
    ) -> None:
        if not run_id or not reason:
            raise ValueError("BOSS stop requires run_id and reason")
        boss_access = self.data.setdefault("boss_access", {"stops": {}, "last_stop_run_id": None})
        boss_access["stops"][run_id] = {
            "reason": reason,
            "checkpoint_path": checkpoint_path,
            "request_count": int(request_count),
            "stopped_at": self.clock(),
        }
        boss_access["last_stop_run_id"] = run_id

    def boss_stop(self, run_id: str) -> dict[str, Any] | None:
        boss_access = self.data.get("boss_access") or {}
        stops = boss_access.get("stops") if isinstance(boss_access, dict) else {}
        value = stops.get(run_id) if isinstance(stops, dict) else None
        return deepcopy(value) if isinstance(value, dict) else None

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.data["jobs"].get(job_id)
        if not isinstance(job, dict):
            raise KeyError(f"job {job_id} has not been recorded")
        return job
