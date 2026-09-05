from __future__ import annotations

import unittest

from scripts import e4_v12_true_latency_replay as replay


class TrueLatencyReplayTests(unittest.TestCase):
    def state(self, ns: int, sequence: int, x: float, y: float) -> replay.ReserveState:
        return replay.ReserveState(
            received_ns=ns,
            sequence=sequence,
            virtual_sol=x,
            virtual_tokens=y,
            real_tokens=1_000_000_000.0,
            price_sol=x / y,
            fdv_usd=4_500.0,
        )

    def test_arrival_time_precedes_event_index(self):
        earlier = {"received_ns": 100, "slot": 5, "event_index": 99, "signature": "a"}
        later = {"received_ns": 101, "slot": 5, "event_index": 0, "signature": "b"}
        self.assertLess(replay.event_sort_key(earlier), replay.event_sort_key(later))

    def test_same_timestamp_sequence_does_not_leak_future_state(self):
        states = [
            self.state(100, 0, 30.0, 1_000.0),
            self.state(100, 1, 35.0, 850.0),
        ]
        self.assertEqual(replay.state_at_or_before(states, 100, 0).sequence, 0)
        self.assertEqual(replay.state_at_or_before(states, 100, 1).sequence, 1)

    def test_curve_buy_and_sell_are_inverse_without_external_flow(self):
        before = self.state(100, 0, 30.0, 1_000.0)
        input_sol = 3.0
        tokens = replay.buy_tokens(input_sol, before)
        after = self.state(101, 1, 33.0, 1_000.0 - tokens)
        recovered = replay.sell_sol(tokens, after)
        self.assertAlmostEqual(recovered, input_sol, places=9)

    def test_output_floor_rejects_a_deteriorated_fill(self):
        decision = self.state(100, 0, 30.0, 1_000.0)
        moved = self.state(105, 1, 36.0, 830.0)
        curve_input = 0.10
        expected = replay.buy_tokens(curve_input, decision)
        actual = replay.buy_tokens(curve_input, moved)
        shortfall = (1.0 - actual / expected) * 10_000.0
        self.assertGreater(shortfall, 600.0)

    def test_budget_solver_includes_protocol_and_route_cost(self):
        budget = 0.0555
        curve = replay.curve_input_for_budget(budget, 0.0125, 0.96)
        total = curve * 1.0125 + replay.fee_bid(curve, 0.96)
        self.assertLessEqual(total, budget + 1e-12)
        self.assertGreater(curve, 0.0)


if __name__ == "__main__":
    unittest.main()
