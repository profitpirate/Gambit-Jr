from __future__ import annotations

import unittest

from scripts.e4_v12_preimpact_model import Launch, Rule, score, trigger


class V12PreImpactModelTests(unittest.TestCase):
    def test_trigger_uses_only_snapshots_before_e4(self):
        launch = Launch(
            mint="mint",
            creator="creator",
            created_ns=1_000_000_000,
            e4_buy_ns=1_050_000_000,
            e4_won=True,
            snapshots=[{
                "received_ns": 1_020_000_000,
                "buy_rank": 3,
                "creator_seed_sol": 2.5,
                "noncreator_buyers": 2,
                "sell_count": 0,
                "fdv_usd": 5000.0,
                "age_ms": 20.0,
            }],
        )
        rule = Rule(2.0, 5.0, 4000.0, 8500.0, 2, 5, 1, 100.0)
        snap = trigger(launch, rule)
        self.assertIsNotNone(snap)
        self.assertLess(snap["received_ns"], launch.e4_buy_ns)

    def test_pre_entry_sell_invalidates_candidate(self):
        launch = Launch(
            mint="mint",
            creator="creator",
            created_ns=1,
            e4_buy_ns=100,
            e4_won=False,
            snapshots=[{
                "received_ns": 20,
                "buy_rank": 3,
                "creator_seed_sol": 3.0,
                "noncreator_buyers": 2,
                "sell_count": 1,
                "fdv_usd": 5000.0,
                "age_ms": 0.0,
            }],
        )
        self.assertIsNone(trigger(launch, Rule(1.0, 5.0, 3000.0, 8500.0, 2, 5, 1, 500.0)))

    def test_score_penalizes_false_positive_spray(self):
        rule = Rule(1.0, 5.0, 3000.0, 8500.0, 2, 5, 1, 500.0)
        good = Launch("good", "c", 1, 100, True, [{"received_ns": 20, "buy_rank": 2, "creator_seed_sol": 2.0, "noncreator_buyers": 1, "sell_count": 0, "fdv_usd": 5000.0, "age_ms": 0.0}])
        bad = Launch("bad", "c2", 1, None, False, [{"received_ns": 20, "buy_rank": 2, "creator_seed_sol": 2.0, "noncreator_buyers": 1, "sell_count": 0, "fdv_usd": 5000.0, "age_ms": 0.0}])
        metrics = score([good, bad], rule)
        self.assertEqual(metrics["true_e4_candidates"], 1)
        self.assertEqual(metrics["false_positives"], 1)
        self.assertAlmostEqual(metrics["precision"], 0.5)


if __name__ == "__main__":
    unittest.main()
