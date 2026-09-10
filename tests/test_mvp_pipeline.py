from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from boss_hire.mvp_pipeline import (
    apply_fast_filters,
    build_evaluation_entries,
    collect_recommendation_cards,
    collect_search_cards,
    build_card_evaluation_input,
    create_confirmation_batch,
    normalize_search_card,
    parse_years_lower_bound,
    prepare_candidates,
    render_confirmation_batch,
    write_confirmation_batch,
)
from boss_hire.state_store import StateStore, card_hash, jd_hash


FIXTURE = Path(__file__).parent / "fixtures/mvp_search.json"
RUBRIC = {
    "hard_gates": [
        {
            "id": "experience",
            "fast_filter": {"type": "min_years", "value": 8, "reject_only_when_known": True},
        },
        {
            "id": "degree",
            "fast_filter": {"type": "min_degree", "value": "bachelor", "reject_only_when_known": True},
        },
    ]
}


class FakeClient:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.pages = fixture["pages"]
        self.resumes = fixture["resumes"]
        self.search_calls: list[int] = []
        self.view_calls: list[str] = []

    def search_geeks(self, _query: str, *, page: int, job_id: str) -> dict[str, Any]:
        self.search_calls.append(page)
        index = page - 1
        return self.pages[index] if index < len(self.pages) else {"code": 0, "zpData": {"geeks": []}}

    def view_geek(self, geek_id: str, _job_id: str, *, security_id: str) -> dict[str, Any]:
        self.view_calls.append(geek_id)
        return self.resumes[geek_id]


