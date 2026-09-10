from __future__ import annotations

import json
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from boss_hire.recruiter_jobs import RecruiterJob
from boss_hire.search_plan import SearchPlanConfig, query_history_key
from boss_hire.state_store import content_hash
from boss_hire.single_job_llm import (
    EVALUATION_SYSTEM_PROMPT,
    OpenAICompatibleJsonLlm,
    SEARCH_GENERATOR_VERSION,
    SearchPlanGenerationError,
    evaluate_redacted_candidate,
    generate_continuous_rubric,
    generate_search_plan,
    ground_search_plan_generation,
    prepare_single_job_llm_artifacts,
    redact_resume_for_llm,
)


JD = """岗位名称：平台招商负责人
负责重点商家拓展、平台招商策略、商务谈判和招商团队建设。
需要理解电商平台经营和商家增长。
"""

ANCHORS = {
    "none": "没有相关证据",
    "weak": "只有相邻证据",
    "partial": "有部分直接证据",
    "strong": "有完整直接证据",
    "exceptional": "有规模化直接证据",
}


def rubric_result() -> dict[str, object]:
    return {
        "job_title": "平台招商负责人",
        "dimensions": [
            {"id": "merchant", "name": "商家拓展", "weight": 40, "critical": True, "requirement": "评价商家拓展", "jd_evidence": ["负责重点商家拓展"], "anchors": ANCHORS},
            {"id": "strategy", "name": "平台策略", "weight": 35, "critical": True, "requirement": "评价平台招商策略", "jd_evidence": ["平台招商策略"], "anchors": ANCHORS},
            {"id": "collaboration", "name": "协作", "weight": 25, "critical": False, "requirement": "评价商务谈判和团队协作", "jd_evidence": ["商务谈判和招商团队建设"], "anchors": ANCHORS},
        ],
    }


def search_semantics_result() -> dict[str, object]:
    return {
        "target_persona": "做过平台招商的电商平台人才",
        "dominant_axis": "role",
        "anchor_terms": ["平台招商"],
        "role_terms": ["商家拓展", "招商策略", "商家增长"],
        "context_terms": ["电商平台"],
        "ecosystem_terms": [],
        "supply_object_terms": [],
        "seniority_terms": [],
        "qualification_terms": [],
        "skill_terms": [],
    }


