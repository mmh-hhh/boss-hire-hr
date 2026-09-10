from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from boss_hire.state_store import content_hash, jd_hash


ALLOWED_DOMINANT_AXES = {
    "industry",
    "role",
    "skill",
    "qualification",
    "ecosystem",
}
ALLOWED_ROUTE_TYPES = {
    "title",
    "industry_role",
    "skill_role",
    "qualification_role",
    "ecosystem_role",
    "adjacent",
    "exploration",
}
MIN_SEARCH_ROUTES = 3
MAX_SEARCH_ROUTES = 6
MIN_V3_SEARCH_ROUTES = 8
MAX_V3_SEARCH_ROUTES = 12
MAX_V3_CANDIDATE_ROUTES = 512
MAX_QUERY_LENGTH = 16
EXPANSION_ROUTE_TYPES = {"adjacent", "exploration"}
GENERIC_ONLY_QUERIES = {
    "商务谈判",
    "团队管理",
    "团队建设",
    "商家入驻",
    "商家准入",
    "商家运营",
    "商家分层运营",
    "数据化招商",
    "数据化管理",
    "策略制定",
    "项目管理",
    "沟通协调",
    "结果导向",
}
GENERIC_ROLE_SUFFIXES = {
    "负责人",
    "总监",
    "经理",
    "主管",
    "专家",
    "专员",
    "运营",
}
DISALLOWED_QUERY_SYNTAX_RE = re.compile(
    r"(?<![A-Za-z])(?:OR|AND)(?![A-Za-z])|[|/、,，;；()（）\[\]【】\"“”]",
    re.IGNORECASE,
)
ALLOWED_FACTOR_CATEGORIES = {
    "anchor",
    "role",
    "context",
    "ecosystem",
    "supply_object",
    "seniority",
    "qualification",
    "skill",
}
CORE_ROLE_CATEGORY = "role"
STRONG_ANCHOR_CATEGORY = "anchor"
ECOSYSTEM_FACTOR_CATEGORIES = {"ecosystem", "supply_object"}
MAX_ROUTES_PER_FAMILY = 3
QUERY_HISTORY_FIELDS = {
    "query",
    "tokens",
    "last_used_at",
    "execution_count",
    "returned_count",
    "new_to_inventory_count",
    "marginal_new_count",
    "duplicate_rate",
    "evaluated_count",
    "median_score",
}


@dataclass(frozen=True)
class SearchPlanConfig:
    """Legacy construction limits retained for callers of the V1 generator."""

    total_query_budget: int
    min_budget_per_route: int = 1

    def __post_init__(self) -> None:
        if self.total_query_budget <= 0:
            raise ValueError("total_query_budget 必须大于 0")
        if self.min_budget_per_route <= 0:
            raise ValueError("min_budget_per_route 必须大于 0")

    @property
    def route_capacity(self) -> int:
        return self.total_query_budget // self.min_budget_per_route

    @property
    def route_limit(self) -> int:
        if self.route_capacity < MIN_SEARCH_ROUTES:
            raise ValueError("搜索计划兼容配置必须至少容纳 3 条路线")
        return min(MAX_SEARCH_ROUTES, self.route_capacity)

    @property
    def v3_target_route_count(self) -> int:
        if self.route_capacity < MIN_V3_SEARCH_ROUTES:
            raise ValueError("搜索计划 V3 配置必须至少容纳 8 条路线")
        return min(MAX_V3_SEARCH_ROUTES, self.route_capacity)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _evidence(value: Any, jd_text: str, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(_text(item) and _text(item) in jd_text for item in value)
    ):
        raise ValueError(f"{field} 缺少可定位 JD 原文")
    return [_text(item) for item in value]


