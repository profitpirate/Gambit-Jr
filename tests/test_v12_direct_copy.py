from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_direct_copy_v12 as direct


class V12DirectCopyTests(unittest.TestCase):
    def test_default_direct_copy_slippage_uses_builder_ceiling(self):
        settings = SimpleNamespace(buy_slippage_bps=800)
        with patch.dict(os.environ, {"E4_DIRECT_COPY_SLIPPAGE_BPS": ""}, clear=False):
            os.environ.pop("E4_DIRECT_COPY_SLIPPAGE_BPS", None)
            self.assertEqual(direct.direct_copy_slippage_bps(settings), 9000)

    def test_direct_copy_slippage_is_bounded_by_builder(self):
        settings = SimpleNamespace(buy_slippage_bps=800)
        with patch.dict(os.environ, {"E4_DIRECT_COPY_SLIPPAGE_BPS": "12000"}, clear=False):
            self.assertEqual(direct.direct_copy_slippage_bps(settings), 9000)

    def test_exact_e4_sol_amount_is_used_when_wallet_can_support_it(self):
        amount, exact = direct.direct_copy_amount_sol(
            3.0,
            balance_sol=10.0,
            reserve_sol=0.1,
            reserved_sol=0.0,
            priority_fee_sol=0.01,
            tip_sol=0.01,
        )
        self.assertEqual(amount, 3.0)
        self.assertTrue(exact)

    def test_direct_copy_amount_uses_maximum_deployable_when_wallet_is_smaller(self):
        amount, exact = direct.direct_copy_amount_sol(
            3.0,
            balance_sol=1.2,
            reserve_sol=0.1,
            reserved_sol=0.0,
            priority_fee_sol=0.01,
            tip_sol=0.01,
        )
        self.assertAlmostEqual(amount, 1.08)
        self.assertFalse(exact)

    def test_direct_copy_bypasses_strategy_absolute_position_ceiling(self):
        amount, exact = direct.direct_copy_amount_sol(
            7.0,
            balance_sol=20.0,
            reserve_sol=0.1,
            reserved_sol=0.0,
            priority_fee_sol=0.01,
            tip_sol=0.01,
        )
        self.assertEqual(amount, 7.0)
        self.assertTrue(exact)

    def test_production_entrypoint_pins_direct_copy_module(self):
        digest = direct.policy_fingerprint()
        entrypoint = Path("src/memecoin_bot/e4_exec/__main__.py").read_text(encoding="utf-8")
        holdout = Path("scripts/e4_300_launch_holdout_v12.py").read_text(encoding="utf-8")
        self.assertIn(digest, entrypoint)
        self.assertIn(digest, holdout)
        self.assertIs(direct.core.Engine.execute_buy, direct._execute_buy_direct_copy_v12)


if __name__ == "__main__":
    unittest.main()