class FakeLlm:
    model = "fake-model"

    def __init__(self, results: dict[str, dict[str, object]]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def complete_json(self, *, operation: str, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append({"operation": operation, "system_prompt": system_prompt, "payload": payload})
        return self.results[operation]


class FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


class SingleJobLlmTests(unittest.TestCase):
    def open_job(self) -> RecruiterJob:
        return RecruiterJob(
            encrypt_job_id="job-1",
            name="平台招商负责人",
            online_status=1,
            detail_status=1,
            description="负责重点商家拓展、平台招商策略、商务谈判和招商团队建设。\n需要理解电商平台经营和商家增长。",
            position_name="招商负责人",
            city="上海",
            address="",
            salary_low_k=30,
            salary_high_k=50,
            salary_months=14,
            experience_code=106,
            experience_label="5-10年",
            degree_code=203,
            degree_label="本科",
        )

    def test_resume_is_narrow_and_removes_identity_contacts_and_platform_ids(self) -> None:
        sanitized = redact_resume_for_llm(
            {
                "basic": {"name": "张三", "phone": "13800138000", "email": "a@example.com", "degree": "本科", "work_years": "8年"},
                "encryptGeekId": "boss-candidate-id",
                "securityId": "boss-security-id",
                "work_experience": [{"company": "某平台", "position": "招商负责人", "responsibility": "联系 13800138000，负责重点商家拓展"}],
                "education": [{"school": "某学校", "degree": "本科", "major": "市场营销"}],
            },
            "candidate-local-1",
        )

        text = json.dumps(sanitized, ensure_ascii=False)
        self.assertEqual(sanitized["candidate_id"], "candidate-local-1")
        self.assertNotIn("张三", text)
        self.assertNotIn("boss-candidate-id", text)
        self.assertNotIn("boss-security-id", text)
        self.assertNotIn("某学校", text)
        self.assertNotIn("13800138000", text)
        self.assertIn("[PHONE_REDACTED]", text)

    def test_generates_valid_rubric_and_program_compiled_search_plan(self) -> None:
        llm = FakeLlm({"rubric": rubric_result(), "search_plan": search_semantics_result()})

        rubric = generate_continuous_rubric(llm, JD)
        plan = generate_search_plan(llm, JD, config=SearchPlanConfig(total_query_budget=8))

        self.assertEqual(rubric["contract"], "continuous_ranking")
        self.assertEqual(plan["contract"], "generic_search_plan")
        self.assertEqual(plan["schema_version"], 3)
        self.assertEqual(plan["search_generator_version"], SEARCH_GENERATOR_VERSION)
        self.assertEqual(plan["routes"][0]["query"], "平台招商 商家拓展")
        self.assertTrue(all(route["query"] == " ".join(route["tokens"]) for route in plan["routes"]))
        self.assertEqual([call["operation"] for call in llm.calls], ["rubric", "search_plan"])
        self.assertEqual(llm.calls[1]["payload"], {"jd_text": JD})
        self.assertNotIn("hard_gates", rubric)

    def test_llm_returns_only_shallow_semantics_and_program_builds_v3_structure(self) -> None:
        result = search_semantics_result()
        result["routes"] = [{"query": "模型伪造路线"}]

        compiled = ground_search_plan_generation(result, JD)

        prompt = FakeLlm({"search_plan": result})
        generate_search_plan(prompt, JD, config=SearchPlanConfig(total_query_budget=8))
        self.assertIn("不得返回 strategy、factors、routes", prompt.calls[0]["system_prompt"])
        self.assertNotIn("query_history", prompt.calls[0]["payload"])
        self.assertEqual(
            [factor["id"] for factor in compiled["strategy"]["factors"]],
            ["factor-anchor-1", "factor-role-1", "factor-role-2", "factor-role-3", "factor-context-1"],
        )
        self.assertEqual(compiled["routes"][0]["factor_ids"], ["factor-anchor-1", "factor-role-1"])
        self.assertNotIn("query", compiled["routes"][0])
        self.assertEqual(compiled["diagnostics"]["candidate_route_count"], 6)

    def test_semantic_terms_are_grounded_and_invalid_suggestions_cannot_widen_routes(self) -> None:
        result = search_semantics_result()
        result["role_terms"] = ["商家增长", "商务谈判", "不存在的职能", "两个 词", 123]
        result["skill_terms"] = ["商家增长"]

        compiled = ground_search_plan_generation(result, JD)

        self.assertEqual(
            [(factor["category"], factor["token"]) for factor in compiled["strategy"]["factors"]],
            [("anchor", "平台招商"), ("role", "商家增长"), ("context", "电商平台"), ("skill", "商家增长")],
        )
        self.assertTrue(all(item in JD for route in compiled["routes"] for item in route["jd_evidence"]))
        self.assertTrue(all(2 <= len(route["factor_ids"]) <= 3 for route in compiled["routes"]))
        self.assertEqual(
            {row["reason"] for row in compiled["diagnostics"]["rejected_terms"]},
            {"generic_role", "not_in_jd", "contains_whitespace", "non_string"},
        )

    def test_v3_generation_keeps_history_in_program_not_model_prompt(self) -> None:
        tokens = ["平台招商", "商家增长"]
        history = {
            query_history_key(tokens): {
                "query": "平台招商 商家增长",
                "tokens": tokens,
                "last_used_at": "2026-09-03T16:30:00+08:00",
                "execution_count": 1,
                "returned_count": 15,
                "new_to_inventory_count": 5,
                "marginal_new_count": 2,
                "duplicate_rate": 0.5,
                "evaluated_count": 5,
                "median_score": 40,
            }
        }
        llm = FakeLlm({"search_plan": search_semantics_result()})

        with patch(
            "boss_hire.single_job_llm.finalize_search_plan",
            return_value={"schema_version": 3, "routes": [{"id": "route-1"}]},
        ) as finalize:
            generate_search_plan(
                llm,
                JD,
                config=SearchPlanConfig(total_query_budget=8),
                route_history=history,
            )

        self.assertEqual(llm.calls[0]["payload"], {"jd_text": JD})
        self.assertEqual(finalize.call_args.kwargs["route_history"], history)

    def test_program_can_rank_more_candidates_than_the_final_route_limit(self) -> None:
        result = search_semantics_result()
        result["context_terms"] = ["电商平台", "平台经营"]
        result["skill_terms"] = ["商务谈判", "招商团队"]

        plan = generate_search_plan(
            FakeLlm({"search_plan": result}),
            JD,
            config=SearchPlanConfig(total_query_budget=8),
        )

        self.assertGreater(plan["generation_diagnostics"]["candidate_route_count"], 8)
        self.assertEqual(len(plan["routes"]), 8)

    def test_v3_generation_rejects_capacity_below_eight_before_calling_llm(self) -> None:
        llm = FakeLlm({"search_plan": search_semantics_result()})

        with self.assertRaisesRegex(ValueError, "至少容纳 8 条"):
            generate_search_plan(
                llm,
                JD,
                config=SearchPlanConfig(total_query_budget=7),
            )

        self.assertEqual(llm.calls, [])

    def test_zero_compiled_routes_are_not_ready(self) -> None:
        result = search_semantics_result()
        result["anchor_terms"] = []
        llm = FakeLlm({"search_plan": result})

        with self.assertRaisesRegex(SearchPlanGenerationError, "没有生成可用搜索路线") as raised:
            generate_search_plan(llm, JD, config=SearchPlanConfig(total_query_budget=8))

        self.assertEqual(raised.exception.diagnostics["accepted_route_count"], 0)

    def test_evaluation_is_grounded_and_program_computes_score(self) -> None:
        self.assertIn("不要返回 candidate_id", EVALUATION_SYSTEM_PROMPT)
        rubric_llm = FakeLlm({"rubric": rubric_result()})
        rubric = generate_continuous_rubric(rubric_llm, JD)
        candidate = redact_resume_for_llm(
            {"work_experience": [{"responsibility": "负责重点商家拓展", "performance": "一年引入500家商家"}]},
            "candidate-local-1",
        )
        evaluation_llm = FakeLlm(
            {
                "candidate_evaluation": {
                    "candidate_id": "candidate-local-1",
                    "dimension_assessments": [
                        {"id": "merchant", "level": "exceptional", "evidence": ["一年引入500家商家"], "reason": "有规模结果", "gaps": []},
                        {"id": "strategy", "level": "none", "evidence": [], "reason": "缺少策略证据", "gaps": ["策略范围待确认"]},
                        {"id": "collaboration", "level": "none", "evidence": [], "reason": "缺少协作证据", "gaps": ["团队范围待确认"]},
                    ],
                    "evidence": ["一年引入500家商家"],
                    "gaps": ["策略范围待确认", "团队范围待确认"],
                    "risks": [],
                    "follow_up_questions": [],
                    "summary": "商家拓展结果明确，策略和协作范围待确认。",
                }
            }
        )

        evaluation = evaluate_redacted_candidate(evaluation_llm, candidate, rubric)

        self.assertEqual(evaluation["total_score"], 40.0)
        self.assertNotIn("decision", evaluation)
        self.assertNotIn("hard_gate", evaluation)

    def test_resume_redaction_does_not_treat_an_opaque_candidate_id_as_a_phone_number(self) -> None:
        candidate_id = "candidate-9b14370279869c9a"

        sanitized = redact_resume_for_llm(
            {"work_experience": [{"responsibility": "联系 13800138000 跟进商家"}]},
            candidate_id,
        )

        self.assertEqual(sanitized["candidate_id"], candidate_id)
        self.assertIn("[PHONE_REDACTED]", sanitized["work_experience"][0]["responsibility"])

    def test_real_adapter_uses_one_mocked_json_request_without_leaking_key_in_payload(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, *, timeout: int) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": json.dumps(rubric_result())}}]})

        llm = OpenAICompatibleJsonLlm(
            base_url="https://llm.example.test",
            api_key="secret-key",
            model="test-model",
            opener=opener,
        )
        result = llm.complete_json(operation="rubric", system_prompt="prompt", payload={"jd_text": JD})

        request = captured["request"]
        body = request.data.decode("utf-8")  # type: ignore[attr-defined]
        self.assertEqual(result["job_title"], "平台招商负责人")
        self.assertNotIn("secret-key", body)
        self.assertEqual(request.get_header("User-agent"), "boss-hire-hr/1.0")  # type: ignore[attr-defined]
        self.assertEqual(captured["timeout"], 180)

    def test_real_adapter_rejects_top_level_search_array(self) -> None:
        def opener(_request: object, *, timeout: int) -> FakeResponse:
            self.assertEqual(timeout, 180)
            return FakeResponse({"choices": [{"message": {"content": json.dumps(["平台招商", "商家增长"])}}]})

        llm = OpenAICompatibleJsonLlm(
            base_url="https://llm.example.test",
            api_key="secret-key",
            model="test-model",
            opener=opener,
        )

        with self.assertRaisesRegex(RuntimeError, "JSON 对象"):
            llm.complete_json(operation="search_plan", system_prompt="prompt", payload={"jd_text": JD})

    def test_real_adapter_preserves_only_public_http_error_details(self) -> None:
        headers = Message()
        headers["X-Request-ID"] = "request-123"
        headers["Server"] = "gateway"
        headers["Set-Cookie"] = "session-secret"

        def opener(request: object, *, timeout: int) -> FakeResponse:
            raise HTTPError(
                url=request.full_url,  # type: ignore[attr-defined]
                code=502,
                msg="Bad Gateway",
                hdrs=headers,
                fp=BytesIO(
                    json.dumps(
                        {
                            "error": {
                                "message": "unknown provider for model test-model",
                                "type": "server_error",
                                "code": "internal_server_error",
                            }
                        }
                    ).encode("utf-8")
                ),
            )

        llm = OpenAICompatibleJsonLlm(
            base_url="https://llm.example.test",
            api_key="secret-key",
            model="test-model",
            opener=opener,
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 502") as raised:
            llm.complete_json(operation="rubric", system_prompt="prompt", payload={"jd_text": JD})

        self.assertEqual(
            raised.exception.public_details,  # type: ignore[attr-defined]
            {
                "status": 502,
                "error": {
                    "message": "unknown provider for model test-model",
                    "type": "server_error",
                    "code": "internal_server_error",
                },
                "headers": {"server": "gateway", "x-request-id": "request-123"},
            },
        )
        self.assertIn("unknown provider", str(raised.exception))
        self.assertNotIn("session-secret", str(raised.exception))

    def test_llm_cannot_bypass_program_scoring(self) -> None:
        rubric = generate_continuous_rubric(FakeLlm({"rubric": rubric_result()}), JD)
        candidate = redact_resume_for_llm({}, "candidate-local-1")
        llm = FakeLlm(
            {
                "candidate_evaluation": {
                    "candidate_id": "candidate-local-1",
                    "total_score": 100,
                    "decision": "优先跟进",
                    "dimension_assessments": [],
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "程序计算字段"):
            evaluate_redacted_candidate(llm, candidate, rubric)

    def test_job_rubric_and_search_artifacts_are_private_versioned_and_immutable(self) -> None:
        llm = FakeLlm({"rubric": rubric_result(), "search_plan": search_semantics_result()})
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "job"
            artifacts = prepare_single_job_llm_artifacts(
                job=self.open_job(),
                llm=llm,
                output_dir=output,
                search_config=SearchPlanConfig(total_query_budget=8),
            )

            self.assertEqual(artifacts["manifest"]["job_id"], "job-1")
            self.assertEqual(artifacts["manifest"]["llm_model"], "fake-model")
            self.assertEqual(artifacts["search_plan"]["schema_version"], 3)
            self.assertEqual(artifacts["manifest"]["search_generator_version"], SEARCH_GENERATOR_VERSION)
            self.assertEqual(artifacts["manifest"]["search_plan_schema_version"], 3)
            self.assertEqual(
                artifacts["manifest"]["search_plan_hash"],
                content_hash(artifacts["search_plan"]),
            )
            for name in ("job.json", "rubric.json", "search_plan.json", "search_generation_diagnostics.json", "manifest.json"):
                self.assertEqual((output / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"job.json", "rubric.json", "search_plan.json", "search_generation_diagnostics.json", "manifest.json"},
            )
            prepare_single_job_llm_artifacts(
                job=self.open_job(),
                llm=FakeLlm({"rubric": rubric_result(), "search_plan": search_semantics_result()}),
                output_dir=output,
                search_config=SearchPlanConfig(total_query_budget=8),
            )
            changed = search_semantics_result()
            changed["role_terms"] = ["商家增长"]
            with self.assertRaisesRegex(ValueError, "不可覆盖"):
                prepare_single_job_llm_artifacts(
                    job=self.open_job(),
                    llm=FakeLlm({"rubric": rubric_result(), "search_plan": changed}),
                    output_dir=output,
                    search_config=SearchPlanConfig(total_query_budget=8),
                )

    def test_failed_zero_route_generation_persists_only_safe_diagnostics(self) -> None:
        result = search_semantics_result()
        result["anchor_terms"] = []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "job"
            with self.assertRaisesRegex(SearchPlanGenerationError, "诊断已保存"):
                prepare_single_job_llm_artifacts(
                    job=self.open_job(),
                    llm=FakeLlm({"rubric": rubric_result(), "search_plan": result}),
                    output_dir=output,
                    search_config=SearchPlanConfig(total_query_budget=8),
                )

            diagnostics = json.loads((output / "search_generation_diagnostics.json").read_text())
            self.assertEqual(diagnostics["accepted_route_count"], 0)
            self.assertEqual({path.name for path in output.iterdir()}, {"search_generation_diagnostics.json"})


if __name__ == "__main__":
    unittest.main()
