from __future__ import annotations

import asyncio
import os
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v5 as v5

core = v5.core


def market_event(
    mint: str,
    kind: core.EventKind,
    *,
    at_ns: int,
    trader: str,
    sol: float,
    price: float,
    signature: str,
    slot: int = 1,
) -> core.Event:
    return core.Event(
        event_id=abs(hash((mint, kind, at_ns, signature))) % 1_000_000,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        slot=slot,
        signature=signature,
        trader=trader,
        sol_amount=sol,
        token_amount=1_000.0,
        price_sol=price,
        fdv_usd=3_200.0,
        creator=trader if kind == core.EventKind.CREATE else None,
    )


def creator_follow_state() -> core.TokenState:
    now = time.time_ns()
    state = core.TokenState("mint")
    state.apply(
        market_event(
            "mint",
            core.EventKind.CREATE,
            at_ns=now,
            trader="creator",
            sol=0.0,
            price=3.0e-8,
            signature="create-and-buy",
        ),
        None,
    )
    state.apply(
        market_event(
            "mint",
            core.EventKind.BUY,
            at_ns=now + 5_000_000,
            trader="creator",
            sol=0.05,
            price=3.2e-8,
            signature="create-and-buy",
        ),
        None,
    )
    state.creator = "creator"
    state.fdv_usd = 3_200.0
    return state


class V5SelectionTests(unittest.TestCase):
    def test_funded_live_mode_fails_closed_without_calibrated_model(self) -> None:
        policy = core.E4Policy(core.Settings(live=True, model_path=Path("missing-model.json")))
        with patch.dict(os.environ, {"E4_SELECTION_MODE": ""}, clear=False):
            accepted, _, _, reason, _ = policy.entry(creator_follow_state())
        self.assertFalse(accepted)
        self.assertIn("calibrated", reason)

    def test_research_cohort_is_explicit_and_hypothesis_only(self) -> None:
        policy = core.E4Policy(core.Settings(live=False, model_path=Path("missing-model.json")))
        with patch.dict(os.environ, {"E4_SELECTION_MODE": "RESEARCH_COHORT"}, clear=False):
            accepted, score, fraction, reason, features = policy.entry(creator_follow_state())
        self.assertTrue(accepted, reason)
        self.assertGreater(score, 0)
        self.assertGreater(fraction, 0)
        self.assertEqual(features["creator_buy_in_create_tx"], 1.0)
        self.assertIn("research-only", reason)

    def test_research_cohort_cannot_fund_trade_without_double_opt_in(self) -> None:
        policy = core.E4Policy(core.Settings(live=True, model_path=Path("missing-model.json")))
        with patch.dict(
            os.environ,
            {"E4_SELECTION_MODE": "RESEARCH_COHORT", "E4_ALLOW_RESEARCH_LIVE": "false"},
            clear=False,
        ):
            accepted, _, _, reason, _ = policy.entry(creator_follow_state())
        self.assertFalse(accepted)
        self.assertIn("forbidden", reason)


class V5SellSerializationTests(unittest.IsolatedAsyncioTestCase):
    def make_position(self, mint: str) -> core.Position:
        return core.Position(
            position_id=f"position-{mint}",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=0.05,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=1.0,
            max_price=1.0,
            last_price=1.0,
            entry_signature=f"entry-{mint}",
        )

    async def test_same_mint_sell_mutations_are_serialized(self) -> None:
        original = v5._previous_execute_sell
        active = 0
        max_active = 0

        async def fake_previous(engine, position, fraction, reason):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

        v5._previous_execute_sell = fake_previous
        try:
            engine = types.SimpleNamespace(position_locks={})
            item = self.make_position("same")
            await asyncio.gather(
                core.Engine.execute_sell(engine, item, 0.20, "partial"),
                core.Engine.execute_sell(engine, item, 1.00, "close"),
            )
        finally:
            v5._previous_execute_sell = original
        self.assertEqual(max_active, 1)

    async def test_different_mints_can_exit_in_parallel(self) -> None:
        original = v5._previous_execute_sell
        active = 0
        max_active = 0

        async def fake_previous(engine, position, fraction, reason):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

        v5._previous_execute_sell = fake_previous
        try:
            engine = types.SimpleNamespace(position_locks={})
            await asyncio.gather(
                core.Engine.execute_sell(engine, self.make_position("one"), 1.0, "close"),
                core.Engine.execute_sell(engine, self.make_position("two"), 1.0, "close"),
            )
        finally:
            v5._previous_execute_sell = original
        self.assertEqual(max_active, 2)


if __name__ == "__main__":
    unittest.main()
