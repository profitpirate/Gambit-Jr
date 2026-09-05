from __future__ import annotations

import unittest

from scripts import e4_v12_true_latency_replay as replay


class TrueLatencyReplayTests(unittest.TestCase):
    def test_received_time_is_primary_event_order(self):
        earlier = {"received_ns": 100, "slot": 9, "event_index": 99, "raw": {}}
        later = {"received_ns": 200, "slot": 1, "event_index": 0, "raw": {}}
        self.assertLess(replay.event_order(earlier), replay.event_order(later))

    def test_constant_product_buy_and_sell_are_inverse_without_external_flow(self):
        state = replay.CurveState(1, 30.0, 1_000_000.0, 800_000.0, 4_000.0)
        tokens = replay.buy_tokens(1.0, state)
        post = replay.CurveState(
            2,
            state.virtual_sol + 1.0,
            state.virtual_tokens - tokens,
            state.real_tokens - tokens,
            4_000.0,
        )
        recovered = replay.sell_sol(tokens, post)
        self.assertAlmostEqual(recovered, 1.0, places=9)

    def test_fill_latency_changes_quote_and_can_trigger_strict_guard(self):
        decision = 1_000_000_000
        rows = [
            {
                "received_ns": decision,
                "slot": 1,
                "event_index": 0,
                "kind": "CREATE",
                "mint": "mint-a",
                "raw": {
                    "virtual_sol_reserves": 30.0,
                    "virtual_token_reserves": 1_000_000.0,
                    "real_token_reserves": 800_000.0,
                },
            },
            {
                "received_ns": decision + 1_000_000,
                "slot": 1,
                "event_index": 1,
                "kind": "BUY",
                "mint": "mint-a",
                "trader": "other",
                "raw": {
                    "virtual_sol_reserves": 31.0,
                    "virtual_token_reserves": 967_741.935483871,
                    "real_token_reserves": 767_741.935483871,
                },
            },
            {
                "received_ns": decision + 10_000_000,
                "slot": 1,
                "event_index": 2,
                "kind": "BUY",
                "mint": "mint-a",
                "trader": "other-2",
                "raw": {
                    "virtual_sol_reserves": 40.0,
                    "virtual_token_reserves": 750_000.0,
                    "real_token_reserves": 550_000.0,
                },
            },
        ]
        prediction = replay.Prediction(
            mint="mint-a",
            decision_ns=decision,
            requested_fraction=0.0185,
            score=0.96,
            mode="test",
            metadata={},
        )
        fast, fast_status = replay.simulate_position(
            prediction,
            rows,
            None,
            liquid_sol=3.0,
            latency_ms=0.0,
            entry_fraction_default=0.0185,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=800,
            confirmation_ms=1.0,
        )
        slow, slow_status = replay.simulate_position(
            prediction,
            rows,
            None,
            liquid_sol=3.0,
            latency_ms=10.0,
            entry_fraction_default=0.0185,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=800,
            confirmation_ms=1.0,
        )
        self.assertEqual(fast_status, "filled")
        self.assertIsNotNone(fast)
        self.assertIsNone(slow)
        self.assertEqual(slow_status, "strict_output_guard_rejected")

    def test_each_latency_is_replayed_independently(self):
        decision = 2_000_000_000
        rows = [
            {
                "received_ns": decision,
                "slot": 2,
                "event_index": 0,
                "kind": "CREATE",
                "mint": "mint-b",
                "raw": {
                    "virtual_sol_reserves": 30.0,
                    "virtual_token_reserves": 1_000_000.0,
                    "real_token_reserves": 800_000.0,
                },
            },
            {
                "received_ns": decision + 5_000_000,
                "slot": 2,
                "event_index": 1,
                "kind": "BUY",
                "mint": "mint-b",
                "trader": "other",
                "raw": {
                    "virtual_sol_reserves": 31.0,
                    "virtual_token_reserves": 967_741.935483871,
                    "real_token_reserves": 767_741.935483871,
                },
            },
            {
                "received_ns": decision + 20_000_000,
                "slot": 2,
                "event_index": 2,
                "kind": "SELL",
                "mint": "mint-b",
                "trader": "other",
                "raw": {
                    "virtual_sol_reserves": 30.5,
                    "virtual_token_reserves": 983_606.5573770492,
                    "real_token_reserves": 783_606.5573770492,
                },
            },
        ]
        prediction = replay.Prediction("mint-b", decision, 0.0185, 0.96, "test", {})
        result_zero = replay.replay_latency(
            [prediction],
            {"mint-b": rows},
            {},
            starting_balance_sol=3.0,
            latency_ms=0.0,
            entry_fraction_default=0.0185,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=2_500,
            confirmation_ms=20.0,
            max_concurrent=2,
        )
        result_five = replay.replay_latency(
            [prediction],
            {"mint-b": rows},
            {},
            starting_balance_sol=3.0,
            latency_ms=5.0,
            entry_fraction_default=0.0185,
            reserve_sol=0.03,
            fee_bps=125,
            max_output_shortfall_bps=2_500,
            confirmation_ms=20.0,
            max_concurrent=2,
        )
        zero_position = result_zero["all_predictions"]["positions"][0]
        five_position = result_five["all_predictions"]["positions"][0]
        self.assertNotEqual(
            zero_position["quoted_tokens_at_fill"],
            five_position["quoted_tokens_at_fill"],
        )
        self.assertNotEqual(
            zero_position["pnl_sol"],
            five_position["pnl_sol"],
        )


if __name__ == "__main__":
    unittest.main()
