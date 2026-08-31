from __future__ import annotations

import time
import unittest
from pathlib import Path

from memecoin_bot import e4_hardening_v10 as v10

core = v10.core
v6 = v10.v6


class CopyExitTests(unittest.TestCase):
    def position(self, mint: str) -> core.Position:
        return core.Position(
            position_id=f"position-{mint}",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=0.000001,
            max_price=0.000001,
            last_price=0.000001,
            entry_signature="our-entry",
        )

    def state(self, mint: str) -> core.TokenState:
        now = time.time_ns()
        state = core.TokenState(mint)
        state.apply(
            core.Event(
                event_id=1,
                kind=core.EventKind.CREATE,
                mint=mint,
                source_ns=now,
                received_ns=now,
                creator="creator",
                trader="creator",
                price_sol=0.000001,
                fdv_usd=4_000,
                signature="create",
            ),
            None,
        )
        return state

    def profile(self, mint: str) -> None:
        v6._PROFILE_BY_MINT[mint] = v6.EntryProfile(
            family="e4_confirmed_fast_copy",
            tier="high",
            fraction=0.03,
            score=0.97,
            first_partial_fraction=0.20,
            features={},
        )

    def test_first_e4_partial_requests_matching_partial(self) -> None:
        mint = "copy-partial"
        self.profile(mint)
        v10.PIPELINES.observe_e4_entry(
            {
                "mint": mint,
                "creator": "creator",
                "observed_ns": time.time_ns(),
                "entry_price_sol": 0.000001,
                "entry_sol": 2.0,
                "token_amount": 1_000.0,
                "signature": "e4-entry",
            }
        )
        v10.PIPELINES.observe_e4_exit(
            mint,
            token_amount=200.0,
            observed_ns=time.time_ns(),
        )
        action, fraction, reason = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).exit(self.position(mint), self.state(mint))
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.20)

    def test_full_e4_exit_liquidates_copy(self) -> None:
        mint = "copy-full"
        self.profile(mint)
        v10.PIPELINES.observe_e4_entry(
            {
                "mint": mint,
                "creator": "creator",
                "observed_ns": time.time_ns(),
                "entry_price_sol": 0.000001,
                "entry_sol": 2.0,
                "token_amount": 1_000.0,
                "signature": "e4-entry",
            }
        )
        v10.PIPELINES.observe_e4_exit(
            mint,
            token_amount=1_000.0,
            observed_ns=time.time_ns(),
        )
        action, fraction, reason = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).exit(self.position(mint), self.state(mint))
        self.assertEqual(action, "SELL_FULL", reason)
        self.assertEqual(fraction, 1.0)


if __name__ == "__main__":
    unittest.main()
