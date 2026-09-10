from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from boss_hire.local_security import atomic_write_json, atomic_write_text

from boss_hire.state_store import StateStore, card_hash, content_hash, now_iso, resume_hash, stable_candidate_id


DEGREE_RANK = {
    "初中及以下": 0,
    "高中": 1,
    "中专": 1,
    "大专": 2,
    "专科": 2,
    "本科": 3,
    "硕士": 4,
    "研究生": 4,
    "博士": 5,
}
DEGREE_ALIASES = {
    "bachelor": "本科",
    "master": "硕士",
    "doctor": "博士",
    "college": "大专",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _nested_name(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("content") or value.get("newValue"))
    return _text(value)


def _work_value(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("value") or value.get("name"))
    return _text(value)


def parse_years_lower_bound(value: Any) -> float | None:
    text = _text(value)
    if not text or any(token in text for token in ("不限", "未知", "应届")):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]\s*\d+(?:\.\d+)?\s*年", text)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:年|年以上)", text)
    if match:
        return float(match.group(1))
    return None


def normalize_degree(value: Any) -> str:
    text = _text(value)
    for degree in ("博士", "硕士", "研究生", "本科", "大专", "专科", "高中", "中专", "初中及以下"):
        if degree in text:
            return degree
    return ""


@dataclass(frozen=True)
class SearchCandidateCard:
    encrypt_geek_id: str
    security_id: str
    encrypt_job_id: str
    name: str
    work_year: str
    degree: str
    city: str
    current_position: str
    expect_position: str
    advantage: str
    works: tuple[str, ...]
    work_details: tuple[dict[str, Any], ...]
    education: tuple[dict[str, Any], ...]
    matches: tuple[str, ...]
    viewed: bool
    old_detailed: bool
    source_page: int
    source_rank: int

    def hash_payload(self) -> dict[str, Any]:
        return {
            "encryptGeekId": self.encrypt_geek_id,
            "workYear": self.work_year,
            "degree": self.degree,
            "city": self.city,
            "currentPosition": self.current_position,
            "expectPosition": self.expect_position,
            "advantage": self.advantage,
            "works": list(self.works),
            "workDetails": list(self.work_details),
            "education": list(self.education),
            "matches": list(self.matches),
        }

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["works"] = list(self.works)
        value["work_details"] = list(self.work_details)
        value["education"] = list(self.education)
        value["matches"] = list(self.matches)
        return value


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        text = _nested_name(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def normalize_search_card(item: dict[str, Any], *, page: int, rank: int, job_id: str) -> SearchCandidateCard:
    card = item.get("geekCard") if isinstance(item.get("geekCard"), dict) else {}
    last_work = item.get("geekLastWork") if isinstance(item.get("geekLastWork"), dict) else {}
    candidate_id = stable_candidate_id(item)
    work_year = _text(card.get("workYear") or card.get("geekWorkYear") or item.get("workYear"))
    degree = normalize_degree(
        card.get("highestDegreeName")
        or card.get("degreeName")
        or card.get("geekDegree")
        or item.get("highestDegreeName")
        or item.get("degreeName")
        or card.get("workEduDesc")
    )
    works: list[str] = []
    work_details: list[dict[str, Any]] = []
    source_works = item.get("works") or item.get("showWorks") or card.get("works") or card.get("geekWorks") or []
    for work in source_works:
        if not isinstance(work, dict):
            continue
        company = _work_value(work.get("company") or work.get("companyName"))
        position = _work_value(
            work.get("positionName")
            or (work.get("position") if isinstance(work.get("position"), (str, dict)) else "")
        )
        line = " · ".join(value for value in (company, position) if value)
        if line:
            works.append(line)
        keywords = _string_list(work.get("workEmphasisList"))
        work_details.append(
            {
                "company": company,
                "department": _text(work.get("department")),
                "duration": _text(work.get("workTime")),
                "position": position,
                "responsibility": _text(work.get("responsibility")),
                "performance": _text(work.get("workPerformance")),
                "keywords": list(keywords),
            }
        )
    source_education = item.get("showEdus") or card.get("geekEdus") or card.get("education") or []
    education = tuple(
        {
            "school": _text(row.get("school")),
            "degree": normalize_degree(row.get("degreeName") or row.get("degree")),
            "major": _text(row.get("major")),
            "duration": _text(row.get("timeSlot")),
        }
        for row in source_education
        if isinstance(row, dict)
    )
    matches = _string_list(card.get("matches") or card.get("hlmatches") or item.get("recLabels"))
    return SearchCandidateCard(
        encrypt_geek_id=candidate_id,
        security_id=_text(card.get("securityId") or item.get("securityId")),
        encrypt_job_id=_text(card.get("encryptJobId") or item.get("jobId") or job_id),
        name=_text(card.get("name") or card.get("geekName") or item.get("name")),
        work_year=work_year,
        degree=degree,
        city=_text(card.get("city") or card.get("expectLocationName")),
        current_position=_nested_name(card.get("current")) or " · ".join(
            value
            for value in (
                _work_value(last_work.get("company")),
                _work_value(last_work.get("position") or last_work.get("positionName")),
            )
            if value
        ),
        expect_position=_nested_name(card.get("expect")) or _text(card.get("expectPositionName")),
        advantage=_nested_name(card.get("geekDesc")),
        works=tuple(works),
        work_details=tuple(work_details),
        education=education,
        matches=matches,
        viewed=bool(card.get("viewed")),
        old_detailed=bool(card.get("oldDetailed")),
        source_page=page,
        source_rank=rank,
    )


def build_card_evaluation_input(card: SearchCandidateCard) -> dict[str, Any]:
    return {
        "basic": {
            "name": card.name,
            "work_years": card.work_year,
            "degree": card.degree,
        },
        "expectation": {"position": card.expect_position},
        "work_experience": list(card.work_details),
        "project_experience": [],
        "education": list(card.education),
        "certifications": [],
        "matches": list(card.matches),
    }


def collect_search_cards(
    client: Any,
    *,
    query: str,
    job_id: str,
    max_pages: int = 7,
    limit: int = 100,
) -> tuple[list[SearchCandidateCard], dict[str, int]]:
    cards: list[SearchCandidateCard] = []
    seen: set[str] = set()
    stats = {"raw": 0, "duplicates": 0, "invalid": 0, "pages": 0}
    for page in range(1, max_pages + 1):
        response = client.search_geeks(query, page=page, job_id=job_id)
        if not isinstance(response, dict) or response.get("code") != 0:
            raise RuntimeError(f"search_geeks failed on page {page}: {response}")
        geeks = (response.get("zpData") or {}).get("geeks") or []
        if not isinstance(geeks, list):
            raise RuntimeError(f"search_geeks page {page} has invalid geeks: {geeks!r}")
        stats["pages"] += 1
        if not geeks:
            break
        for rank, item in enumerate(geeks, 1):
            stats["raw"] += 1
            if not isinstance(item, dict):
                stats["invalid"] += 1
                continue
            try:
                card = normalize_search_card(item, page=page, rank=rank, job_id=job_id)
            except ValueError:
                stats["invalid"] += 1
                continue
            if card.encrypt_geek_id in seen:
                stats["duplicates"] += 1
                continue
            seen.add(card.encrypt_geek_id)
            cards.append(card)
            if len(cards) >= limit:
                return cards, stats
    return cards, stats


def collect_recommendation_cards(
    fetch_page: Callable[[int], dict[str, Any]],
    *,
    job_id: str,
    max_pages: int = 7,
    limit: int = 100,
) -> tuple[list[SearchCandidateCard], dict[str, int]]:
    cards: list[SearchCandidateCard] = []
    seen: set[str] = set()
    stats = {"raw": 0, "duplicates": 0, "invalid": 0, "pages": 0}
    for page in range(1, max_pages + 1):
        response = fetch_page(page)
        if not isinstance(response, dict) or response.get("code") != 0:
            raise RuntimeError(f"recommendations failed on page {page}: {response}")
        data = response.get("zpData")
        geeks = data.get("geekList") or [] if isinstance(data, dict) else None
        if not isinstance(geeks, list):
            raise RuntimeError(f"recommendations page {page} has invalid geekList: {geeks!r}")
        stats["pages"] += 1
        if not geeks:
            break
        for rank, item in enumerate(geeks, 1):
            stats["raw"] += 1
            if not isinstance(item, dict):
                stats["invalid"] += 1
                continue
            try:
                card = normalize_search_card(item, page=page, rank=rank, job_id=job_id)
            except ValueError:
                stats["invalid"] += 1
                continue
            if card.encrypt_geek_id in seen:
                stats["duplicates"] += 1
                continue
            seen.add(card.encrypt_geek_id)
            cards.append(card)
            if len(cards) >= limit:
                return cards, stats
        if not data.get("hasMore"):
            break
    return cards, stats


def _fast_filters(rubric: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row["fast_filter"]
        for row in rubric.get("hard_gates") or []
        if isinstance(row, dict) and isinstance(row.get("fast_filter"), dict)
    ]


def apply_fast_filters(
    *,
    work_year: Any,
    degree: Any,
    rubric: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    any_unknown = False
    for rule in _fast_filters(rubric):
        rule_type = rule.get("type")
        if rule_type == "min_years":
            actual = parse_years_lower_bound(work_year)
            required = float(rule["value"])
            if actual is None:
                status = "uncertain"
                any_unknown = True
            else:
                status = "pass" if actual >= required else "fail"
            checks.append({"type": rule_type, "required": required, "actual": actual, "status": status})
        elif rule_type == "min_degree":
            actual_degree = normalize_degree(degree)
            required_degree = DEGREE_ALIASES.get(_text(rule["value"]).lower(), normalize_degree(rule["value"]))
            if not actual_degree or not required_degree:
                status = "uncertain"
                any_unknown = True
            else:
                status = "pass" if DEGREE_RANK[actual_degree] >= DEGREE_RANK[required_degree] else "fail"
            checks.append(
                {
                    "type": rule_type,
                    "required": required_degree,
                    "actual": actual_degree or None,
                    "status": status,
                }
            )
        else:
            raise ValueError(f"unsupported fast filter: {rule}")
    if any(check["status"] == "fail" for check in checks):
        status = "fail"
    elif any_unknown:
        status = "uncertain"
    else:
        status = "pass"
    return {"status": status, "checks": checks}


def _resume_filter_values(resume: dict[str, Any]) -> tuple[Any, Any]:
    basic = resume.get("basic") if isinstance(resume.get("basic"), dict) else {}
    return basic.get("work_years"), basic.get("degree")


def _atomic_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def prepare_card_candidates(
    *,
    job_id: str,
    rubric: dict[str, Any],
    state: StateStore,
    output_dir: Path,
    cards: list[SearchCandidateCard],
    source_stats: dict[str, int],
    excluded_candidate_ids: set[str] | None = None,
    pre_evaluated_candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    excluded = excluded_candidate_ids or set()
    pre_evaluated = pre_evaluated_candidate_ids or set()
    counts = {
        "new": 0,
        "card_changed": 0,
        "reused": 0,
        "excluded_search_history": 0,
        "full_evaluation_reused": 0,
        "card_rejected": 0,
        "eligible": 0,
    }
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []

    for card in cards:
        digest = card_hash(card.hash_payload())
        card_change = state.classify_card(job_id, card.encrypt_geek_id, digest)
        counts[card_change if card_change != "unchanged" else "reused"] += 1
        card_dir = output_dir / "cards" / card.encrypt_geek_id
        snapshot_path = card_dir / "card.json"
        _atomic_json(
            snapshot_path,
            {
                "source": "recommendation",
                "captured_at": now_iso(),
                "card_hash": digest,
                "card": card.to_dict(),
            },
        )

        card_filter = apply_fast_filters(work_year=card.work_year, degree=card.degree, rubric=rubric)
        if card.encrypt_geek_id in excluded:
            status = "excluded_search_history"
            counts[status] += 1
        elif card.encrypt_geek_id in pre_evaluated:
            status = "full_evaluation_reused"
            counts[status] += 1
        elif card_filter["status"] == "fail":
            status = "card_rejected"
            counts[status] += 1
        else:
            status = "card_ready"
            counts["eligible"] += 1
            evaluation_input_path = card_dir / "evaluation_input.json"
            _atomic_json(evaluation_input_path, build_card_evaluation_input(card))
            entries.append(
                {
                    "experiment_id": "R" + content_hash(card.encrypt_geek_id)[:15],
                    "candidate_id": card.encrypt_geek_id,
                    "name": card.name,
                    "page": card.source_page,
                    "source_rank": card.source_rank,
                    "resume_json": str(evaluation_input_path.resolve()),
                }
            )
        state.record_card(job_id, card.encrypt_geek_id, digest, screening_status=status)
        rows.append(
            {
                "candidate": card.to_dict(),
                "card_hash": digest,
                "card_change": card_change,
                "status": status,
                "card_filter": card_filter,
                "snapshot_path": str(snapshot_path.resolve()),
            }
        )

    state.save()
    prepared = {
        "job_id": job_id,
        "source": source_stats,
        "unique_candidates": len(cards),
        "counts": counts,
        "candidates": rows,
        "evaluation_entries": entries,
    }
    _atomic_json(output_dir / "card_prepared.json", prepared)
    return prepared


def prepare_candidates(
    client: Any,
    *,
    job_id: str,
    query: str,
    rubric: dict[str, Any],
    state: StateStore,
    output_dir: Path,
    parse_resume: Callable[[dict[str, Any]], dict[str, Any]],
    max_pages: int = 7,
    limit: int = 100,
    card_source: Callable[[], tuple[list[SearchCandidateCard], dict[str, int]]] | None = None,
    excluded_candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    if card_source is None:
        cards, source_stats = collect_search_cards(
            client,
            query=query,
            job_id=job_id,
            max_pages=max_pages,
            limit=limit,
        )
    else:
        cards, source_stats = card_source()
    if excluded_candidate_ids is not None:
        discovered = len(cards)
        cards = [card for card in cards if card.encrypt_geek_id not in excluded_candidate_ids]
        source_stats = {**source_stats, "excluded": discovered - len(cards)}
    results: list[dict[str, Any]] = []
    counts = {
        "new": 0,
        "card_changed": 0,
        "reused": 0,
        "card_rejected": 0,
        "resume_rejected": 0,
        "ready": 0,
        "detail_fetched": 0,
        "resume_reused": 0,
        "errors": 0,
    }
    for card in cards:
        digest = card_hash(card.hash_payload())
        card_change = state.classify_card(job_id, card.encrypt_geek_id, digest)
        if card_change == "unchanged":
            counts["reused"] += 1
            results.append(
                {
                    "candidate": card.to_dict(),
                    "status": state.screening_status(job_id, card.encrypt_geek_id) or "reused",
                    "card_change": card_change,
                }
            )
            continue
        counts[card_change] += 1

        card_filter = apply_fast_filters(work_year=card.work_year, degree=card.degree, rubric=rubric)
        if card_filter["status"] == "fail":
            counts["card_rejected"] += 1
            state.record_card(
                job_id,
                card.encrypt_geek_id,
                digest,
                screening_status="card_rejected",
            )
            results.append(
                {
                    "candidate": card.to_dict(),
                    "status": "card_rejected",
                    "card_change": card_change,
                    "card_filter": card_filter,
                }
            )
            continue

        resume_record = state.resume_record(card.encrypt_geek_id)
        resume_path = output_dir / "resumes" / card.encrypt_geek_id / "resume.json"
        parsed: dict[str, Any] | None = None
        cached_path = resume_path
        if not cached_path.is_file() and resume_record:
            cached_path = Path(str(resume_record.get("resume_path") or ""))
        if card_change == "new" and cached_path.is_file():
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
                parsed = cached if isinstance(cached, dict) else None
            except (OSError, json.JSONDecodeError):
                parsed = None
            if parsed is not None:
                counts["resume_reused"] += 1
        if parsed is None and not card.security_id:
            counts["errors"] += 1
            results.append(
                {
                    "candidate": card.to_dict(),
                    "status": "error",
                    "card_change": card_change,
                    "error": "missing_security_id_for_view_geek",
                }
            )
            continue
        elif parsed is None:
            raw = client.view_geek(card.encrypt_geek_id, job_id, security_id=card.security_id)
            if not isinstance(raw, dict) or raw.get("code") != 0:
                counts["errors"] += 1
                results.append(
                    {
                        "candidate": card.to_dict(),
                        "status": "error",
                        "card_change": card_change,
                        "error": "view_geek_failed",
                    }
                )
                continue
            payload = raw.get("zpData")
            if isinstance(payload, dict) and "geekDetailInfo" in payload and payload.get("geekDetailInfo") is None:
                counts["errors"] += 1
                state.record_card(
                    job_id,
                    card.encrypt_geek_id,
                    digest,
                    screening_status="detail_unavailable",
                )
                results.append(
                    {
                        "candidate": card.to_dict(),
                        "status": "detail_unavailable",
                        "card_change": card_change,
                        "error": "missing_geek_detail_info",
                    }
                )
                continue
            try:
                parsed = parse_resume(raw)
                if not isinstance(parsed, dict):
                    raise TypeError("parse_resume must return a dict")
            except Exception as exc:  # noqa: BLE001 - isolate one malformed BOSS resume
                counts["errors"] += 1
                results.append(
                    {
                        "candidate": card.to_dict(),
                        "status": "error",
                        "card_change": card_change,
                        "error": "parse_resume_failed",
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            _atomic_json(resume_path, parsed)
            counts["detail_fetched"] += 1

        parsed_digest = resume_hash(parsed)
        state.record_resume(card.encrypt_geek_id, parsed_digest, str(resume_path.resolve()))
        resume_years, resume_degree = _resume_filter_values(parsed)
        resume_filter = apply_fast_filters(work_year=resume_years, degree=resume_degree, rubric=rubric)
        if resume_filter["status"] == "fail":
            counts["resume_rejected"] += 1
            status = "resume_rejected"
        else:
            counts["ready"] += 1
            status = "ready"
        state.record_card(
            job_id,
            card.encrypt_geek_id,
            digest,
            screening_status=status,
        )
        results.append(
            {
                "candidate": card.to_dict(),
                "status": status,
                "card_change": card_change,
                "card_filter": card_filter,
                "resume_filter": resume_filter,
                "resume_hash": parsed_digest,
                "resume_path": str(resume_path.resolve()),
            }
        )
    state.save()
    return {
        "job_id": job_id,
        "query": query,
        "limit": limit,
        "source": source_stats,
        "unique_candidates": len(cards),
        "counts": counts,
        "candidates": results,
    }


def build_evaluation_entries(
    prepared: dict[str, Any],
    state: StateStore,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    entries: list[dict[str, Any]] = []
    stats = {"eligible": 0, "excluded": 0, "missing_resume": 0}
    for row in prepared.get("candidates") or []:
        if not isinstance(row, dict) or row.get("status") != "ready":
            stats["excluded"] += 1
            continue
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
        candidate_id = _text(candidate.get("encrypt_geek_id"))
        resume = state.resume_record(candidate_id) if candidate_id else None
        resume_path = Path(str((resume or {}).get("resume_path") or ""))
        if not candidate_id or not resume_path.is_file():
            stats["missing_resume"] += 1
            continue
        entries.append(
            {
                "experiment_id": "M" + content_hash(candidate_id)[:15],
                "candidate_id": candidate_id,
                "name": _text(candidate.get("name")),
                "page": candidate.get("source_page"),
                "source_rank": candidate.get("source_rank"),
                "resume_json": str(resume_path.resolve()),
            }
        )
        stats["eligible"] += 1
    return entries, stats


def _priority_evaluation(row: dict[str, Any]) -> bool:
    evaluation = row.get("evaluation") or {}
    return bool(
        evaluation.get("hard_gate") == "pass"
        and evaluation.get("decision") == "优先跟进"
        and float(evaluation.get("total_score") or 0) >= 80
    )


def create_confirmation_batch(
    *,
    job_id: str,
    job_name: str,
    query: str | None = None,
    prepared: dict[str, Any],
    evaluations: list[dict[str, Any]],
    state: StateStore,
    created_at: str | None = None,
) -> dict[str, Any]:
    cards = {
        _text((row.get("candidate") or {}).get("encrypt_geek_id")): row.get("candidate") or {}
        for row in prepared.get("candidates") or []
        if isinstance(row, dict) and isinstance(row.get("candidate"), dict)
    }
    selected: list[dict[str, Any]] = []
    stats = {
        "evaluated": len(evaluations),
        "not_priority": 0,
        "already_favorited": 0,
        "favorite_unknown": 0,
        "missing_mapping": 0,
        "selected": 0,
    }
    ranked = sorted(
        evaluations,
        key=lambda row: (-float((row.get("evaluation") or {}).get("total_score") or 0), _text(row.get("candidate_id"))),
    )
    for row in ranked:
        if not _priority_evaluation(row):
            stats["not_priority"] += 1
            continue
        candidate_id = _text(row.get("candidate_id"))
        favorite_status = state.favorite_status(candidate_id)
        if favorite_status == "favorited":
            stats["already_favorited"] += 1
            continue
        if favorite_status == "favorite_unknown":
            stats["favorite_unknown"] += 1
            continue
        card = cards.get(candidate_id) or {}
        security_id = _text(card.get("security_id"))
        if not candidate_id or not security_id:
            stats["missing_mapping"] += 1
            continue
        evaluation = row["evaluation"]
        missing = evaluation.get("missing_requirements") or []
        selected.append(
            {
                "experiment_id": _text(row.get("experiment_id")),
                "encrypt_geek_id": candidate_id,
                "security_id": security_id,
                "name": _text(card.get("name")),
                "score": evaluation["total_score"],
                "summary": _text(evaluation.get("summary")),
                "key_gap": _text(missing[0]) if missing else "暂无明确缺口",
                "source_page": card.get("source_page"),
                "source_rank": card.get("source_rank"),
                "evaluation_fingerprint": _text(row.get("input_fingerprint")),
            }
        )
    stats["selected"] = len(selected)
    batch_id = "batch-" + content_hash(
        {
            "job_id": job_id,
            "candidates": [
                [row["encrypt_geek_id"], row["evaluation_fingerprint"]]
                for row in selected
            ],
        }
    )[:12]
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": "awaiting_authorization",
        "created_at": created_at or now_iso(),
        "job_id": job_id,
        "job_name": job_name,
        "query": query or job_name,
        "candidate_count": len(selected),
        "write_contract": {"endpoint": "userMark/add", "mark_type": 6},
        "candidates": selected,
        "stats": stats,
    }


def render_confirmation_batch(batch: dict[str, Any]) -> str:
    def cell(value: Any) -> str:
        return _text(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# 待确认收藏批次",
        "",
        f"- 岗位：{cell(batch.get('job_name'))}",
        f"- 批次：{cell(batch.get('batch_id'))}",
        f"- 建议收藏：{int(batch.get('candidate_count') or 0)} 人",
        "- 当前状态：仅生成建议，尚未对 BOSS 执行任何写操作",
        "",
        "| 候选人 | 分数 | 一句话理由 | 关键缺口 |",
        "|---|---:|---|---|",
    ]
    for row in batch.get("candidates") or []:
        lines.append(
            f"| {cell(row.get('name'))} | {row.get('score')} | {cell(row.get('summary'))} | {cell(row.get('key_gap'))} |"
        )
    if not batch.get("candidates"):
        lines.append("| — | — | 当前没有达到收藏标准且可执行的候选人 | — |")
    lines.extend(["", "详细证据保留在本地评分结果中，仅在需要复核单人时展开。", ""])
    return "\n".join(lines)


def write_confirmation_batch(batch: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    json_path = output_dir / "batch.json"
    markdown_path = output_dir / "batch.md"
    _atomic_json(json_path, batch)
    atomic_write_text(markdown_path, render_confirmation_batch(batch))
    return {"json": json_path, "markdown": markdown_path}


def deliver_confirmation_batch(
    batch: dict[str, Any],
    *,
    authorized_batch_id: str,
    state: StateStore,
    favorite: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    batch_id = _text(batch.get("batch_id"))
    if not batch_id or authorized_batch_id != batch_id:
        raise PermissionError("authorized batch id does not match batch.json")
    if batch.get("status") != "awaiting_authorization":
        raise RuntimeError(f"batch is not deliverable: {batch.get('status')}")
    results: list[dict[str, Any]] = []
    stopped = False
    for candidate in batch.get("candidates") or []:
        candidate_id = _text(candidate.get("encrypt_geek_id"))
        if not candidate_id:
            results.append({"status": "preflight_failed", "reason": "missing_candidate_id"})
            continue
        if state.favorite_status(candidate_id) == "favorited":
            results.append({"candidate_id": candidate_id, "status": "favorited", "changed": False})
            continue
        result = favorite(candidate)
        status = _text(result.get("status"))
        if status in {"favorited", "favorite_unknown", "favorite_failed"}:
            state.set_favorite_status(candidate_id, status)
            state.save()
        results.append({"candidate_id": candidate_id, **result})
        if status == "favorite_unknown" or result.get("reason") == "unexpected_contact_side_effect":
            stopped = True
            break
    return {
        "batch_id": batch_id,
        "attempted": sum(bool(row.get("write_attempted")) for row in results),
        "favorited": sum(row.get("status") == "favorited" for row in results),
        "failed": sum(row.get("status") in {"favorite_failed", "preflight_failed"} for row in results),
        "unknown": sum(row.get("status") == "favorite_unknown" for row in results),
        "stopped": stopped,
        "results": results,
    }