def parse_resume(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["parsed"]


class MvpPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.temp_dir = tempfile.TemporaryDirectory(prefix="boss_hire_pipeline_")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def state(self) -> StateStore:
        state = StateStore(self.root / "state.json")
        state.record_job("job-open", jd_hash("JD"))
        return state

    def test_normalize_card_uses_encrypt_geek_id_and_excludes_protected_fields(self) -> None:
        item = self.fixture["pages"][0]["zpData"]["geeks"][0]
        card = normalize_search_card(item, page=1, rank=1, job_id="job-open")
        self.assertEqual(card.encrypt_geek_id, "geek-a")
        self.assertEqual(card.degree, "本科")
        self.assertEqual(card.current_position, "汽配公司·招商负责人")
        self.assertNotIn("age", card.to_dict())
        self.assertNotIn("gender", card.to_dict())

    def test_collect_dedupes_by_stable_id_and_reports_invalid_rows(self) -> None:
        client = FakeClient(self.fixture)
        cards, stats = collect_search_cards(client, query="招商", job_id="job-open", max_pages=3, limit=100)
        self.assertEqual([card.encrypt_geek_id for card in cards], ["geek-a", "geek-b", "geek-c"])
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(stats["pages"], 3)

    def test_normalize_recommendation_card_reuses_search_card_model(self) -> None:
        item = {
            "encryptGeekId": "rec-a",
            "geekCard": {
                "securityId": "sec-rec-a",
                "encryptJobId": "job-open",
                "geekName": "候选人甲",
                "geekWorkYear": "10年以上",
                "geekDegree": "本科",
                "expectLocationName": "上海",
                "expectPositionName": "平台招商总监",
                "geekDesc": {"content": "汽配平台招商经验"},
                "geekWorks": [
                    {"company": "汽配公司", "positionName": "招商负责人"},
                ],
            },
            "geekLastWork": {"company": "汽配公司", "positionName": "招商负责人"},
            "showWorks": [
                {
                    "company": "汽配公司",
                    "positionName": "招商负责人",
                    "workTime": "10年",
                    "responsibility": "从0到1建立汽配核心商家池",
                    "workPerformance": "引入20家重点商家",
                    "workEmphasisList": ["汽配招商", "商家管理"],
                }
            ],
            "showEdus": [{"degreeName": "本科", "major": "汽车服务工程", "timeSlot": "2008-2012"}],
        }
        item["geekCard"].update({"matches": ["汽配招商"], "viewed": True, "oldDetailed": False})

        card = normalize_search_card(item, page=2, rank=3, job_id="job-open")

        self.assertEqual(card.encrypt_geek_id, "rec-a")
        self.assertEqual(card.security_id, "sec-rec-a")
        self.assertEqual(card.name, "候选人甲")
        self.assertEqual(card.work_year, "10年以上")
        self.assertEqual(card.degree, "本科")
        self.assertEqual(card.city, "上海")
        self.assertEqual(card.current_position, "汽配公司 · 招商负责人")
        self.assertEqual(card.expect_position, "平台招商总监")
        self.assertEqual(card.advantage, "汽配平台招商经验")
        self.assertEqual(card.works, ("汽配公司 · 招商负责人",))
        self.assertEqual(card.work_details[0]["responsibility"], "从0到1建立汽配核心商家池")
        self.assertEqual(card.work_details[0]["performance"], "引入20家重点商家")
        self.assertEqual(card.education[0]["major"], "汽车服务工程")
        self.assertEqual(card.matches, ("汽配招商",))
        self.assertTrue(card.viewed)
        self.assertFalse(card.old_detailed)
        evaluation_input = build_card_evaluation_input(card)
        self.assertEqual(evaluation_input["work_experience"][0]["performance"], "引入20家重点商家")
        self.assertEqual(evaluation_input["matches"], ["汽配招商"])
        changed_view = json.loads(json.dumps(item, ensure_ascii=False))
        changed_view["geekCard"]["securityId"] = "fresh-security-id"
        changed_view["geekCard"]["viewed"] = False
        changed_view["geekCard"]["oldDetailed"] = True
        second = normalize_search_card(changed_view, page=8, rank=9, job_id="job-open")
        self.assertEqual(card_hash(card.hash_payload()), card_hash(second.hash_payload()))

    def test_collect_recommendations_paginates_dedupes_and_honors_limit(self) -> None:
        pages = {
            1: {
                "code": 0,
                "zpData": {
                    "geekList": [
                        {"encryptGeekId": "rec-a", "geekCard": {"securityId": "sec-a"}},
                        {"encryptGeekId": "rec-b", "geekCard": {"securityId": "sec-b"}},
                        "invalid",
                    ],
                    "hasMore": True,
                },
            },
            2: {
                "code": 0,
                "zpData": {
                    "geekList": [
                        {"encryptGeekId": "rec-b", "geekCard": {"securityId": "sec-b2"}},
                        {"encryptGeekId": "rec-c", "geekCard": {"securityId": "sec-c"}},
                    ],
                    "hasMore": False,
                },
            },
        }
        calls: list[int] = []

        def fetch_page(page: int) -> dict[str, Any]:
            calls.append(page)
            return pages[page]

        cards, stats = collect_recommendation_cards(fetch_page, job_id="job-open", max_pages=7, limit=3)

        self.assertEqual(calls, [1, 2])
        self.assertEqual([card.encrypt_geek_id for card in cards], ["rec-a", "rec-b", "rec-c"])
        self.assertTrue(all(card.encrypt_job_id == "job-open" for card in cards))
        self.assertEqual(stats, {"raw": 5, "duplicates": 1, "invalid": 1, "pages": 2})

    def test_collect_recommendations_stops_at_one_hundred(self) -> None:
        calls: list[int] = []

        def fetch_page(page: int) -> dict[str, Any]:
            calls.append(page)
            start = (page - 1) * 60
            return {
                "code": 0,
                "zpData": {
                    "geekList": [
                        {"encryptGeekId": f"rec-{index}", "geekCard": {"securityId": f"sec-{index}"}}
                        for index in range(start, start + 60)
                    ],
                    "hasMore": True,
                },
            }

        cards, stats = collect_recommendation_cards(fetch_page, job_id="job-open", max_pages=7, limit=100)

        self.assertEqual(len(cards), 100)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(stats["raw"], 100)

    def test_fast_filter_rejects_only_known_failures(self) -> None:
        self.assertEqual(parse_years_lower_bound("10年以上"), 10)
        self.assertEqual(parse_years_lower_bound("5-10年"), 5)
        self.assertIsNone(parse_years_lower_bound("经验未知"))
        rejected = apply_fast_filters(work_year="5年", degree="本科", rubric=RUBRIC)
        uncertain = apply_fast_filters(work_year="经验未知", degree="", rubric=RUBRIC)
        passed = apply_fast_filters(work_year="10年以上", degree="本科", rubric=RUBRIC)
        self.assertEqual(rejected["status"], "fail")
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertEqual(passed["status"], "pass")

    def test_first_run_fetches_only_non_rejected_resumes(self) -> None:
        client = FakeClient(self.fixture)
        result = prepare_candidates(
            client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=self.state(),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        self.assertEqual(client.view_calls, ["geek-a", "geek-c"])
        self.assertEqual(result["counts"]["card_rejected"], 1)
        self.assertEqual(result["counts"]["ready"], 2)
        self.assertEqual(result["counts"]["detail_fetched"], 2)

    def test_identical_second_run_does_not_fetch_resumes(self) -> None:
        state = self.state()
        first_client = FakeClient(self.fixture)
        prepare_candidates(
            first_client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=state,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        second_client = FakeClient(self.fixture)
        reloaded = StateStore(self.root / "state.json")
        result = prepare_candidates(
            second_client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=reloaded,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        self.assertEqual(second_client.view_calls, [])
        self.assertEqual(result["counts"]["reused"], 3)
        self.assertEqual(result["counts"]["new"], 0)
        self.assertEqual(
            [row["status"] for row in result["candidates"]],
            ["ready", "card_rejected", "ready"],
        )

    def test_resume_rejection_stays_excluded_from_evaluation_on_reuse(self) -> None:
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        fixture["resumes"]["geek-a"]["parsed"]["basic"]["work_years"] = "5年"
        state = self.state()
        prepare_candidates(
            FakeClient(fixture),
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=state,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        second = prepare_candidates(
            FakeClient(fixture),
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=StateStore(self.root / "state.json"),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )

        entries, stats = build_evaluation_entries(second, StateStore(self.root / "state.json"))
        reversed_entries, _ = build_evaluation_entries(
            {**second, "candidates": list(reversed(second["candidates"]))},
            StateStore(self.root / "state.json"),
        )

        self.assertEqual([row["candidate_id"] for row in entries], ["geek-c"])
        self.assertEqual(entries[0]["experiment_id"], reversed_entries[0]["experiment_id"])
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["excluded"], 2)

    def test_detail_error_is_retried_instead_of_cached_as_processed(self) -> None:
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        fixture["resumes"]["geek-a"] = {"code": 1}
        state = self.state()
        first = prepare_candidates(
            FakeClient(fixture),
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=state,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        repaired = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        client = FakeClient(repaired)
        second = prepare_candidates(
            client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=StateStore(self.root / "state.json"),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )

        self.assertEqual(first["counts"]["errors"], 1)
        self.assertIn("geek-a", client.view_calls)
        self.assertEqual(second["counts"]["new"], 1)

    def test_resume_parser_error_is_isolated_to_one_candidate(self) -> None:
        client = FakeClient(self.fixture)

        def parse_with_one_failure(raw: dict[str, Any]) -> dict[str, Any]:
            if raw is self.fixture["resumes"]["geek-a"]:
                raise AttributeError("missing geekDetailInfo")
            return parse_resume(raw)

        result = prepare_candidates(
            client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=self.state(),
            output_dir=self.root,
            parse_resume=parse_with_one_failure,
            max_pages=3,
        )

        self.assertEqual(client.view_calls, ["geek-a", "geek-c"])
        self.assertEqual(result["counts"]["errors"], 1)
        self.assertEqual(result["counts"]["ready"], 1)
        failed = next(row for row in result["candidates"] if row["candidate"]["encrypt_geek_id"] == "geek-a")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["error"], "parse_resume_failed")

    def test_orphaned_resume_file_is_reused_after_interrupted_run(self) -> None:
        orphan = self.root / "resumes/geek-a/resume.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_text(
            json.dumps(self.fixture["resumes"]["geek-a"]["parsed"], ensure_ascii=False),
            encoding="utf-8",
        )
        client = FakeClient(self.fixture)

        result = prepare_candidates(
            client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=self.state(),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )

        self.assertEqual(client.view_calls, ["geek-c"])
        self.assertEqual(result["counts"]["resume_reused"], 1)

    def test_explicitly_unavailable_resume_is_not_retried_until_card_changes(self) -> None:
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        fixture["resumes"]["geek-a"] = {"code": 0, "zpData": {"geekDetailInfo": None}}
        state = self.state()
        first_client = FakeClient(fixture)
        first = prepare_candidates(
            first_client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=state,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        second_client = FakeClient(fixture)
        second = prepare_candidates(
            second_client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=StateStore(self.root / "state.json"),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )

        failed = next(row for row in first["candidates"] if row["candidate"]["encrypt_geek_id"] == "geek-a")
        self.assertEqual(failed["status"], "detail_unavailable")
        self.assertEqual(first_client.view_calls, ["geek-a", "geek-c"])
        self.assertEqual(second_client.view_calls, [])
        self.assertEqual(second["counts"]["reused"], 3)

    def test_confirmation_batch_shows_only_actionable_summary(self) -> None:
        state = self.state()
        state.record_resume("geek-a", "resume-a", str(self.root / "a.json"))
        state.record_resume("geek-c", "resume-c", str(self.root / "c.json"))
        state.set_favorite_status("geek-c", "favorited")
        prepared = {
            "candidates": [
                {
                    "status": "ready",
                    "candidate": {
                        "encrypt_geek_id": "geek-a",
                        "security_id": "sec-a",
                        "name": "候选人A",
                        "source_page": 1,
                        "source_rank": 2,
                    },
                },
                {
                    "status": "ready",
                    "candidate": {
                        "encrypt_geek_id": "geek-c",
                        "security_id": "sec-c",
                        "name": "候选人C",
                        "source_page": 1,
                        "source_rank": 3,
                    },
                },
            ]
        }
        evaluations = [
            {
                "experiment_id": "M-a",
                "candidate_id": "geek-a",
                "input_fingerprint": "fp-a",
                "evaluation": {
                    "hard_gate": "pass",
                    "total_score": 91,
                    "decision": "优先跟进",
                    "summary": "汽配平台招商与从零搭建经历直接匹配。",
                    "missing_requirements": ["商家资源规模待确认"],
                    "evidence": ["不应展示的详细证据"],
                },
            },
            {
                "experiment_id": "M-c",
                "candidate_id": "geek-c",
                "input_fingerprint": "fp-c",
                "evaluation": {
                    "hard_gate": "pass",
                    "total_score": 88,
                    "decision": "优先跟进",
                    "summary": "已收藏候选人。",
                    "missing_requirements": [],
                },
            },
        ]

        batch = create_confirmation_batch(
            job_id="job-open",
            job_name="跨境汽配平台招商总监",
            prepared=prepared,
            evaluations=list(reversed(evaluations)),
            state=state,
            created_at="2026-08-25T18:00:00+08:00",
        )
        same = create_confirmation_batch(
            job_id="job-open",
            job_name="跨境汽配平台招商总监",
            prepared=prepared,
            evaluations=evaluations,
            state=state,
            created_at="2026-08-25T18:01:00+08:00",
        )
        markdown = render_confirmation_batch(batch)
        paths = write_confirmation_batch(batch, self.root / "batch")

        self.assertEqual(batch["batch_id"], same["batch_id"])
        self.assertEqual(batch["candidate_count"], 1)
        self.assertEqual(batch["candidates"][0]["encrypt_geek_id"], "geek-a")
        self.assertEqual(batch["candidates"][0]["security_id"], "sec-a")
        self.assertEqual(batch["stats"]["already_favorited"], 1)
        self.assertIn("候选人A", markdown)
        self.assertIn("商家资源规模待确认", markdown)
        self.assertNotIn("不应展示的详细证据", markdown)
        self.assertTrue(paths["json"].is_file())
        self.assertTrue(paths["markdown"].is_file())

    def test_card_change_refreshes_only_that_resume(self) -> None:
        state = self.state()
        prepare_candidates(
            FakeClient(self.fixture),
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=state,
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        changed_fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False))
        changed_fixture["pages"][0]["zpData"]["geeks"][0]["geekCard"]["current"]["name"] = "汽配公司·招商总监"
        client = FakeClient(changed_fixture)
        result = prepare_candidates(
            client,
            job_id="job-open",
            query="招商",
            rubric=RUBRIC,
            state=StateStore(self.root / "state.json"),
            output_dir=self.root,
            parse_resume=parse_resume,
            max_pages=3,
        )
        self.assertEqual(client.view_calls, ["geek-a"])
        self.assertEqual(result["counts"]["card_changed"], 1)
        self.assertEqual(result["counts"]["reused"], 2)


if __name__ == "__main__":
    unittest.main()
