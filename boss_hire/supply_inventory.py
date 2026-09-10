from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from boss_hire.local_security import atomic_write_json, ensure_private_file
from boss_hire.ranking_contract import ranking_key
from boss_hire.state_store import content_hash


SCHEMA_VERSION = 2


def _identifier(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": {},
        "evaluations": {},
        "delivered": {},
        "job_artifacts": {},
        "next_discovery_order": 1,
    }


def stable_local_candidate_id(boss_candidate_id: str) -> str:
    stable_boss_id = _identifier(boss_candidate_id, "boss_candidate_id")
    return "candidate-" + content_hash({"boss_candidate_id": stable_boss_id})[:16]


class CandidateInventory:
    """Serializable offline inventory with global resumes and job-scoped rankings."""

    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        self._state = deepcopy(dict(state)) if state is not None else _empty_state()
        self._migrate_state()
        self._validate_state()

    def _migrate_state(self) -> None:
        version = self._state.get("schema_version")
        if version == 1:
            legacy_displayed = self._state.pop("displayed", {})
            delivered: dict[str, dict[str, dict[str, str]]] = {}
            if isinstance(legacy_displayed, Mapping):
                for job_id, candidates in legacy_displayed.items():
                    if not isinstance(candidates, Mapping):
                        continue
                    delivered[str(job_id)] = {}
                    for candidate_id, receipt in candidates.items():
                        board_id = (
                            str(receipt.get("board_id") or "").strip()
                            if isinstance(receipt, Mapping)
                            else ""
                        )
                        delivered[str(job_id)][str(candidate_id)] = {
                            "batch_id": board_id or "legacy-displayed",
                            "migrated_from": "displayed",
                        }
            self._state["delivered"] = delivered
            self._state["schema_version"] = SCHEMA_VERSION

    @classmethod
    def load(cls, path: Path) -> "CandidateInventory":
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取候选人库存：{target}") from exc
        if not isinstance(state, Mapping):
            raise ValueError("候选人库存必须是 JSON 对象")
        ensure_private_file(target)
        return cls(state)

    def save(self, path: Path) -> None:
        atomic_write_json(Path(path), self._state, sort_keys=True)

    def _validate_state(self) -> None:
        if self._state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("库存 schema_version 无效")
        self._state.setdefault("job_artifacts", {})
        for field in ("candidates", "evaluations", "delivered", "job_artifacts"):
            if not isinstance(self._state.get(field), dict):
                raise ValueError(f"库存 {field} 必须是对象")
        next_discovery_order = 1
        for candidate_id in sorted(self._state["candidates"]):
            candidate = self._state["candidates"][candidate_id]
            if not isinstance(candidate, dict):
                raise ValueError(f"库存候选人 {candidate_id} 必须是对象")
            discovery_order = candidate.get("discovery_order")
            if not isinstance(discovery_order, int) or isinstance(discovery_order, bool) or discovery_order <= 0:
                discovery_order = next_discovery_order
                candidate["discovery_order"] = discovery_order
            next_discovery_order = max(next_discovery_order, discovery_order + 1)
            sources = candidate.setdefault("sources", {})
            if not isinstance(sources, dict):
                raise ValueError(f"库存候选人 {candidate_id} sources 必须是对象")
            candidate.setdefault("card_status", "ready" if sources else "missing")
            candidate.setdefault(
                "resume_status",
                "ready" if candidate.get("resume") is not None else "pending" if sources else "missing",
            )
            if candidate["card_status"] not in {"missing", "ready"}:
                raise ValueError(f"库存候选人 {candidate_id} card_status 无效")
            if candidate["resume_status"] not in {"missing", "pending", "ready"}:
                raise ValueError(f"库存候选人 {candidate_id} resume_status 无效")
        stored_next_order = self._state.get("next_discovery_order")
        if not isinstance(stored_next_order, int) or isinstance(stored_next_order, bool):
            stored_next_order = 1
        self._state["next_discovery_order"] = max(stored_next_order, next_discovery_order)

    def _candidate(self, candidate_id: str) -> dict[str, Any]:
        candidates = self._state["candidates"]
        candidate = candidates.get(candidate_id)
        if candidate is None:
            discovery_order = self._state["next_discovery_order"]
            self._state["next_discovery_order"] = discovery_order + 1
            candidate = {
                "candidate_id": candidate_id,
                "resume": None,
                "sources": {},
                "card_status": "missing",
                "resume_status": "missing",
                "discovery_order": discovery_order,
            }
            candidates[candidate_id] = candidate
        return candidate

    def ensure_resume(
        self,
        candidate_id: str,
        loader: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        stable_id = _identifier(candidate_id, "candidate_id")
        candidate = self._candidate(stable_id)
        if candidate.get("resume") is None:
            resume = loader()
            if not isinstance(resume, Mapping):
                raise ValueError("resume loader 必须返回对象")
            candidate["resume"] = deepcopy(dict(resume))
            candidate["resume_status"] = "ready"
        return deepcopy(candidate["resume"])

    def get_resume(self, candidate_id: str) -> dict[str, Any] | None:
        stable_id = _identifier(candidate_id, "candidate_id")
        candidate = self._state["candidates"].get(stable_id)
        if not isinstance(candidate, Mapping) or candidate.get("resume") is None:
            return None
        return deepcopy(candidate["resume"])

    def candidate_status(self, candidate_id: str) -> dict[str, str]:
        stable_id = _identifier(candidate_id, "candidate_id")
        candidate = self._state["candidates"].get(stable_id)
        if not isinstance(candidate, Mapping):
            raise ValueError("候选人尚未进入库存")
        return {
            "card": str(candidate.get("card_status") or "missing"),
            "resume": str(candidate.get("resume_status") or "missing"),
        }

    def list_resume_pending(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        stable_job_id = _identifier(job_id, "job_id") if job_id is not None else None
        result: list[dict[str, Any]] = []
        ordered_candidates = sorted(
            self._state["candidates"].items(),
            key=lambda item: (item[1].get("discovery_order", 0), item[0]),
        )
        for candidate_id, candidate in ordered_candidates:
            if candidate.get("resume_status") != "pending":
                continue
            sources = candidate.get("sources") or {}
            if stable_job_id is not None:
                belongs_to_job = any(
                    str(card.get("encryptJobId") or card.get("encrypt_job_id") or "").strip()
                    == stable_job_id
                    for cards in sources.values()
                    if isinstance(cards, list)
                    for card in cards
                    if isinstance(card, Mapping)
                )
                if not belongs_to_job:
                    continue
            result.append(
                {
                    "candidate_id": candidate_id,
                    "status": {"card": "ready", "resume": "pending"},
                    "sources": deepcopy(sources),
                }
            )
        return result

    def select_resume_pending(
        self,
        *,
        job_id: str,
        selection: str | int,
    ) -> dict[str, Any]:
        stable_job_id = _identifier(job_id, "job_id")
        if selection == "all":
            limit: int | None = None
            selection_label = "all"
        else:
            try:
                limit = int(selection)
            except (TypeError, ValueError) as exc:
                raise ValueError("详情选择必须是 all 或正整数") from exc
            if isinstance(selection, bool) or limit <= 0 or str(selection).strip() != str(limit):
                raise ValueError("详情选择必须是 all 或正整数")
            selection_label = str(limit)
        pending = self.list_resume_pending(job_id=stable_job_id)
        selected = pending if limit is None else pending[:limit]
        candidates: list[dict[str, Any]] = []
        for row in selected:
            identities: set[tuple[str, str, str]] = set()
            for cards in row["sources"].values():
                if not isinstance(cards, list):
                    continue
                for card in cards:
                    if not isinstance(card, Mapping):
                        continue
                    encrypt_geek_id = str(card.get("encryptGeekId") or "").strip()
                    encrypt_job_id = str(card.get("encryptJobId") or "").strip()
                    security_id = str(card.get("securityId") or "").strip()
                    if encrypt_geek_id and encrypt_job_id == stable_job_id:
                        identities.add((encrypt_geek_id, encrypt_job_id, security_id))
            if len(identities) != 1:
                raise ValueError(f"候选人在线详情标识不唯一：{row['candidate_id']}")
            encrypt_geek_id, encrypt_job_id, security_id = next(iter(identities))
            candidate = {
                "candidate_id": row["candidate_id"],
                "encrypt_geek_id": encrypt_geek_id,
                "encrypt_job_id": encrypt_job_id,
            }
            if security_id:
                candidate["security_id"] = security_id
            candidates.append(candidate)
        return {
            "job_id": stable_job_id,
            "selection": selection_label,
            "pending_count": len(pending),
            "selected_count": len(candidates),
            "remaining_pending_count": len(pending) - len(candidates),
            "candidates": candidates,
        }

    def get_evaluation(
        self,
        job_id: str,
        candidate_id: str,
        *,
        rubric_version: str,
    ) -> dict[str, Any] | None:
        stable_job_id = _identifier(job_id, "job_id")
        stable_candidate_id = _identifier(candidate_id, "candidate_id")
        stable_rubric_version = _identifier(rubric_version, "rubric_version")
        evaluation = self._state["evaluations"].get(stable_job_id, {}).get(stable_candidate_id)
        if not isinstance(evaluation, Mapping):
            return None
        if evaluation.get("rubric_version") != stable_rubric_version:
            return None
        return deepcopy(dict(evaluation))

    def list_score_pending(
        self,
        *,
        job_id: str,
        rubric_version: str,
    ) -> list[dict[str, Any]]:
        stable_job_id = _identifier(job_id, "job_id")
        stable_rubric_version = _identifier(rubric_version, "rubric_version")
        ordered_candidates = sorted(
            self._state["candidates"].items(),
            key=lambda item: (item[1].get("discovery_order", 0), item[0]),
        )
        result: list[dict[str, Any]] = []
        for candidate_id, candidate in ordered_candidates:
            if candidate.get("resume_status") != "ready":
                continue
            if self.get_evaluation(
                stable_job_id,
                candidate_id,
                rubric_version=stable_rubric_version,
            ) is not None:
                continue
            result.append(
                {
                    "candidate_id": candidate_id,
                    "resume": deepcopy(candidate.get("resume")),
                    "sources": deepcopy(candidate.get("sources") or {}),
                }
            )
        return result

    def get_job_artifacts(self, job_id: str, source_jd_hash: str) -> dict[str, Any] | None:
        stable_job_id = _identifier(job_id, "job_id")
        stable_jd_hash = _identifier(source_jd_hash, "source_jd_hash")
        artifacts = self._state["job_artifacts"].get(stable_job_id)
        if not isinstance(artifacts, Mapping) or artifacts.get("source_jd_hash") != stable_jd_hash:
            return None
        return deepcopy(dict(artifacts))

    def record_job_artifacts(
        self,
        job_id: str,
        source_jd_hash: str,
        *,
        rubric: Mapping[str, Any],
        search_plan: Mapping[str, Any],
    ) -> None:
        stable_job_id = _identifier(job_id, "job_id")
        stable_jd_hash = _identifier(source_jd_hash, "source_jd_hash")
        if rubric.get("source_jd_hash") != stable_jd_hash:
            raise ValueError("rubric 与当前 JD 不一致")
        if search_plan.get("source_jd_hash") != stable_jd_hash:
            raise ValueError("search_plan 与当前 JD 不一致")
        self._state["job_artifacts"][stable_job_id] = {
            "source_jd_hash": stable_jd_hash,
            "rubric": deepcopy(dict(rubric)),
            "search_plan": deepcopy(dict(search_plan)),
        }

    def record_source_card(
        self,
        candidate_id: str,
        source: str,
        card: Mapping[str, Any],
    ) -> None:
        stable_id = _identifier(candidate_id, "candidate_id")
        source_name = _identifier(source, "source")
        if not isinstance(card, Mapping):
            raise ValueError("source card 必须是对象")
        candidate = self._candidate(stable_id)
        cards = candidate["sources"].setdefault(source_name, [])
        normalized = deepcopy(dict(card))
        if normalized not in cards:
            cards.append(normalized)
        candidate["card_status"] = "ready"
        if candidate.get("resume") is None:
            candidate["resume_status"] = "pending"

    def record_evaluation(
        self,
        job_id: str,
        candidate_id: str,
        evaluation: Mapping[str, Any],
    ) -> None:
        stable_job_id = _identifier(job_id, "job_id")
        stable_candidate_id = _identifier(candidate_id, "candidate_id")
        if stable_candidate_id not in self._state["candidates"]:
            raise ValueError("候选人详情尚未进入全局库存")
        if not isinstance(evaluation, Mapping):
            raise ValueError("evaluation 必须是对象")
        normalized = deepcopy(dict(evaluation))
        if normalized.get("contract") != "continuous_ranking":
            raise ValueError("evaluation 必须使用 continuous_ranking 契约")
        if _identifier(normalized.get("candidate_id"), "evaluation.candidate_id") != stable_candidate_id:
            raise ValueError("evaluation candidate_id 不一致")
        self._state["evaluations"].setdefault(stable_job_id, {})[stable_candidate_id] = normalized

    def select_undelivered(
        self,
        job_id: str,
        limit: int,
        *,
        rubric_version: str | None = None,
    ) -> dict[str, Any]:
        stable_job_id = _identifier(job_id, "job_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        evaluations = self._state["evaluations"].get(stable_job_id, {})
        delivered = self._state["delivered"].get(stable_job_id, {})
        ranked_ids = sorted(
            (
                candidate_id
                for candidate_id, evaluation in evaluations.items()
                if candidate_id not in delivered
                and (rubric_version is None or evaluation.get("rubric_version") == rubric_version)
            ),
            key=lambda candidate_id: ranking_key(evaluations[candidate_id], candidate_id),
        )
        selected_ids = ranked_ids[:limit]
        candidates = []
        for candidate_id in selected_ids:
            candidate = self._state["candidates"][candidate_id]
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "evaluation": deepcopy(evaluations[candidate_id]),
                    "resume": deepcopy(candidate.get("resume")),
                    "sources": deepcopy(candidate.get("sources") or {}),
                }
            )
        shortage = limit - len(candidates)
        return {
            "job_id": stable_job_id,
            "requested_count": limit,
            "actual_count": len(candidates),
            "shortage_count": shortage,
            "shortage_reason": "inventory_exhausted" if shortage else None,
            "candidates": candidates,
        }

    def select_evaluated(
        self,
        job_id: str,
        limit: int,
        *,
        rubric_version: str | None = None,
    ) -> dict[str, Any]:
        """Rank all evaluated inventory without consuming delivery history."""

        stable_job_id = _identifier(job_id, "job_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        evaluations = self._state["evaluations"].get(stable_job_id, {})
        ranked_ids = sorted(
            (
                candidate_id
                for candidate_id, evaluation in evaluations.items()
                if rubric_version is None
                or evaluation.get("rubric_version") == rubric_version
            ),
            key=lambda candidate_id: ranking_key(evaluations[candidate_id], candidate_id),
        )
        candidates = []
        for candidate_id in ranked_ids[:limit]:
            candidate = self._state["candidates"][candidate_id]
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "evaluation": deepcopy(evaluations[candidate_id]),
                    "resume": deepcopy(candidate.get("resume")),
                    "sources": deepcopy(candidate.get("sources") or {}),
                }
            )
        shortage = limit - len(candidates)
        return {
            "job_id": stable_job_id,
            "requested_count": limit,
            "actual_count": len(candidates),
            "shortage_count": shortage,
            "shortage_reason": "inventory_exhausted" if shortage else None,
            "candidates": candidates,
        }

    def evaluated_count(self, job_id: str, *, rubric_version: str | None = None) -> int:
        stable_job_id = _identifier(job_id, "job_id")
        evaluations = self._state["evaluations"].get(stable_job_id, {})
        return sum(
            1
            for evaluation in evaluations.values()
            if rubric_version is None
            or evaluation.get("rubric_version") == rubric_version
        )

    def undelivered_count(self, job_id: str, *, rubric_version: str | None = None) -> int:
        stable_job_id = _identifier(job_id, "job_id")
        evaluations = self._state["evaluations"].get(stable_job_id, {})
        delivered = self._state["delivered"].get(stable_job_id, {})
        return sum(
            1
            for candidate_id, evaluation in evaluations.items()
            if candidate_id not in delivered
            and (rubric_version is None or evaluation.get("rubric_version") == rubric_version)
        )

    def mark_delivered(
        self,
        batch_id: str,
        job_id: str,
        candidate_ids: list[str],
    ) -> None:
        stable_batch_id = _identifier(batch_id, "batch_id")
        stable_job_id = _identifier(job_id, "job_id")
        stable_candidate_ids = [_identifier(value, "candidate_id") for value in candidate_ids]
        if len(set(stable_candidate_ids)) != len(stable_candidate_ids):
            raise ValueError("candidate_ids 不能重复")
        evaluations = self._state["evaluations"].get(stable_job_id, {})
        history = self._state["delivered"].setdefault(stable_job_id, {})
        for candidate_id in stable_candidate_ids:
            if candidate_id not in evaluations:
                raise ValueError("只能标记已完成岗位评价的候选人")
            if candidate_id in history:
                receipt = history[candidate_id]
                if isinstance(receipt, Mapping) and receipt.get("batch_id") == stable_batch_id:
                    continue
                raise ValueError(f"候选人×岗位重复交付：{candidate_id}")
        for candidate_id in stable_candidate_ids:
            history.setdefault(candidate_id, {"batch_id": stable_batch_id})

    def select_unshown(
        self,
        job_id: str,
        limit: int,
        *,
        rubric_version: str | None = None,
    ) -> dict[str, Any]:
        return self.select_undelivered(job_id, limit, rubric_version=rubric_version)

    def unshown_count(self, job_id: str, *, rubric_version: str | None = None) -> int:
        return self.undelivered_count(job_id, rubric_version=rubric_version)

    def mark_displayed(self, board_id: str, job_id: str, candidate_ids: list[str]) -> None:
        self.mark_delivered(board_id, job_id, candidate_ids)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._state)