def _validate_strategy(strategy: Mapping[str, Any], jd_text: str) -> dict[str, Any]:
    if not isinstance(strategy, Mapping):
        raise ValueError("search strategy 必须是对象")
    target_persona = _text(strategy.get("target_persona"))
    dominant_axis = strategy.get("dominant_axis")
    dominant_anchor = _text(strategy.get("dominant_anchor"))
    if not target_persona:
        raise ValueError("search strategy 缺少 target_persona")
    if dominant_axis not in ALLOWED_DOMINANT_AXES:
        raise ValueError("search strategy dominant_axis 无效")
    if not dominant_anchor or dominant_anchor not in jd_text:
        raise ValueError("search strategy dominant_anchor 必须来自 JD")
    return {
        "target_persona": target_persona,
        "dominant_axis": dominant_axis,
        "dominant_anchor": dominant_anchor,
        "jd_evidence": _evidence(strategy.get("jd_evidence"), jd_text, "search strategy"),
    }


def _validate_query(query: str, route_id: str) -> str:
    if not query or "\n" in query or "\r" in query:
        raise ValueError(f"搜索 query {route_id} 不能为空或包含换行")
    if DISALLOWED_QUERY_SYNTAX_RE.search(query):
        raise ValueError(f"搜索 query {route_id} 必须是单个短语，禁止布尔词或列表分隔符")
    compact = re.sub(r"\s+", "", query)
    if len(compact) > MAX_QUERY_LENGTH:
        raise ValueError(f"搜索 query {route_id} 过长，最多 {MAX_QUERY_LENGTH} 个字符")
    remaining = compact.lower()
    for generic_term in sorted(GENERIC_ONLY_QUERIES, key=len, reverse=True):
        remaining = remaining.replace(generic_term.lower(), "")
    if not remaining or remaining in {value.lower() for value in GENERIC_ROLE_SUFFIXES}:
        raise ValueError(f"搜索 query {route_id} 不能只包含泛化职责词")
    return re.sub(r"\s+", " ", query).strip()


def _validate_factor_token(value: Any, factor_id: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"搜索因子 {factor_id} token 必须是字符串")
    token = value.strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError(f"搜索因子 {factor_id} token 必须是单个短词")
    if DISALLOWED_QUERY_SYNTAX_RE.search(token):
        raise ValueError(f"搜索因子 {factor_id} token 禁止布尔词或列表分隔符")
    if len(token) > 12:
        raise ValueError(f"搜索因子 {factor_id} token 过长")
    return token


def _validate_v3_strategy(strategy: Mapping[str, Any], jd_text: str) -> dict[str, Any]:
    validated = _validate_strategy(strategy, jd_text)
    raw_factors = strategy.get("factors")
    if not isinstance(raw_factors, list) or not raw_factors:
        raise ValueError("search strategy 缺少 factors")

    factors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for factor in raw_factors:
        if not isinstance(factor, Mapping):
            raise ValueError("搜索因子必须是 JSON 对象")
        factor_id = _text(factor.get("id"))
        if not factor_id or factor_id in seen_ids:
            raise ValueError("搜索因子 id 缺失或重复")
        category = factor.get("category")
        if category not in ALLOWED_FACTOR_CATEGORIES:
            raise ValueError(f"搜索因子 {factor_id} category 无效")
        token = _validate_factor_token(factor.get("token"), factor_id)
        source_term = _text(factor.get("source_term"))
        evidence = _evidence(factor.get("jd_evidence"), jd_text, f"搜索因子 {factor_id}")
        if not source_term or source_term not in jd_text or not any(source_term in item for item in evidence):
            raise ValueError(f"搜索因子 {factor_id} source_term 缺少可定位 JD 原文")
        factors.append(
            {
                "id": factor_id,
                "category": category,
                "token": token,
                "source_term": source_term,
                "jd_evidence": evidence,
            }
        )
        seen_ids.add(factor_id)

    validated["factors"] = factors
    return validated


def _factor_matches_dominant_anchor(factor: Mapping[str, Any], dominant_anchor: str) -> bool:
    anchor = dominant_anchor.lower()
    return factor.get("category") == STRONG_ANCHOR_CATEGORY and (
        anchor in _text(factor.get("token")).lower()
        or anchor in _text(factor.get("source_term")).lower()
    )


