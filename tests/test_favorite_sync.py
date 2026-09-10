from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.safe_recruiter_client import BossRiskStop
from boss_hire.favorite_sync import (
    FavoriteSyncContractError,
    collect_favorite_pages,
    parse_favorite_list_page,
    persist_favorite_sync_result,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "boss_favorite_list"


def fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"fixture {name} must contain an object")
    return value


class FavoriteListPageContractTests(unittest.TestCase):
    def test_parses_confirmed_page_contract_without_time_semantics(self) -> None:
        page = parse_favorite_list_page(fixture("page_1_has_more.json"))

        self.assertEqual(len(page.candidate_ids), 10)
        self.assertEqual(page.candidate_ids[:2], ("new-01", "new-02"))
        self.assertTrue(page.has_more)
        self.assertFalse(hasattr(page, "favorite_time"))

    def test_empty_last_page_is_valid(self) -> None:
        page = parse_favorite_list_page(fixture("empty_end.json"))

        self.assertEqual(page.candidate_ids, ())
        self.assertFalse(page.has_more)

    def test_missing_card_list_fails_closed(self) -> None:
        with self.assertRaisesRegex(FavoriteSyncContractError, "cardList"):
            parse_favorite_list_page(fixture("malformed_missing_card_list.json"))

    def test_missing_stable_candidate_id_fails_closed(self) -> None:
        with self.assertRaisesRegex(FavoriteSyncContractError, "encryptGeekId"):
            parse_favorite_list_page(
                {"code": 0, "zpData": {"cardList": [{"anonymous": True}], "hasMore": False}}
            )

    def test_more_than_ten_cards_fails_closed(self) -> None:
        cards = [{"encryptGeekId": f"geek-{index}"} for index in range(11)]

        with self.assertRaisesRegex(FavoriteSyncContractError, "10"):
            parse_favorite_list_page(
                {"code": 0, "zpData": {"cardList": cards, "hasMore": False}}
            )

    def test_nonzero_business_code_fails_closed(self) -> None:
        with self.assertRaisesRegex(FavoriteSyncContractError, "121"):
            parse_favorite_list_page({"code": 121, "message": "invalid"})


