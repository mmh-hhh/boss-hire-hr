from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from boss_hire.ranking_contract import (
    finalize_continuous_evaluation,
    finalize_continuous_rubric,
)
from boss_hire.local_security import ensure_private_directory, ensure_private_file
from boss_hire.recruiter_jobs import RecruiterJob
from boss_hire.search_plan import (
    ALLOWED_DOMINANT_AXES,
    DISALLOWED_QUERY_SYNTAX_RE,
    SearchPlanConfig,
    _is_generic_role_token,
    finalize_search_plan,
    validate_query_history,
)
from boss_hire.state_store import content_hash, jd_hash


PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

RUBRIC_SYSTEM_PROMPT = """根据输入 JD 生成连续排序评分卡 JSON。
只返回 job_title 和 dimensions。dimensions 必须为 3-7 项，权重合计 100。
每项包含 id（字符串）、name（字符串）、weight（整数）、critical（布尔值）、requirement（字符串）、jd_evidence（字符串数组）和五档 anchors 对象：none、weak、partial、strong、exceptional。
jd_evidence 必须是从 jd_text 直接复制的连续原文子串：不得改写、概括、补充标点或合并不相连片段。
输出前逐条检查：每个 jd_evidence 都必须可以用精确字符串匹配在 jd_text 中找到。
不得返回 hard_gates、decision_rules、通过/不通过或总分。"""

SEARCH_PLAN_SYSTEM_PROMPT = """根据输入 JD 提取候选人搜索语义词。
只返回 target_persona、dominant_axis，以及以下字符串数组：anchor_terms、role_terms、context_terms、ecosystem_terms、supply_object_terms、seniority_terms、qualification_terms、skill_terms。
每个词必须是 jd_text 中可精确匹配的连续短词，并且词内不含空白。anchor_terms 放行业或业务强锚点；role_terms 放核心职能；其余数组只放可缩小候选范围的场景、生态、供给对象、资历、资格或技能词。
团队管理、商务谈判、策略制定、结果导向、商家入驻、商家准入、商家运营、数据化管理等泛化要求不要放入 role_terms。每个数组最多 8 项，精准词不足时如实少返回。
不得返回 strategy、factors、routes、id、priority、type、factor_ids、query、证据对象或搜索计划 schema；这些结构、引用、路线组合和 query 全部由程序生成。"""

SEARCH_GENERATOR_VERSION = "semantic-terms-v1"
MAX_SEMANTIC_TERMS_PER_CATEGORY = 8
SEMANTIC_TERM_FIELDS = (
    ("anchor_terms", "anchor"),
    ("role_terms", "role"),
    ("context_terms", "context"),
    ("ecosystem_terms", "ecosystem"),
    ("supply_object_terms", "supply_object"),
    ("seniority_terms", "seniority"),
    ("qualification_terms", "qualification"),
    ("skill_terms", "skill"),
)

EVALUATION_SYSTEM_PROMPT = """根据固定评分卡和脱敏候选人经历生成证据评价 JSON。
只返回 dimension_assessments、evidence、gaps、risks、follow_up_questions、summary。候选人身份由调用方绑定，不要返回 candidate_id。
每个维度评价包含 id（字符串）、level（字符串）、evidence（字符串数组）、reason（字符串）、gaps（字符串数组）；level 只能是 none、weak、partial、strong、exceptional。
顶层 evidence、gaps、risks、follow_up_questions 也必须都是字符串数组。
所有正向 evidence 必须从 candidate 中直接复制连续原文子串，不得改写、概括或补充标点。
输出前逐条检查：每条正向 evidence 都必须能在 candidate 序列化文本中精确匹配；找不到直接原文时降低 level 并留空 evidence。
不得返回 total_score、decision、hard_gate 或通过/不通过。"""