def _semantic_token(value: str) -> str:
    compact = re.sub(r"\s+", "", value).lower()
    for suffix in sorted(GENERIC_ROLE_SUFFIXES, key=len, reverse=True):
        normalized_suffix = suffix.lower()
        if compact.endswith(normalized_suffix) and len(compact) > len(normalized_suffix):
            return compact[: -len(normalized_suffix)] + "<seniority>"
    return compact


def _is_generic_role_token(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).lower()
    for suffix in sorted(GENERIC_ROLE_SUFFIXES, key=len, reverse=True):
        normalized_suffix = suffix.lower()
        if normalized.endswith(normalized_suffix):
            normalized = normalized[: -len(normalized_suffix)]
            break
    return normalized in {term.lower() for term in GENERIC_ONLY_QUERIES}


def query_history_key(tokens: list[str]) -> str:
    if not isinstance(tokens, list) or not 2 <= len(tokens) <= 3:
        raise ValueError("query history tokens 必须包含 2-3 个短词")
    normalized = [_semantic_token(_validate_factor_token(token, "history")) for token in tokens]
    if len(set(normalized)) != len(normalized):
        raise ValueError("query history tokens 不能重复")
    return "|".join(sorted(normalized))


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"query history {field} 必须是非负整数")
    return value


def _bounded_number(value: Any, field: str, *, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"query history {field} 必须是数字")
    number = float(value)
    if not 0 <= number <= maximum:
        raise ValueError(f"query history {field} 超出范围")
    return number