class FavoritePageCollectionTests(unittest.TestCase):
    def test_incremental_anchor_can_span_pages(self) -> None:
        payloads = [fixture("page_1_has_more.json"), fixture("page_2_end.json")]
        calls: list[int] = []

        def fetch(page: int) -> dict[str, object]:
            calls.append(page)
            return payloads[page - 1]

        result = collect_favorite_pages(
            fetch_page=fetch,
            mode="incremental",
            anchor_group=tuple(f"anchor-{index:02d}" for index in range(1, 11)),
            max_pages=40,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "anchor_reached")
        self.assertTrue(result.complete)
        self.assertEqual(result.pages_read, 2)
        self.assertEqual(result.first_page_ids[:2], ("new-01", "new-02"))
        self.assertIn("older-02", result.observed_candidate_ids)

    def test_initialization_requires_list_end(self) -> None:
        payloads = [fixture("page_1_has_more.json"), fixture("page_2_end.json")]

        result = collect_favorite_pages(
            fetch_page=lambda page: payloads[page - 1],
            mode="initialize",
            anchor_group=(),
            max_pages=40,
        )

        self.assertEqual(result.status, "end_reached")
        self.assertTrue(result.complete)
        self.assertEqual(result.pages_read, 2)

    def test_known_registry_id_does_not_stop_incremental_before_anchor_or_end(self) -> None:
        payloads = [
            {
                "code": 0,
                "zpData": {
                    "cardList": [{"encryptGeekId": "already-registered"}],
                    "hasMore": True,
                },
            },
            fixture("empty_end.json"),
        ]
        calls: list[int] = []

        result = collect_favorite_pages(
            fetch_page=lambda page: calls.append(page) or payloads[page - 1],
            mode="incremental",
            anchor_group=("missing-anchor",),
            max_pages=40,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "end_reached")
        self.assertTrue(result.complete)

    def test_page_forty_with_has_more_is_incomplete_and_never_fetches_page_41(self) -> None:
        calls: list[int] = []

        def fetch(page: int) -> dict[str, object]:
            calls.append(page)
            if page == 40:
                return fixture("page_40_has_more.json")
            return {
                "code": 0,
                "zpData": {
                    "cardList": [
                        {"encryptGeekId": f"page{page:02d}-{index:02d}"}
                        for index in range(1, 11)
                    ],
                    "hasMore": True,
                },
            }

        result = collect_favorite_pages(
            fetch_page=fetch,
            mode="initialize",
            anchor_group=(),
            max_pages=40,
        )

        self.assertEqual(calls, list(range(1, 41)))
        self.assertEqual(result.status, "sync_incomplete")
        self.assertFalse(result.complete)
        self.assertEqual(result.pages_read, 40)

    def test_duplicate_nonterminal_page_fails_without_retry(self) -> None:
        payload = fixture("page_1_has_more.json")
        calls: list[int] = []

        def fetch(page: int) -> dict[str, object]:
            calls.append(page)
            return payload

        result = collect_favorite_pages(
            fetch_page=fetch,
            mode="initialize",
            anchor_group=(),
            max_pages=40,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.complete)
        self.assertIn("duplicate", result.error or "")

    def test_risk_on_second_page_stops_and_preserves_first_page_observation(self) -> None:
        calls: list[int] = []

        def fetch(page: int) -> dict[str, object]:
            calls.append(page)
            if page == 1:
                return fixture("page_1_has_more.json")
            raise BossRiskStop("risk", outcome="risk_stop", transport_attempted=True)

        result = collect_favorite_pages(
            fetch_page=fetch,
            mode="initialize",
            anchor_group=(),
            max_pages=40,
        )

        self.assertEqual(calls, [1, 2])
        self.assertEqual(result.status, "risk_stopped")
        self.assertFalse(result.complete)
        self.assertEqual(result.pages_read, 1)
        self.assertEqual(len(result.observed_candidate_ids), 10)

    def test_invalid_mode_and_page_budget_fail_before_fetch(self) -> None:
        calls: list[int] = []

        for mode, max_pages in (("full", 40), ("incremental", 0), ("incremental", 41)):
            with self.subTest(mode=mode, max_pages=max_pages), self.assertRaises(ValueError):
                collect_favorite_pages(
                    fetch_page=lambda page: calls.append(page) or fixture("empty_end.json"),
                    mode=mode,
                    anchor_group=(),
                    max_pages=max_pages,
                )

        self.assertEqual(calls, [])

    def test_incomplete_result_preserves_observations_without_advancing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = FavoriteRegistry(Path(tmp) / "favorites", account_key="account-a")
            original_checkpoint = registry.save_complete_checkpoint(
                anchor_group=["old-anchor"],
                receipt_id="old-receipt",
                sync_status="end_reached",
                completed_at="2026-09-03T10:00:00+08:00",
            )
            result = collect_favorite_pages(
                fetch_page=lambda _page: fixture("page_40_has_more.json"),
                mode="initialize",
                anchor_group=(),
                max_pages=1,
            )

            persisted = persist_favorite_sync_result(
                registry=registry,
                result=result,
                receipt_id="incomplete-receipt",
                observed_at="2026-09-04T10:00:00+08:00",
            )

            self.assertFalse(persisted.checkpoint_advanced)
            self.assertEqual(persisted.new_candidate_count, 10)
            self.assertEqual(registry.checkpoint(), original_checkpoint)
            self.assertTrue(registry.contains("page40-01"))

    def test_complete_result_records_candidates_and_advances_first_page_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = FavoriteRegistry(Path(tmp) / "favorites", account_key="account-a")
            payloads = [fixture("page_1_has_more.json"), fixture("page_2_end.json")]
            result = collect_favorite_pages(
                fetch_page=lambda page: payloads[page - 1],
                mode="initialize",
                anchor_group=(),
                max_pages=40,
            )

            persisted = persist_favorite_sync_result(
                registry=registry,
                result=result,
                receipt_id="complete-receipt",
                observed_at="2026-09-04T10:00:00+08:00",
            )

            self.assertTrue(persisted.checkpoint_advanced)
            self.assertEqual(persisted.observed_count, 14)
            self.assertEqual(registry.checkpoint()["anchor_group"], list(result.first_page_ids))
            self.assertEqual(registry.checkpoint()["sync_status"], "end_reached")
            self.assertTrue(registry.contains("older-02"))


if __name__ == "__main__":
    unittest.main()
