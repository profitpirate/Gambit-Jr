from __future__ import annotations

import os
import time
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_sub10ms_repairs_v12 as repairs
from memecoin_bot.e4_pipeline_manager_v11 import E4Signal

PIPELINES = repairs.PIPELINES
v6 = repairs.v6
core = repairs.core


class Sub10msRepairTests(unittest.TestCase):
    def setUp(self):
        self.entries = PIPELINES._e4_entries
        self.profiles = dict(v6._PROFILE_BY_MINT)
        PIPELINES._e4_entries = MappingProxyType({})
        v6._PROFILE_BY_MINT.clear()

    def tearDown(self):
        PIPELINES._e4_entries = self.entries
        v6._PROFILE_BY_MINT.clear()
        v6._PROFILE_BY_MINT.update(self.profiles)

    def source(self, mint: str, observed_ns: int) -> E4Signal:
        return E4Signal(
            mint=mint,
            creator="creator",
            observed_ns=observed_ns,
            entry_price_sol=1.0,
            entry_sol=1.0,
            signature="entry",
            entry_tokens=1_000.0,
            remaining_tokens=1_000.0,
            last_sell_fraction=0.0,
            last_sell_ns=0,
            last_sell_signature="",
            sell_count=0,
            fully_exited=False,
            sold=False,
        )

    def position(self, mint: str):
        return core.Position(
            position_id="position",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=1.0,
            max_price=1.0,
            last_price=1.0,
            entry_signature="simulation",
        )

    def state(self, mint: str, latest_ns: int):
        state = core.TokenState(mint)
        state.created_ns = latest_ns - 1_000_000
        state.latest_ns = latest_ns
        state.price_sol = 1.0
        state.fdv_usd = 4_500.0
        return state

    def policy(self):
        return core.E4Policy(SimpleNamespace(model_path=None))

    def install(self, mint: str, source: E4Signal) -> None:
        PIPELINES._e4_entries = MappingProxyType({mint: source})
        v6._PROFILE_BY_MINT[mint] = SimpleNamespace(
            family=repairs.role_model.ROLE_MODEL_FAMILY
        )

    def test_output_floor_defaults_to_six_percent_not_ninety(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS", None)
            self.assertEqual(repairs.max_output_shortfall_bps(), 600)
        settings = SimpleNamespace(buy_slippage_bps=8_000)
        self.assertEqual(repairs.direct.direct_copy_slippage_bps(settings), 600)

    def test_output_floor_override_is_bounded(self):
        with patch.dict(
            os.environ,
            {"E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS": "5000"},
            clear=False,
        ):
            self.assertEqual(repairs.max_output_shortfall_bps(), 1_200)
        with patch.dict(
            os.environ,
            {"E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS": "1"},
            clear=False,
        ):
            self.assertEqual(repairs.max_output_shortfall_bps(), 50)

    def test_token_accounted_four_leg_exit_is_preserved(self):
        mint = "mint-four-leg"
        observed = time.time_ns()
        self.install(mint, self.source(mint, observed))
        values = []
        for index, amount in enumerate((300.0, 350.0, 200.0, 150.0), start=1):
            values.append(PIPELINES.observe_e4_exit(
                mint,
                token_amount=amount,
                observed_ns=observed + index,
                signature=f"sell-{index}",
            ))
        self.assertEqual([row.sell_count for row in values], [1, 2, 3, 4])
        self.assertEqual(
            [round(row.remaining_tokens, 6) for row in values],
            [700.0, 350.0, 150.0, 0.0],
        )
        self.assertEqual([row.fully_exited for row in values], [False, False, False, True])

    def test_direct_copy_waits_for_source_instead_of_manufacturing_stop(self):
        mint = "mint-source-authoritative"
        observed = time.time_ns()
        self.install(mint, self.source(mint, observed))
        action, fraction, reason = self.policy().exit(
            self.position(mint),
            self.state(mint, observed + 1_000_000_000),
        )
        self.assertEqual(action, "HOLD", reason)
        self.assertEqual(fraction, 0.0)

    def test_cumulative_source_partial_is_mirrored_exactly(self):
        mint = "mint-partial"
        observed = time.time_ns()
        source = self.source(mint, observed)
        source = repairs.replace(
            source,
            remaining_tokens=700.0,
            sell_count=1,
            last_sell_ns=observed + 1,
            last_sell_fraction=0.30,
            sold=True,
        )
        self.install(mint, source)
        action, fraction, reason = self.policy().exit(
            self.position(mint),
            self.state(mint, observed + 2),
        )
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30, places=9)


if __name__ == "__main__":
    unittest.main()
