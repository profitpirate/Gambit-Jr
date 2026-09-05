from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import e4_v12_failed_aware_preimpact_v2 as failed_aware


class FailedAwarePreimpactTests(unittest.TestCase):
    def _launch(self):
        return SimpleNamespace(
            mint="mint-a",
            creator="creator-a",
            create_ns=1_000_000_000,
            create_slot=10,
            events=[
                {
                    "received_ns": 1_000_000_000,
                    "slot": 10,
                    "event_index": 0,
                    "kind": "CREATE",
                    "mint": "mint-a",
                    "trader": "creator-a",
                },
                {
                    "received_ns": 1_050_000_000,
                    "slot": 10,
                    "event_index": 1,
                    "kind": "BUY",
                    "mint": "mint-a",
                    "trader": "buyer-a",
                },
                {
                    "received_ns": 1_200_000_000,
                    "slot": 10,
                    "event_index": 2,
                    "kind": "BUY",
                    "mint": "mint-a",
                    "trader": "buyer-b",
                },
            ],
        )

    def test_failed_attempt_labels_only_earlier_snapshot_and_updates_later_history(self):
        launch = self._launch()
        runs = [SimpleNamespace(launches={launch.mint: launch})]
        original_rows = [
            {
                "mint": "mint-a",
                "creator": "creator-a",
                "decision_ns": 1_100_000_000,
                "positive": False,
                "first_buyers": ["buyer-a"],
                "prior_creator_attempts": 0,
                "known_buyer_count": 0,
                "max_prior_buyer_attempts": 0,
                "sum_prior_buyer_attempts": 0,
                "max_creator_buyer_pair": 0,
            },
            {
                "mint": "mint-a",
                "creator": "creator-a",
                "decision_ns": 1_300_000_000,
                "positive": False,
                "first_buyers": ["buyer-a", "buyer-b"],
                "prior_creator_attempts": 0,
                "known_buyer_count": 0,
                "max_prior_buyer_attempts": 0,
                "sum_prior_buyer_attempts": 0,
                "max_creator_buyer_pair": 0,
            },
        ]
        payload = {
            "attempts_by_mint": {
                "mint-a": [
                    {
                        "mint": "mint-a",
                        "attempt_ns": 1_150_000_000,
                        "attempt_slot": 10,
                        "attempt_transaction_index": 3,
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.dict(os.environ, {"E4_FAILED_INTENT_REGISTRY": str(path)}, clear=False):
                with patch.object(
                    failed_aware,
                    "_ORIGINAL_BUILD_DATASET",
                    return_value=[dict(row) for row in original_rows],
                ):
                    rows = failed_aware.build_dataset_failed_aware_v2(runs, 100.0)

        early, late = rows
        self.assertTrue(early["positive"])
        self.assertEqual(early["intent_label"], "FAILED_ATTEMPT")
        self.assertAlmostEqual(early["lead_ms"], 50.0)
        self.assertEqual(early["prior_creator_attempts"], 0)

        self.assertFalse(late["positive"])
        self.assertEqual(late["prior_creator_attempts"], 1)
        self.assertEqual(late["failed_intent_history_count"], 1)
        self.assertGreaterEqual(late["known_buyer_count"], 1)

    def test_attempt_outside_horizon_is_not_labelled_positive(self):
        launch = self._launch()
        runs = [SimpleNamespace(launches={launch.mint: launch})]
        original = [
            {
                "mint": "mint-a",
                "creator": "creator-a",
                "decision_ns": 1_000_000_000,
                "positive": False,
                "first_buyers": [],
                "prior_creator_attempts": 0,
                "known_buyer_count": 0,
                "max_prior_buyer_attempts": 0,
                "sum_prior_buyer_attempts": 0,
                "max_creator_buyer_pair": 0,
            }
        ]
        payload = {"attempts_by_mint": {"mint-a": [{"attempt_ns": 2_000_000_000}]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.dict(os.environ, {"E4_FAILED_INTENT_REGISTRY": str(path)}, clear=False):
                with patch.object(failed_aware, "_ORIGINAL_BUILD_DATASET", return_value=[dict(original[0])]):
                    rows = failed_aware.build_dataset_failed_aware_v2(runs, 100.0)
        self.assertFalse(rows[0]["positive"])
        self.assertNotIn("failed_e4_intent_target", rows[0])


if __name__ == "__main__":
    unittest.main()
