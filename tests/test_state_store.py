from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boss_hire.state_store import (
    StateStore,
    card_hash,
    evaluation_fingerprint,
    jd_hash,
    resume_hash,
    stable_candidate_id,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="boss_hire_state_")
        self.path = Path(self.temp_dir.name) / "state.json"
        self.times = iter(
            [
                "2026-08-25T10:00:00+08:00",
                "2026-08-25T10:01:00+08:00",
                "2026-08-25T10:02:00+08:00",
                "2026-08-25T10:03:00+08:00",
                "2026-08-25T10:04:00+08:00",
                "2026-08-25T10:05:00+08:00",
                "2026-08-25T10:06:00+08:00",
                "2026-08-25T10:07:00+08:00",
                "2026-08-25T10:08:00+08:00",
                "2026-08-25T10:09:00+08:00",
            ]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def store(self) -> StateStore:
        return StateStore(self.path, clock=lambda: next(self.times))

    def test_stable_candidate_id_requires_encrypt_geek_id(self) -> None:
        self.assertEqual(
            stable_candidate_id({"geekCard": {"encryptGeekId": "geek-1"}}),
            "geek-1",
        )
        with self.assertRaisesRegex(ValueError, "encryptGeekId"):
            stable_candidate_id({"securityId": "temporary"})

    def test_card_hash_ignores_transient_request_and_position_fields(self) -> None:
        first = {
            "encryptGeekId": "geek-1",
            "securityId": "security-a",
            "sourcePage": 1,
            "sourceRank": 1,
            "workExp": "汽配平台招商",
        }
        second = {
            **first,
            "securityId": "security-b",
            "sourcePage": 3,
            "sourceRank": 12,
        }
        self.assertEqual(card_hash(first), card_hash(second))
        second["workExp"] = "跨境平台招商"
        self.assertNotEqual(card_hash(first), card_hash(second))

    def test_same_inputs_reuse_card_resume_and_evaluation(self) -> None:
        store = self.store()
        jd_digest = jd_hash("岗位要求\n8年以上经验")
        card_digest = card_hash({"encryptGeekId": "geek-1", "workExp": "汽配"})
        resume_digest = resume_hash({"work_experience": [{"company": "A"}]})
        fingerprint = evaluation_fingerprint(
            jd_digest=jd_digest,
            rubric={"version": "v1"},
            prompt="prompt-v1",
            model="model-a",
            resume_digest=resume_digest,
        )

        self.assertEqual(store.record_job("job-1", jd_digest), "new")
        self.assertEqual(store.classify_card("job-1", "geek-1", card_digest), "new")
        store.record_card("job-1", "geek-1", card_digest)
        self.assertEqual(store.record_resume("geek-1", resume_digest, "resume.json"), "new")
        store.record_evaluation("job-1", "geek-1", fingerprint=fingerprint, path="eval.json")
        store.save()

        reloaded = self.store()
        self.assertEqual(reloaded.record_job("job-1", jd_digest), "unchanged")
        self.assertEqual(reloaded.classify_card("job-1", "geek-1", card_digest), "unchanged")
        self.assertEqual(reloaded.record_resume("geek-1", resume_digest, "resume.json"), "unchanged")
        self.assertTrue(reloaded.evaluation_reusable("job-1", "geek-1", fingerprint))

    def test_changes_invalidate_only_the_relevant_layer(self) -> None:
        store = self.store()
        first_jd = jd_hash("JD v1")
        second_jd = jd_hash("JD v2")
        first_card = card_hash({"encryptGeekId": "geek-1", "workExp": "A"})
        second_card = card_hash({"encryptGeekId": "geek-1", "workExp": "B"})
        first_resume = resume_hash({"work": "A"})
        second_resume = resume_hash({"work": "B"})

        store.record_job("job-1", first_jd)
        store.record_card("job-1", "geek-1", first_card)
        store.record_resume("geek-1", first_resume, "resume.json")
        old_fingerprint = evaluation_fingerprint(
            jd_digest=first_jd,
            rubric={"version": "v1"},
            prompt="p",
            model="m",
            resume_digest=first_resume,
        )
        store.record_evaluation("job-1", "geek-1", fingerprint=old_fingerprint, path="eval.json")

        self.assertEqual(store.classify_card("job-1", "geek-1", second_card), "card_changed")
        self.assertEqual(store.record_resume("geek-1", second_resume, "resume.json"), "resume_changed")
        self.assertEqual(store.record_job("job-1", second_jd), "jd_changed")
        new_fingerprint = evaluation_fingerprint(
            jd_digest=second_jd,
            rubric={"version": "v1"},
            prompt="p",
            model="m",
            resume_digest=second_resume,
        )
        self.assertFalse(store.evaluation_reusable("job-1", "geek-1", new_fingerprint))

    def test_resume_is_global_but_evaluation_is_job_specific(self) -> None:
        store = self.store()
        resume_digest = resume_hash({"work": "same resume"})
        store.record_job("job-a", jd_hash("JD A"))
        store.record_job("job-b", jd_hash("JD B"))
        store.record_card("job-a", "geek-1", card_hash({"encryptGeekId": "geek-1", "job": "A"}))
        store.record_card("job-b", "geek-1", card_hash({"encryptGeekId": "geek-1", "job": "B"}))

        self.assertEqual(store.record_resume("geek-1", resume_digest, "resume.json"), "new")
        self.assertEqual(store.record_resume("geek-1", resume_digest, "resume.json"), "unchanged")
        store.record_evaluation("job-a", "geek-1", fingerprint="fp-a", path="a.json")
        self.assertTrue(store.evaluation_reusable("job-a", "geek-1", "fp-a"))
        self.assertFalse(store.evaluation_reusable("job-b", "geek-1", "fp-a"))

    def test_save_is_atomic_and_invalid_schema_fails_closed(self) -> None:
        store = self.store()
        store.record_job("job-1", jd_hash("JD"))
        store.save()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

        self.path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unsupported state schema"):
            self.store()

    def test_source_cards_are_classified_independently_and_keep_provenance(self) -> None:
        store = self.store()
        store.record_job("job-1", jd_hash("JD"))
        search_digest = card_hash({"candidate": "shared", "source": "search"})
        recommendation_digest = card_hash({"candidate": "shared", "source": "recommendation"})

        self.assertEqual(store.classify_source_card("job-1", "shared", "search", search_digest), "new")
        store.record_source_card(
            "job-1",
            "shared",
            "search",
            search_digest,
            screening_status="detail_candidate",
            metadata={"queries": ["平台招商总监"]},
        )
        self.assertEqual(
            store.classify_source_card("job-1", "shared", "recommendation", recommendation_digest),
            "new",
        )
        store.record_source_card(
            "job-1",
            "shared",
            "recommendation",
            recommendation_digest,
            screening_status="detail_candidate",
        )

        self.assertEqual(store.classify_source_card("job-1", "shared", "search", search_digest), "unchanged")
        self.assertEqual(
            store.classify_source_card("job-1", "shared", "recommendation", recommendation_digest),
            "unchanged",
        )
        self.assertEqual(store.candidate_sources("job-1", "shared"), ["search", "recommendation"])
        self.assertEqual(
            store.source_card_record("job-1", "shared", "search")["queries"],
            ["平台招商总监"],
        )

    def test_boss_stop_checkpoint_survives_reload_without_credentials(self) -> None:
        store = self.store()
        store.record_boss_stop(
            run_id="run-risk",
            reason="code_36",
            checkpoint_path="data/local/supply_mvp/state.json",
            request_count=17,
        )
        store.save()

        reloaded = self.store()
        stop = reloaded.boss_stop("run-risk")
        self.assertEqual(stop["reason"], "code_36")
        self.assertEqual(stop["request_count"], 17)
        self.assertNotIn("cookies", json.dumps(stop))
        self.assertEqual(reloaded.snapshot()["boss_access"]["last_stop_run_id"], "run-risk")


if __name__ == "__main__":
    unittest.main()
