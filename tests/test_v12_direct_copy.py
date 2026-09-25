from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from memecoin_bot import e4_direct_copy_v12 as direct
from memecoin_bot import e4_production_guard_v12 as production_guard


class V12DirectCopyTests(unittest.TestCase):
    def test_default_direct_copy_slippage_uses_six_percent_guard(self):
        settings = SimpleNamespace(buy_slippage_bps=800)
        with patch.dict(os.environ, {"E4_DIRECT_COPY_SLIPPAGE_BPS": ""}, clear=False):
            os.environ.pop("E4_DIRECT_COPY_SLIPPAGE_BPS", None)
            self.assertEqual(direct.direct_copy_slippage_bps(settings), 600)

    def test_direct_copy_slippage_is_bounded_by_canonical_guard(self):
        settings = SimpleNamespace(buy_slippage_bps=800)
        with patch.dict(os.environ, {"E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS": "12000"}, clear=False):
            self.assertEqual(direct.direct_copy_slippage_bps(settings), 1200)

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
        self.assertIs(
            production_guard._PREVIOUS_EXECUTE_BUY,
            direct._execute_buy_direct_copy_v12,
        )
    def test_production_guard_delegates_authoritative_direct_copy(self):
        safety = SimpleNamespace(entries_allowed=True, reason="")
        engine = SimpleNamespace(
            v12_breaker=SimpleNamespace(store=SimpleNamespace(snapshot=lambda: safety)),
            pending_entries=set(),
            positions={},
            store=SimpleNamespace(decision=Mock()),
        )
        state = SimpleNamespace(mint="direct-copy-mint")
        previous = AsyncMock()

        with (
            patch.object(
                production_guard,
                "_is_direct_copy_authoritative",
                return_value=True,
            ),
            patch.object(production_guard, "_PREVIOUS_EXECUTE_BUY", previous),
        ):
            asyncio.run(
                production_guard._execute_buy_production(
                    engine,
                    state,
                    0.95,
                    0.2,
                    "direct copy regression",
                )
            )

        previous.assert_awaited_once_with(
            engine,
            state,
            0.95,
            0.2,
            "direct copy regression",
        )


if __name__ == "__main__":
    unittest.main()