_HTTP_ERROR_BODY_LIMIT = 4096
_HTTP_ERROR_MESSAGE_LIMIT = 500
_PUBLIC_HTTP_ERROR_HEADERS = frozenset(
    {
        "cf-ray",
        "request-id",
        "server",
        "via",
        "x-amzn-requestid",
        "x-openai-request-id",
        "x-request-id",
    }
)


def _redact_text(value: Any) -> Any:
    if isinstance(value, str):
        return EMAIL_RE.sub("[EMAIL_REDACTED]", PHONE_RE.sub("[PHONE_REDACTED]", value))
    if isinstance(value, list):
        return [_redact_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_text(item) for key, item in value.items()}
    return value


def _public_http_error_details(exc: HTTPError) -> dict[str, Any]:
    """Keep provider diagnostics that can identify an outage without storing request data."""
    details: dict[str, Any] = {"status": exc.code}
    headers = {
        str(key).lower(): str(value)
        for key, value in (exc.headers.items() if exc.headers is not None else [])
        if str(key).lower() in _PUBLIC_HTTP_ERROR_HEADERS
    }
    if headers:
        details["headers"] = dict(sorted(headers.items()))

    try:
        raw_body = exc.read(_HTTP_ERROR_BODY_LIMIT)
        body = json.loads(raw_body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        body = None
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        public_error: dict[str, Any] = {}
        for field in ("message", "type", "code"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                public_error[field] = _redact_text(value.strip()[:_HTTP_ERROR_MESSAGE_LIMIT])
        if public_error:
            details["error"] = public_error
    return details


class LlmHttpError(RuntimeError):
    """A sanitized HTTP failure suitable for persistence in local score summaries."""

    def __init__(self, *, operation: str, public_details: Mapping[str, Any]) -> None:
        self.public_details = dict(public_details)
        rendered = json.dumps(self.public_details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        super().__init__(f"LLM {operation} HTTP {self.public_details['status']}; diagnostics={rendered}")


class SearchPlanGenerationError(ValueError):
    def __init__(self, message: str, diagnostics: Mapping[str, Any]) -> None:
        self.diagnostics = dict(diagnostics)
        super().__init__(message)


def redact_resume_for_llm(resume: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    """Build a narrow resume view that excludes identity, contacts and BOSS identifiers."""
    if not isinstance(resume, Mapping):
        raise ValueError("resume 必须是对象")
    stable_candidate_id = str(candidate_id or "").strip()
    if not stable_candidate_id:
        raise ValueError("candidate_id 不能为空")
    basic = resume.get("basic") if isinstance(resume.get("basic"), Mapping) else {}
    expectation = resume.get("expectation") if isinstance(resume.get("expectation"), Mapping) else {}

    def rows(field: str, allowed: tuple[str, ...]) -> list[dict[str, Any]]:
        value = resume.get(field)
        if not isinstance(value, list):
            return []
        return [
            {key: row.get(key) for key in allowed if row.get(key) not in (None, "")}
            for row in value
            if isinstance(row, Mapping)
        ]

    sanitized = {
        "basic": {
            key: basic.get(key)
            for key in ("degree", "work_years")
            if basic.get(key) not in (None, "")
        },
        "expectation": {
            key: expectation.get(key)
            for key in ("position",)
            if expectation.get(key) not in (None, "")
        },
        "work_experience": rows(
            "work_experience",
            ("company", "department", "duration", "position", "responsibility", "performance", "keywords"),
        ),
        "project_experience": rows(
            "project_experience",
            ("name", "role", "duration", "description", "achievement"),
        ),
        "education": rows("education", ("degree", "major")),
        "certifications": list(resume.get("certifications") or []),
        "matches": list(resume.get("matches") or []),
    }
    # This is a control-plane identifier bound by the caller, not resume text.
    # Redacting it can turn an opaque ID containing an 11-digit sequence into a
    # phone-redacted value and break the inventory writeback binding.
    return {"candidate_id": stable_candidate_id, **_redact_text(sanitized)}


class JsonLlm(Protocol):
    model: str

    def complete_json(self, *, operation: str, system_prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OpenAICompatibleJsonLlm:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 180
    opener: Callable[..., Any] = urlopen

    def __post_init__(self) -> None:
        if not self.base_url.strip() or not self.api_key.strip() or not self.model.strip():
            raise ValueError("LLM base_url、api_key 和 model 均不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("LLM timeout_seconds 必须大于 0")

    def complete_json(
        self,
        *,
        operation: str,
        system_prompt: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(dict(payload), ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        request = Request(
            f"{self.base_url.rstrip('/')}/v1/chat/completions",
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise LlmHttpError(
                operation=operation,
                public_details=_public_http_error_details(exc),
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LLM {operation} 请求失败：{type(exc).__name__}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LLM {operation} 未返回可解析 JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"LLM {operation} 必须返回 JSON 对象")
        return result


def generate_continuous_rubric(llm: JsonLlm, jd_text: str) -> dict[str, Any]:
    result = llm.complete_json(
        operation="rubric",
        system_prompt=RUBRIC_SYSTEM_PROMPT,
        payload={"jd_text": jd_text},
    )
    return finalize_continuous_rubric(result, jd_text)


def generate_search_plan(
    llm: JsonLlm,
    jd_text: str,
    *,
    config: SearchPlanConfig,
    route_history: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    target_routes = config.v3_target_route_count
    validated_history = validate_query_history(route_history)
    result = llm.complete_json(
        operation="search_plan",
        system_prompt=SEARCH_PLAN_SYSTEM_PROMPT,
        payload={"jd_text": jd_text},
    )
    grounded = ground_search_plan_generation(result, jd_text)
    diagnostics = {**grounded["diagnostics"], "target_route_count": target_routes}
    if not grounded["routes"]:
        diagnostics["accepted_route_count"] = 0
        raise SearchPlanGenerationError("没有生成可用搜索路线", diagnostics)
    plan = finalize_search_plan(
        grounded["routes"],
        jd_text,
        strategy=grounded["strategy"],
        config=config,
        route_history=validated_history,
    )
    diagnostics["accepted_route_count"] = len(plan["routes"])
    plan["search_generator_version"] = SEARCH_GENERATOR_VERSION
    plan["generation_diagnostics"] = diagnostics
    return plan


def _route_type(category: str | None) -> str:
    return {
        "ecosystem": "ecosystem_role",
        "supply_object": "ecosystem_role",
        "skill": "skill_role",
        "qualification": "qualification_role",
        "seniority": "title",
    }.get(category, "industry_role")


def _default_target_persona(jd_text: str) -> str:
    first_line = next((line.strip() for line in jd_text.splitlines() if line.strip()), "")
    title = first_line.removeprefix("岗位名称：").strip()
    return f"符合{title or '当前 JD'}的候选人"


def ground_search_plan_generation(
    result: Mapping[str, Any],
    jd_text: str,
) -> dict[str, Any]:
    """Ground shallow semantic terms and let the program own every V3 reference."""
    factors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = {category: [] for _, category in SEMANTIC_TERM_FIELDS}
    suggested_count = 0
    for field, category in SEMANTIC_TERM_FIELDS:
        values = result.get(field, [])
        if not isinstance(values, list):
            rejected.append({"field": field, "term": None, "reason": "not_list"})
            continue
        seen: set[str] = set()
        for raw_term in values:
            suggested_count += 1
            if not isinstance(raw_term, str):
                rejected.append({"field": field, "term": None, "reason": "non_string"})
                continue
            token = raw_term.strip()
            public_term = _redact_text(token[:80])
            if not token:
                rejected.append({"field": field, "term": public_term, "reason": "empty"})
                continue
            if any(character.isspace() for character in token):
                rejected.append({"field": field, "term": public_term, "reason": "contains_whitespace"})
                continue
            if DISALLOWED_QUERY_SYNTAX_RE.search(token):
                rejected.append({"field": field, "term": public_term, "reason": "disallowed_syntax"})
                continue
            if len(token) > 12:
                rejected.append({"field": field, "term": public_term, "reason": "too_long"})
                continue
            if token not in jd_text:
                rejected.append({"field": field, "term": public_term, "reason": "not_in_jd"})
                continue
            normalized = token.lower()
            if normalized in seen:
                rejected.append({"field": field, "term": public_term, "reason": "duplicate"})
                continue
            if category == "role" and _is_generic_role_token(token):
                rejected.append({"field": field, "term": public_term, "reason": "generic_role"})
                continue
            if len(by_category[category]) >= MAX_SEMANTIC_TERMS_PER_CATEGORY:
                rejected.append({"field": field, "term": public_term, "reason": "category_limit"})
                continue
            seen.add(normalized)
            factor = {
                "id": f"factor-{category}-{len(by_category[category]) + 1}",
                "category": category,
                "token": token,
                "source_term": token,
                "jd_evidence": [token],
            }
            factors.append(factor)
            by_category[category].append(factor)

    anchors = by_category["anchor"][:1]
    roles = by_category["role"]
    qualifiers = [
        factor
        for _, category in SEMANTIC_TERM_FIELDS[2:]
        for factor in by_category[category]
    ]
    route_factor_sets: list[list[dict[str, Any]]] = []
    if anchors:
        anchor = anchors[0]
        route_factor_sets.extend(
            [anchor, role]
            for role in roles
            if role["token"].lower() != anchor["token"].lower()
        )
        route_factor_sets.extend(
            [anchor, role, qualifier]
            for qualifier in qualifiers
            for role in roles
            if len({anchor["token"].lower(), role["token"].lower(), qualifier["token"].lower()}) == 3
        )

    target_persona = str(result.get("target_persona") or "").strip() or _default_target_persona(jd_text)
    dominant_axis = result.get("dominant_axis")
    if dominant_axis not in ALLOWED_DOMINANT_AXES:
        dominant_axis = "industry" if anchors else "role"
    routes = []
    for index, route_factors in enumerate(route_factor_sets, 1):
        qualifier = route_factors[2] if len(route_factors) == 3 else None
        tokens = [factor["token"] for factor in route_factors]
        routes.append(
            {
                "id": f"route-{index:03d}",
                "priority": index,
                "type": _route_type(qualifier["category"] if qualifier else None),
                "factor_ids": [factor["id"] for factor in route_factors],
                "target_persona": target_persona,
                "reason": "程序组合强锚点、核心职能" + ("和限定词" if qualifier else ""),
                "jd_evidence": tokens,
            }
        )

    dominant_anchor = anchors[0]["token"] if anchors else ""
    diagnostics = {
        "schema_version": 1,
        "contract": "search_generation_diagnostics",
        "search_generator_version": SEARCH_GENERATOR_VERSION,
        "suggested_term_count": suggested_count,
        "accepted_factor_count": len(factors),
        "rejected_factor_count": len(rejected),
        "rejected_terms": rejected,
        "candidate_route_count": len(routes),
        "accepted_route_count": None,
        "ignored_structural_fields": sorted(
            set(result) & {"strategy", "factors", "routes", "id", "priority", "type", "factor_ids", "query"}
        ),
    }
    strategy = {
        "target_persona": target_persona,
        "dominant_axis": dominant_axis,
        "dominant_anchor": dominant_anchor,
        "jd_evidence": [dominant_anchor] if dominant_anchor else [],
        "factors": factors,
    }
    return {"strategy": strategy, "routes": routes, "diagnostics": diagnostics}


def evaluate_redacted_candidate(
    llm: JsonLlm,
    candidate: Mapping[str, Any],
    rubric: Mapping[str, Any],
) -> dict[str, Any]:
    result = llm.complete_json(
        operation="candidate_evaluation",
        system_prompt=EVALUATION_SYSTEM_PROMPT,
        payload={"rubric": dict(rubric), "candidate": dict(candidate)},
    )
    return finalize_continuous_evaluation(result, dict(candidate), dict(rubric))


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ensure_private_directory(path.parent)
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise ValueError(f"已有产物不可覆盖：{path.name}")
        ensure_private_file(path)
        return
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    ensure_private_file(path)


def prepare_single_job_llm_artifacts(
    *,
    job: RecruiterJob,
    llm: JsonLlm,
    output_dir: Path,
    search_config: SearchPlanConfig,
    route_history: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate protected job/rubric/search artifacts using an injected LLM."""
    if not job.is_open:
        raise ValueError("只能为开放岗位生成 LLM 产物")
    jd_text = job.to_jd_text()
    rubric = generate_continuous_rubric(llm, jd_text)
    try:
        search_plan = generate_search_plan(
            llm,
            jd_text,
            config=search_config,
            route_history=route_history,
        )
    except SearchPlanGenerationError as exc:
        diagnostics_path = Path(output_dir) / "search_generation_diagnostics.json"
        _write_immutable_json(diagnostics_path, exc.diagnostics)
        raise SearchPlanGenerationError(
            f"{exc}；诊断已保存：{diagnostics_path}",
            exc.diagnostics,
        ) from exc
    if search_plan.get("schema_version") != 3:
        raise ValueError("新生成的 search_plan 必须使用 schema V3")
    return write_single_job_llm_artifacts(
        job=job,
        rubric=rubric,
        search_plan=search_plan,
        output_dir=output_dir,
        llm_model=llm.model,
    )


def write_single_job_llm_artifacts(
    *,
    job: RecruiterJob,
    rubric: Mapping[str, Any],
    search_plan: Mapping[str, Any],
    output_dir: Path,
    llm_model: str,
) -> dict[str, Any]:
    """Write one run's immutable copies of current job-scoped LLM artifacts."""
    if not job.is_open:
        raise ValueError("只能为开放岗位写入 LLM 产物")
    jd_text = job.to_jd_text()
    expected_jd_hash = jd_hash(jd_text)
    if rubric.get("source_jd_hash") != expected_jd_hash:
        raise ValueError("rubric 与当前 JD 不一致")
    if search_plan.get("source_jd_hash") != expected_jd_hash:
        raise ValueError("search_plan 与当前 JD 不一致")
    if search_plan.get("search_generator_version") != SEARCH_GENERATOR_VERSION:
        raise ValueError("search_plan 生成契约版本无效")
    diagnostics = search_plan.get("generation_diagnostics")
    if not isinstance(diagnostics, Mapping):
        raise ValueError("search_plan 缺少生成诊断")
    job_snapshot = {
        "schema_version": 1,
        "contract": "single_job_snapshot",
        "job": job.to_dict(),
        "jd_text": jd_text,
    }
    manifest = {
        "schema_version": 1,
        "contract": "single_job_llm_artifacts",
        "job_id": job.encrypt_job_id,
        "llm_model": llm_model,
        "job_snapshot_hash": content_hash(job_snapshot),
        "rubric_version": rubric["version"],
        "rubric_hash": content_hash(rubric),
        "search_generator_version": SEARCH_GENERATOR_VERSION,
        "search_generation_diagnostics_hash": content_hash(diagnostics),
        "search_plan_schema_version": search_plan["schema_version"],
        "search_plan_version": search_plan["version"],
        "search_plan_hash": content_hash(search_plan),
    }
    target = Path(output_dir)
    _write_immutable_json(target / "job.json", job_snapshot)
    _write_immutable_json(target / "rubric.json", rubric)
    _write_immutable_json(target / "search_plan.json", search_plan)
    _write_immutable_json(target / "search_generation_diagnostics.json", diagnostics)
    _write_immutable_json(target / "manifest.json", manifest)
    return {
        "job": job_snapshot,
        "rubric": dict(rubric),
        "search_plan": dict(search_plan),
        "manifest": manifest,
    }
