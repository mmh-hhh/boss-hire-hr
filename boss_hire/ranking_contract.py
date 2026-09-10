from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from boss_hire.state_store import jd_hash


LEVEL_FACTORS = {
    "none": 0.0,
    "weak": 0.25,
    "partial": 0.5,
    "strong": 0.75,
    "exceptional": 1.0,
}
PROGRAM_FIELDS = {
    "total_score",
    "evidence_coverage",
    "dimension_scores",
    "decision",
    "hard_gate",
    "hard_gate_results",
}
EVIDENCE_RULES = [
    "所有正向判断必须引用候选人材料中的连续原文事实",
    "缺少证据时使用 none，并把待确认项写入 gaps",
    "JD 中的‘必须’和‘任职要求’只影响维度权重与 critical 标记，不产生淘汰判断",
    "不得使用受保护属性或与 JD 无关的个人信息评分",
]
EXCLUDED_ATTRIBUTES = [
    "姓名",
    "性别",
    "年龄",
    "照片",
    "婚育状况",
    "民族",
    "联系方式",
    "学校层级",
    "与 JD 无关的居住信息",
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def _grounded(quote: Any, sources: Iterable[str]) -> bool:
    text = _text(quote)
    return bool(text) and any(text in source for source in sources)


UNGROUNDED_EVIDENCE_GAP = "正向证据无法逐字定位"


def finalize_continuous_rubric(core: dict[str, Any], jd_text: str) -> dict[str, Any]:
    """Validate and version an LLM-produced continuous ranking rubric."""
    if not isinstance(core, dict):
        raise ValueError("rubric 必须是 JSON 对象")
    if not _text(core.get("job_title")):
        raise ValueError("rubric 缺少 job_title")
    if "hard_gates" in core or "decision_rules" in core:
        raise ValueError("连续排序 rubric 不允许 hard_gates 或 decision_rules")

    dimensions = core.get("dimensions")
    if not isinstance(dimensions, list) or not 3 <= len(dimensions) <= 7:
        raise ValueError("dimensions 数量必须为 3-7")

    seen_ids: set[str] = set()
    total_weight = 0
    required_anchors = set(LEVEL_FACTORS)
    for row in dimensions:
        if not isinstance(row, dict):
            raise ValueError("评分维度必须是 JSON 对象")
        dimension_id = _text(row.get("id"))
        if not dimension_id or dimension_id in seen_ids:
            raise ValueError("评分维度 id 缺失或重复")
        seen_ids.add(dimension_id)
        if not _text(row.get("name")) or not _text(row.get("requirement")):
            raise ValueError(f"评分维度 {dimension_id} 缺少 name 或 requirement")
        if not isinstance(row.get("critical"), bool):
            raise ValueError(f"评分维度 {dimension_id} critical 必须为布尔值")
        weight = row.get("weight")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError(f"评分维度 {dimension_id} 权重无效")
        total_weight += weight

        jd_evidence = row.get("jd_evidence")
        jd_sources = list(_all_strings(jd_text))
        if (
            not isinstance(jd_evidence, list)
            or not jd_evidence
            or not all(_grounded(item, jd_sources) for item in jd_evidence)
        ):
            raise ValueError(f"评分维度 {dimension_id} 缺少可定位 JD 原文")

        anchors = row.get("anchors")
        if not isinstance(anchors, dict) or set(anchors) != required_anchors:
            raise ValueError(f"评分维度 {dimension_id} 必须包含五档 anchors")
        if not all(_text(anchors[level]) for level in LEVEL_FACTORS):
            raise ValueError(f"评分维度 {dimension_id} anchors 不能为空")

    if total_weight != 100:
        raise ValueError(f"评分维度权重合计必须为 100，实际为 {total_weight}")

    result = deepcopy(core)
    digest = jd_hash(jd_text)
    result.update(
        {
            "schema_version": 2,
            "contract": "continuous_ranking",
            "version": f"jd-{digest[:12]}-ranking-v2",
            "source_jd_hash": digest,
            "purpose": "依据 JD 与候选人原文证据生成连续分数和优先级，不作通过或淘汰判断。",
            "evidence_rules": list(EVIDENCE_RULES),
            "excluded_attributes": list(EXCLUDED_ATTRIBUTES),
        }
    )
    return result


def finalize_continuous_evaluation(
    llm_result: dict[str, Any],
    candidate: dict[str, Any],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Validate evidence and deterministically compute the candidate's score."""
    if not isinstance(llm_result, dict):
        raise ValueError("候选人评价必须是 JSON 对象")
    forbidden = sorted(PROGRAM_FIELDS.intersection(llm_result))
    if forbidden:
        raise ValueError("LLM 返回了程序计算字段：" + ", ".join(forbidden))
    if rubric.get("contract") != "continuous_ranking":
        raise ValueError("rubric 不是 continuous_ranking 契约")

    candidate_id = _text(candidate.get("candidate_id"))
    if not candidate_id:
        raise ValueError("candidate_id 不能为空")

    assessments = llm_result.get("dimension_assessments")
    if not isinstance(assessments, list):
        raise ValueError("缺少 dimension_assessments")
    by_id: dict[str, dict[str, Any]] = {}
    for row in assessments:
        if not isinstance(row, dict):
            raise ValueError("维度评价必须是 JSON 对象")
        dimension_id = _text(row.get("id"))
        if not dimension_id or dimension_id in by_id:
            raise ValueError("维度评价 id 缺失或重复")
        by_id[dimension_id] = row

    expected_ids = [_text(row.get("id")) for row in rubric.get("dimensions") or []]
    if set(by_id) != set(expected_ids):
        raise ValueError("维度评价必须与 rubric 完整对应")

    candidate_sources = list(_all_strings(candidate))
    dimension_scores: list[dict[str, Any]] = []
    total_score = 0.0
    covered_weight = 0
    for dimension in rubric["dimensions"]:
        dimension_id = dimension["id"]
        assessment = deepcopy(by_id[dimension_id])
        level = assessment.get("level")
        if level not in LEVEL_FACTORS:
            raise ValueError(f"维度 {dimension_id} level 无效")
        evidence = assessment.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"维度 {dimension_id} evidence 必须为列表")
        if level != "none":
            if not evidence or not all(_grounded(item, candidate_sources) for item in evidence):
                level = "none"
                evidence = []
                gaps = assessment.get("gaps") if isinstance(assessment.get("gaps"), list) else []
                if UNGROUNDED_EVIDENCE_GAP not in gaps:
                    gaps = [*gaps, UNGROUNDED_EVIDENCE_GAP]
                assessment["gaps"] = gaps
                assessment["reason"] = UNGROUNDED_EVIDENCE_GAP + "，已按无证据计分"
            else:
                covered_weight += dimension["weight"]
        elif evidence and not all(_grounded(item, candidate_sources) for item in evidence):
            evidence = []

        score = round(dimension["weight"] * LEVEL_FACTORS[level], 2)
        total_score += score
        dimension_scores.append(
            {
                "id": dimension_id,
                "name": dimension["name"],
                "weight": dimension["weight"],
                "critical": dimension["critical"],
                "level": level,
                "score": score,
                "evidence": deepcopy(evidence),
                "reason": _text(assessment.get("reason")),
                "gaps": deepcopy(assessment.get("gaps") or []),
            }
        )

    result = deepcopy(llm_result)
    result.pop("dimension_assessments", None)
    # Candidate identity comes from the persisted local scoring task, not a
    # generative response that can omit or mutate an opaque identifier.
    result.pop("candidate_id", None)
    if isinstance(result.get("evidence"), list):
        result["evidence"] = [
            item for item in result["evidence"] if _grounded(item, candidate_sources)
        ]
    result.update(
        {
            "schema_version": 2,
            "contract": "continuous_ranking",
            "candidate_id": candidate_id,
            "rubric_version": rubric.get("version"),
            "total_score": round(total_score, 2),
            "evidence_coverage": round(float(covered_weight), 2),
            "dimension_scores": dimension_scores,
        }
    )
    return result


def adapt_legacy_evaluation(legacy: dict[str, Any]) -> dict[str, Any]:
    """Convert a frozen gate-based evaluation into a sortable read-only record."""
    if not isinstance(legacy, dict):
        raise ValueError("legacy evaluation 必须是 JSON 对象")
    rows = legacy.get("dimension_scores") or []
    evidence_rows = [row for row in rows if isinstance(row, dict) and row.get("evidence")]
    coverage = len(evidence_rows) / len(rows) if rows else 0.0
    return {
        "schema_version": 2,
        "contract": "continuous_ranking",
        "source_contract": "legacy_gate_v1",
        "candidate_id": _text(legacy.get("candidate_id")),
        "total_score": float(legacy.get("total_score") or 0),
        "evidence_coverage": coverage,
        "dimension_scores": deepcopy(rows),
        "evidence": deepcopy(legacy.get("evidence") or []),
        "gaps": deepcopy(legacy.get("gaps") or legacy.get("missing_requirements") or []),
        "risks": deepcopy(legacy.get("risks") or []),
        "follow_up_questions": deepcopy(legacy.get("follow_up_questions") or []),
        "summary": _text(legacy.get("summary")),
    }


def ranking_key(evaluation: dict[str, Any], candidate_id: str) -> tuple[float, float, str]:
    """Stable ascending sort key: higher score, then coverage, then candidate id."""
    score = float(evaluation.get("total_score") or 0)
    coverage = float(evaluation.get("evidence_coverage") or 0)
    normalized_coverage = coverage / 100 if coverage > 1 else coverage
    return (-score, -normalized_coverage, str(candidate_id))
