from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from memecoin_bot import e4_strict_output_v12 as strict
from memecoin_bot import e4_tight_output_v12 as guard


class TightOutputTests(unittest.TestCase):
    def test_direct_buy_slippage_no_longer_defaults_to_ninety_percent(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(guard.guarded_buy_slippage_bps(), 800)
            self.assertLessEqual(guard.guarded_buy_slippage_bps(), 2_500)

    def test_current_curve_quote_normalizes_raw_reserves(self):
        request = {
            "side": "BUY",
            "amount": 1.0,
            "metadata": {
                "virtual_sol_reserves": 30_000_000_000,
                "virtual_token_reserves": 1_000_000_000_000,
                "real_token_reserves": 800_000_000_000,
                "total_fee_bps": 125,
            },
        }
        quote = strict.current_curve_token_quote(request)
        self.assertGreater(quote, 0)
        self.assertLess(quote, 800_000)

    def test_guard_rejects_source_output_that_current_curve_cannot_supply(self):
        request = {
            "side": "BUY",
            "mint": "mint-a",
            "amount": 1.0,
            "slippage_bps": 9_000,
            "metadata": {
                "e4_direct_copy": True,
                "strict_output_guard": True,
                "expected_token_output": 100_000.0,
                "max_output_shortfall_bps": 800,
                "virtual_sol_reserves": 100.0,
                "virtual_token_reserves": 1_000_000.0,
                "real_token_reserves": 1_000_000.0,
                "total_fee_bps": 125,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "strict token-output rejection"):
            strict.guarded_request(request)

    def test_guard_accepts_good_quote_and_overrides_forced_slippage(self):
        request = {
            "side": "BUY",
            "mint": "mint-b",
            "amount": 1.0,
            "slippage_bps": 9_000,
            "metadata": {
                "e4_direct_copy": True,
                "strict_output_guard": True,
                "expected_token_output": 30_000.0,
                "max_output_shortfall_bps": 800,
                "virtual_sol_reserves": 30.0,
                "virtual_token_reserves": 1_000_000.0,
                "real_token_reserves": 800_000.0,
                "total_fee_bps": 125,
            },
        }
        guarded = strict.guarded_request(request)
        self.assertEqual(guarded["slippage_bps"], 800)
        self.assertTrue(guarded["metadata"]["strict_output_guard"])
        self.assertGreater(guarded["metadata"]["current_quoted_token_output"], 30_000.0 * 0.92)


if __name__ == "__main__":
    unittest.main()
