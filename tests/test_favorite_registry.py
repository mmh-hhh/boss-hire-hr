from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from boss_hire.favorite_registry import FavoriteRegistry


class FavoriteRegistryTests(unittest.TestCase):
    def make_registry(self, root: Path, account_key: str = "0123456789abcdef") -> FavoriteRegistry:
        return FavoriteRegistry(root / "favorites", account_key=account_key)

    def test_same_candidate_merges_sources_without_changing_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))

            self.assertEqual(
                registry.record_candidates(
                    ["geek-1"],
                    source="list_sync",
                    receipt_id="sync-1",
                    observed_at="2026-09-03T10:00:00+08:00",
                ),
                1,
            )
            self.assertEqual(
                registry.record_candidates(
                    ["geek-1"],
                    source="favorite_confirmed",
                    receipt_id="delivery-1",
                    observed_at="2026-09-04T10:00:00+08:00",
                ),
                0,
            )

            record = registry.record("geek-1")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["sources"], ["favorite_confirmed", "list_sync"])
            self.assertEqual(record["first_seen_at"], "2026-09-03T10:00:00+08:00")
            self.assertEqual(record["last_seen_at"], "2026-09-04T10:00:00+08:00")
            self.assertEqual(record["first_receipt_id"], "sync-1")
            self.assertEqual(record["last_receipt_id"], "delivery-1")

    def test_later_observation_never_deletes_missing_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))
            registry.record_candidates(
                ["geek-1", "geek-2"],
                source="list_sync",
                receipt_id="sync-1",
            )

            registry.record_candidates(["geek-2"], source="list_sync", receipt_id="sync-2")

            self.assertEqual(registry.known_candidate_ids(), {"geek-1", "geek-2"})

    def test_complete_checkpoint_keeps_order_and_rejects_incomplete_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))
            anchor = [f"anchor-{index:02d}" for index in range(1, 11)]

            checkpoint = registry.save_complete_checkpoint(
                anchor_group=anchor,
                receipt_id="sync-1",
                sync_status="end_reached",
                completed_at="2026-09-04T10:00:00+08:00",
            )

            self.assertEqual(checkpoint["anchor_group"], anchor)
            self.assertEqual(registry.checkpoint(), checkpoint)
            with self.assertRaisesRegex(ValueError, "complete"):
                registry.save_complete_checkpoint(
                    anchor_group=anchor,
                    receipt_id="sync-2",
                    sync_status="sync_incomplete",
                )
            self.assertEqual(registry.checkpoint(), checkpoint)

    def test_checkpoint_rejects_duplicate_or_more_than_ten_anchor_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))

            with self.assertRaisesRegex(ValueError, "duplicate"):
                registry.save_complete_checkpoint(
                    anchor_group=["geek-1", "geek-1"],
                    receipt_id="sync-1",
                    sync_status="end_reached",
                )
            with self.assertRaisesRegex(ValueError, "10"):
                registry.save_complete_checkpoint(
                    anchor_group=[f"geek-{index}" for index in range(11)],
                    receipt_id="sync-1",
                    sync_status="end_reached",
                )

    def test_concurrent_instances_do_not_lose_candidate_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def record(candidate_id: str) -> None:
                self.make_registry(root).record_candidates(
                    [candidate_id],
                    source="list_sync",
                    receipt_id=f"sync-{candidate_id}",
                )

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(record, [f"geek-{index}" for index in range(20)]))

            self.assertEqual(
                self.make_registry(root).known_candidate_ids(),
                {f"geek-{index}" for index in range(20)},
            )

    def test_registry_and_lock_files_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))
            registry.record_candidates(["geek-1"], source="list_sync", receipt_id="sync-1")
            registry.save_complete_checkpoint(
                anchor_group=["geek-1"],
                receipt_id="sync-1",
                sync_status="end_reached",
            )

            self.assertEqual(registry.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(registry.registry_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(registry.checkpoint_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(registry.lock_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_schema_or_account_binding_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "favorites"
            root.mkdir()
            (root / "registry.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "contract": "boss_favorite_registry",
                        "account_key": "other-account",
                        "candidates": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "account"):
                FavoriteRegistry(root, account_key="0123456789abcdef")

    def test_empty_ids_and_unapproved_sources_fail_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = self.make_registry(Path(temporary))

            with self.assertRaises(ValueError):
                registry.record_candidates([""], source="list_sync", receipt_id="sync-1")
            with self.assertRaisesRegex(ValueError, "source"):
                registry.record_candidates(
                    ["geek-1"], source="screenshot_match", receipt_id="sync-1"
                )
            self.assertFalse(registry.registry_path.exists())


if __name__ == "__main__":
    unittest.main()
