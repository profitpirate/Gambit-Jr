from __future__ import annotations

import unittest

from memecoin_bot import e4_strict_output_deferred_v12 as deferred


class DeferredStrictOutputTests(unittest.TestCase):
    def test_missing_pre_enrichment_reserves_are_deferred_not_rejected(self):
        request = {
            "side": "BUY",
            "mint": "mint-a",
            "amount": 0.05,
            "slippage_bps": 9_000,
            "metadata": {
                "e4_direct_copy": True,
                "expected_token_output": 10_000.0,
            },
        }
        protected = deferred.guarded_request(request)
        self.assertEqual(protected["slippage_bps"], 800)
        self.assertTrue(protected["metadata"]["strict_output_guard"])
        self.assertTrue(protected["metadata"]["strict_output_check_deferred"])

    def test_enriched_bad_quote_is_still_rejected(self):
        request = {
            "side": "BUY",
            "mint": "mint-b",
            "amount": 0.05,
            "slippage_bps": 9_000,
            "metadata": {
                "e4_direct_copy": True,
                "expected_token_output": 100_000.0,
                "virtual_sol_reserves": 100.0,
                "virtual_token_reserves": 1_000_000.0,
                "real_token_reserves": 1_000_000.0,
                "total_fee_bps": 125,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "strict token-output rejection"):
            deferred.guarded_request(request)


if __name__ == "__main__":
    unittest.main()
