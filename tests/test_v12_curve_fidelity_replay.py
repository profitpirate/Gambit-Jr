from __future__ import annotations

import unittest

from scripts import e4_v12_curve_fidelity_replay as replay


class V12CurveFidelityReplayTests(unittest.TestCase):
    def test_reverse_buy_recovers_pre_e4_reserves(self):
        # Synthetic constant-product buy: pre x=30 SOL, y=1000 tokens, input=3 SOL.
        # dy = 3*1000/(30+3) = 90.9090909 tokens.
        token_out = 3.0 * 1000.0 / 33.0
        buy = {
            "sol_amount": 3.0,
            "token_amount": token_out,
            "raw": {
                "virtual_sol_reserves": 33.0 * replay.LAMPORTS,
                "virtual_token_reserves": (1000.0 - token_out) * replay.TOKEN_SCALE,
                "real_token_reserves": (800.0 - token_out) * replay.TOKEN_SCALE,
            },
        }
        recovered = replay.e4_pre_buy_state(buy)
        self.assertIsNotNone(recovered)
        sol, tokens, real = recovered
        self.assertAlmostEqual(sol, 30.0, places=8)
        self.assertAlmostEqual(tokens, 1000.0, places=6)
        self.assertAlmostEqual(real, 800.0, places=6)

    def test_curve_buy_and_sell_are_inverse_without_external_flow(self):
        x, y = 30.0, 1000.0
        dx = 3.0
        tokens = replay.buy_against_curve(dx, x, y, 1e9)
        post_x = x + dx
        post_y = y - tokens
        recovered_sol = replay.sell_against_curve(tokens, post_x, post_y)
        self.assertAlmostEqual(recovered_sol, dx, places=9)

    def test_state_at_or_before_never_jumps_to_future_trade(self):
        states = [
            (100, 30.0, 1000.0, 800.0),
            (200, 31.0, 990.0, 790.0),
            (300, 32.0, 980.0, 780.0),
        ]
        self.assertEqual(replay.state_at_or_before(states, 250)[0], 200)
        self.assertEqual(replay.state_at_or_before(states, 200)[0], 200)
        self.assertIsNone(replay.state_at_or_before(states, 99))

    def test_affordable_curve_input_respects_wallet_reserve_and_costs(self):
        curve = replay.affordable_curve_input(3.0, 1.2, 0.0125)
        total = curve * 1.0125 + replay.fee_bid(curve, 0.96)
        self.assertLessEqual(total, 1.2 - 0.03 + 1e-9)
        self.assertLess(curve, 3.0)


if __name__ == "__main__":
    unittest.main()
