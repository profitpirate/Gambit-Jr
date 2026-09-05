from __future__ import annotations

import unittest

from scripts import e4_v12_independent_exit_replay as replay


class IndependentExitReplayTests(unittest.TestCase):
    def _event(self, ns: int, virtual_sol: float, virtual_tokens: float, kind: str = "BUY"):
        return {
            "received_ns": ns,
            "slot": 1,
            "event_index": ns,
            "kind": kind,
            "mint": "mint",
            "trader": "other",
            "fdv_usd": 4_000.0,
            "raw": {
                "virtual_sol_reserves": virtual_sol,
                "virtual_token_reserves": virtual_tokens,
                "real_token_reserves": virtual_tokens * 0.75,
                "total_fee_bps": 125,
            },
        }

    def test_fast_failure_hits_hard_stop(self):
        decision = 1_000_000_000
        rows = [
            self._event(decision, 30.0, 1_000_000.0, "CREATE"),
            self._event(decision + 10_000_000, 27.0, 1_111_111.0, "SELL"),
            self._event(decision + 20_000_000, 25.0, 1_200_000.0, "SELL"),
        ]
        prediction = replay.base.Prediction("mint", decision, 0.0185, 0.96, "preimpact", {})
        policy = replay.ExitPolicy(0.05, 0.20, 0.30, 0.60, 0.15, 0.0, 1_000.0, 0.0)
        position, status = replay.simulate_independent(
            prediction,
            rows,
            liquid_sol=3.0,
            latency_ms=0.0,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=800,
            policy=policy,
        )
        self.assertEqual(status, "filled")
        self.assertIsNotNone(position)
        self.assertEqual(position.exit_reason, "hard_stop")
        self.assertFalse(position.first_partial_done)

    def test_runner_takes_partial_then_final_profit(self):
        decision = 2_000_000_000
        rows = [
            self._event(decision, 30.0, 1_000_000.0, "CREATE"),
            self._event(decision + 10_000_000, 38.0, 789_473.684, "BUY"),
            self._event(decision + 20_000_000, 55.0, 545_454.545, "BUY"),
        ]
        prediction = replay.base.Prediction("mint", decision, 0.0185, 0.96, "preimpact", {})
        policy = replay.ExitPolicy(0.10, 0.15, 0.30, 0.50, 0.20, 0.0, 1_000.0, 0.0)
        position, status = replay.simulate_independent(
            prediction,
            rows,
            liquid_sol=3.0,
            latency_ms=0.0,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=800,
            policy=policy,
        )
        self.assertEqual(status, "filled")
        self.assertIsNotNone(position)
        self.assertTrue(position.first_partial_done)
        self.assertIn(position.exit_reason, {"final_take_profit", "trailing_stop", "maximum_hold", "tail_flatten"})
        self.assertGreater(position.sell_count, 0)

    def test_policy_round_trip(self):
        policy = replay.ExitPolicy(0.1, 0.2, 0.3, 0.6, 0.15, 0.0, 5_000.0, 25.0)
        self.assertEqual(policy, replay.ExitPolicy.from_dict(policy.as_dict()))


if __name__ == "__main__":
    unittest.main()