def validate_query_history(
    route_history: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Validate a local aggregate-only query history summary."""
    if route_history is None:
        return {}
    if not isinstance(route_history, Mapping):
        raise ValueError("query history 必须是对象")

    validated: dict[str, dict[str, Any]] = {}
    for raw_key, raw_row in route_history.items():
        key = _text(raw_key)
        if not key or not isinstance(raw_row, Mapping):
            raise ValueError("query history 条目无效")
        unknown = set(raw_row) - QUERY_HISTORY_FIELDS
        if unknown:
            raise ValueError(f"query history 包含未知字段：{', '.join(sorted(unknown))}")

        raw_tokens = raw_row.get("tokens")
        if not isinstance(raw_tokens, list):
            raise ValueError("query history tokens 必须是数组")
        tokens = [_validate_factor_token(token, "history") for token in raw_tokens]
        expected_key = query_history_key(tokens)
        if key != expected_key:
            raise ValueError("query history key 与 tokens 不一致")
        query = _text(raw_row.get("query"))
        if not query or "\n" in query or "\r" in query or DISALLOWED_QUERY_SYNTAX_RE.search(query):
            raise ValueError("query history query 必须是一个完整搜索组合")
        query = re.sub(r"\s+", " ", query).strip()
        if re.sub(r"\s+", "", query).lower() != "".join(tokens).lower():
            raise ValueError("query history query 与 tokens 不一致")

        execution_count = _non_negative_int(raw_row.get("execution_count"), "execution_count")
        returned_count = _non_negative_int(raw_row.get("returned_count", 0), "returned_count")
        new_to_inventory_count = _non_negative_int(
            raw_row.get("new_to_inventory_count", 0),
            "new_to_inventory_count",
        )
        marginal_new_count = _non_negative_int(
            raw_row.get("marginal_new_count", 0),
            "marginal_new_count",
        )
        evaluated_count = _non_negative_int(raw_row.get("evaluated_count", 0), "evaluated_count")
        if not marginal_new_count <= new_to_inventory_count <= returned_count:
            raise ValueError("query history 新增数量不能超过返回数量")
        if evaluated_count > returned_count:
            raise ValueError("query history evaluated_count 不能超过 returned_count")

        duplicate_rate = _bounded_number(
            raw_row.get("duplicate_rate", 0.0),
            "duplicate_rate",
            maximum=1.0,
        )
        last_used_at = raw_row.get("last_used_at")
        if last_used_at is not None:
            if not isinstance(last_used_at, str) or not last_used_at.strip():
                raise ValueError("query history last_used_at 必须是 ISO-8601 字符串或 null")
            try:
                datetime.fromisoformat(last_used_at.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("query history last_used_at 必须是 ISO-8601 字符串或 null") from exc
            last_used_at = last_used_at.strip()

        median_score = raw_row.get("median_score")
        if median_score is not None:
            median_score = _bounded_number(median_score, "median_score", maximum=100.0)
        if execution_count == 0 and any(
            value
            for value in (
                returned_count,
                new_to_inventory_count,
                marginal_new_count,
                evaluated_count,
                duplicate_rate,
            )
        ):
            raise ValueError("query history 未执行查询不能包含结果统计")
        if execution_count == 0 and last_used_at is not None:
            raise ValueError("query history 未执行查询不能包含 last_used_at")
        if evaluated_count == 0 and median_score is not None:
            raise ValueError("query history 无已评分候选时 median_score 必须为 null")
        if evaluated_count > 0 and median_score is None:
            raise ValueError("query history 有已评分候选时必须提供 median_score")

        validated[key] = {
            "query": query,
            "tokens": tokens,
            "last_used_at": last_used_at,
            "execution_count": execution_count,
            "returned_count": returned_count,
            "new_to_inventory_count": new_to_inventory_count,
            "marginal_new_count": marginal_new_count,
            "duplicate_rate": duplicate_rate,
            "evaluated_count": evaluated_count,
            "median_score": median_score,
        }
    return {key: validated[key] for key in sorted(validated)}


def _route_family(route_type: str, factors: list[Mapping[str, Any]]) -> str:
    categories = {str(factor["category"]) for factor in factors}
    if route_type in EXPANSION_ROUTE_TYPES:
        return "expansion"
    if route_type == "title" or "seniority" in categories:
        return "title"
    if categories & ECOSYSTEM_FACTOR_CATEGORIES:
        return "ecosystem_role"
    if "context" in categories:
        return "context_role"
    if "skill" in categories:
        return "skill_role"
    if "qualification" in categories:
        return "qualification_role"
    return "direct_role"


def _history_annotation(
    candidate: Mapping[str, Any],
    route_history: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    key = query_history_key(list(candidate["tokens"]))
    row = route_history.get(key)
    if row is None or row["execution_count"] == 0:
        return {
            "key": key,
            "status": "unseen",
            "execution_count": 0,
            "last_used_at": None,
            "marginal_new_count": 0,
            "duplicate_rate": 0.0,
            "evaluated_count": 0,
            "median_score": None,
        }
    return {
        "key": key,
        "status": "executed",
        "execution_count": row["execution_count"],
        "last_used_at": row["last_used_at"],
        "marginal_new_count": row["marginal_new_count"],
        "duplicate_rate": row["duplicate_rate"],
        "evaluated_count": row["evaluated_count"],
        "median_score": row["median_score"],
    }


def _history_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    history = candidate["history"]
    expansion_tier = 1 if candidate["family"] == "expansion" else 0
    executed_tier = 1 if history["status"] == "executed" else 0
    median_score = history["median_score"] if history["median_score"] is not None else -1.0
    return (
        expansion_tier,
        executed_tier,
        -history["marginal_new_count"],
        history["duplicate_rate"],
        -median_score,
        history["execution_count"],
        candidate["priority"],
        candidate["id"],
    )


def _finalize_v3_search_plan(
    routes: list[dict[str, Any]],
    jd_text: str,
    *,
    strategy: Mapping[str, Any],
    config: SearchPlanConfig,
    route_history: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    target_route_count = config.v3_target_route_count
    if not isinstance(routes, list) or len(routes) > MAX_V3_CANDIDATE_ROUTES:
        raise ValueError(f"搜索候选路线数量不能超过 {MAX_V3_CANDIDATE_ROUTES}")

    validated_strategy = _validate_v3_strategy(strategy, jd_text)
    factors_by_id = {factor["id"]: factor for factor in validated_strategy["factors"]}
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_priorities: set[int] = set()

    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("搜索路线必须是 JSON 对象")
        route_id = _text(route.get("id"))
        if not route_id or route_id in seen_ids:
            raise ValueError("搜索路线 id 缺失或重复")
        if "query" in route:
            raise ValueError(f"搜索路线 {route_id} query 必须由程序根据 tokens 生成")

        priority = route.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            raise ValueError(f"搜索路线 {route_id} priority 无效")
        if priority in seen_priorities:
            raise ValueError("搜索路线 priority 缺失、重复或不连续")

        route_type = route.get("type")
        if route_type not in ALLOWED_ROUTE_TYPES:
            raise ValueError(f"搜索路线 {route_id} type 无效")

        factor_ids = route.get("factor_ids")
        if (
            not isinstance(factor_ids, list)
            or not 2 <= len(factor_ids) <= 3
            or not all(isinstance(factor_id, str) and factor_id in factors_by_id for factor_id in factor_ids)
            or len(set(factor_ids)) != len(factor_ids)
        ):
            raise ValueError(f"搜索路线 {route_id} 必须引用 2-3 个有效搜索因子")
        route_factors = [factors_by_id[factor_id] for factor_id in factor_ids]
        anchor_factors = [
            factor
            for factor in route_factors
            if _factor_matches_dominant_anchor(factor, validated_strategy["dominant_anchor"])
        ]
        role_factors = [
            factor for factor in route_factors if factor["category"] == CORE_ROLE_CATEGORY
        ]
        if not anchor_factors:
            raise ValueError(f"搜索路线 {route_id} 缺少强锚点")
        if not role_factors:
            raise ValueError(f"搜索路线 {route_id} 缺少核心职能")
        if any(_is_generic_role_token(factor["token"]) for factor in role_factors):
            raise ValueError(f"搜索路线 {route_id} 的泛化动作不能作为核心职能")
        if route_type == "ecosystem_role" and not any(
            factor["category"] in ECOSYSTEM_FACTOR_CATEGORIES for factor in route_factors
        ):
            raise ValueError(f"搜索路线 {route_id} 缺少生态或供给对象因子")

        target_persona = _text(route.get("target_persona"))
        reason = _text(route.get("reason"))
        if not target_persona:
            raise ValueError(f"搜索路线 {route_id} 缺少 target_persona")
        if not reason:
            raise ValueError(f"搜索路线 {route_id} 缺少 reason")

        tokens = [factor["token"] for factor in route_factors]
        if len({_semantic_token(token) for token in tokens}) != len(tokens):
            raise ValueError(f"搜索路线 {route_id} 不能包含重复 token")
        query = " ".join(tokens)
        family = _route_family(str(route_type), route_factors)
        candidate = {
                "id": route_id,
                "priority": priority,
                "type": route_type,
                "factor_ids": list(factor_ids),
                "tokens": tokens,
                "query": query,
                "signature": [factor["category"] for factor in route_factors],
                "family": family,
                "target_persona": target_persona,
                "reason": reason,
                "jd_evidence": _evidence(
                    route.get("jd_evidence"),
                    jd_text,
                    f"搜索路线 {route_id}",
                ),
            }
        candidate["history"] = _history_annotation(candidate, route_history)
        candidate["ordering_reason"] = (
            "unseen_precise_combination"
            if candidate["history"]["status"] == "unseen"
            else "executed_history_ranked_by_marginal_supply_and_quality"
        )
        candidates.append(candidate)
        seen_ids.add(route_id)
        seen_priorities.add(priority)

    candidates.sort(key=_history_sort_key)
    validated: list[dict[str, Any]] = []
    seen_compact_queries: set[str] = set()
    seen_token_sets: set[tuple[str, ...]] = set()
    family_counts: dict[str, int] = {}
    for candidate in candidates:
        compact_query = re.sub(r"\s+", "", candidate["query"]).lower()
        semantic_token_set = tuple(sorted(_semantic_token(token) for token in candidate["tokens"]))
        family = candidate["family"]
        family_limit = 1 if family == "expansion" else MAX_ROUTES_PER_FAMILY
        if compact_query in seen_compact_queries or semantic_token_set in seen_token_sets:
            continue
        if family_counts.get(family, 0) >= family_limit:
            continue
        selected = dict(candidate)
        selected["priority"] = len(validated) + 1
        validated.append(selected)
        seen_compact_queries.add(compact_query)
        seen_token_sets.add(semantic_token_set)
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(validated) == target_route_count:
            break
    fingerprint_payload = {
        "jd": jd_text,
        "target_route_count": target_route_count,
        "strategy": validated_strategy,
        "routes": validated,
    }
    return {
        "schema_version": 3,
        "contract": "generic_search_plan",
        "version": f"search-{content_hash(fingerprint_payload)[:12]}",
        "source_jd_hash": jd_hash(jd_text),
        "target_route_count": target_route_count,
        "generation_shortfall": target_route_count - len(validated),
        "strategy": validated_strategy,
        "routes": validated,
    }


def finalize_search_plan(
    routes: list[dict[str, Any]],
    jd_text: str,
    *,
    strategy: Mapping[str, Any],
    config: SearchPlanConfig,
    route_history: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and freeze a V3 factor plan or a legacy V2 query plan."""
    validated_history = validate_query_history(route_history)
    if isinstance(strategy, Mapping) and "factors" in strategy:
        return _finalize_v3_search_plan(
            routes,
            jd_text,
            strategy=strategy,
            config=config,
            route_history=validated_history,
        )
    if validated_history:
        raise ValueError("Search Plan V2 暂不接受历史收益自动改词或调序")
    max_routes = config.route_limit
    if not isinstance(routes, list) or not MIN_SEARCH_ROUTES <= len(routes) <= max_routes:
        raise ValueError(f"搜索路线数量必须为 {MIN_SEARCH_ROUTES}-{max_routes}")

    validated_strategy = _validate_strategy(strategy, jd_text)
    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    seen_priorities: set[int] = set()
    expansion_count = 0

    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("搜索路线必须是 JSON 对象")
        route_id = _text(route.get("id"))
        if not route_id or route_id in seen_ids:
            raise ValueError("搜索路线 id 缺失或重复")

        priority = route.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            raise ValueError(f"搜索路线 {route_id} priority 无效")
        if priority in seen_priorities:
            raise ValueError("搜索路线 priority 缺失、重复或不连续")

        route_type = route.get("type")
        if route_type not in ALLOWED_ROUTE_TYPES:
            raise ValueError(f"搜索路线 {route_id} type 无效")
        if route_type in EXPANSION_ROUTE_TYPES:
            expansion_count += 1

        query = _validate_query(_text(route.get("query")), route_id)
        normalized_query = re.sub(r"\s+", "", query).lower()
        if normalized_query in seen_queries:
            raise ValueError("搜索 query 缺失或重复")
        if (
            validated_strategy["dominant_axis"] == "industry"
            and validated_strategy["dominant_anchor"].lower() not in query.lower()
        ):
            raise ValueError(f"搜索路线 {route_id} 缺少主导行业锚点")

        target_persona = _text(route.get("target_persona"))
        reason = _text(route.get("reason"))
        if not target_persona:
            raise ValueError(f"搜索路线 {route_id} 缺少 target_persona")
        if not reason:
            raise ValueError(f"搜索路线 {route_id} 缺少 reason")

        validated.append(
            {
                "id": route_id,
                "priority": priority,
                "type": route_type,
                "query": query,
                "target_persona": target_persona,
                "reason": reason,
                "jd_evidence": _evidence(
                    route.get("jd_evidence"),
                    jd_text,
                    f"搜索路线 {route_id}",
                ),
            }
        )
        seen_ids.add(route_id)
        seen_queries.add(normalized_query)
        seen_priorities.add(priority)

    if expansion_count > 1:
        raise ValueError("相邻或探索路线最多一条")
    if seen_priorities != set(range(1, len(validated) + 1)):
        raise ValueError("搜索路线 priority 缺失、重复或不连续")

    validated.sort(key=lambda row: row["priority"])
    fingerprint_payload = {
        "jd": jd_text,
        "strategy": validated_strategy,
        "routes": validated,
    }
    return {
        "schema_version": 2,
        "contract": "generic_search_plan",
        "version": f"search-{content_hash(fingerprint_payload)[:12]}",
        "source_jd_hash": jd_hash(jd_text),
        "strategy": validated_strategy,
        "routes": validated,
    }
