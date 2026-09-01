from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import MappingProxyType

from memecoin_bot import e4_hardening_v12 as v12
from memecoin_bot.e4_pipelines_v10 import CreatorProfile, CreatorSnapshot, CreatorTier

core = v12.core


def event(event_id, kind, mint, at_ns, *, creator=None, trader=None, sol=0.0, price=1e-6, fdv=4_900.0):
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        creator=creator,
        trader=trader,
        sol_amount=sol,
        token_amount=1_000.0,
        price_sol=price,
        fdv_usd=fdv,
        signature=f"sig-{event_id}",
    )


def state_for(creator: str, *, seed: float, buyers: int = 0, buyer_sol: float = 0.1, sell: bool = False, mint: str = "mint"):
    now = time.time_ns()
    state = core.TokenState(mint)
    state.apply(event(1, core.EventKind.CREATE, mint, now, creator=creator, trader=creator, fdv=4_800.0), None)
    state.apply(event(2, core.EventKind.BUY, mint, now + 1_000_000, creator=creator, trader=creator, sol=seed, price=1.01e-6, fdv=4_900.0), None)
    for i in range(buyers):
        state.apply(event(3+i, core.EventKind.BUY, mint, now + (2+i)*1_000_000, trader=f"buyer-{i}", sol=buyer_sol, price=(1.02+i*0.005)*1e-6, fdv=4_950.0+i*20), None)
    if sell:
        state.apply(event(20, core.EventKind.SELL, mint, now + 8_000_000, trader="seller", sol=0.05, price=0.99e-6, fdv=4_700.0), None)
    return state


class V12SelectionTests(unittest.TestCase):
    def setUp(self):
        self.previous = v12.PIPELINES.creators._snapshot
        v12.v6._CONTEXT_BY_MINT.clear()
        v12.v6._PROFILE_BY_MINT.clear()

    def tearDown(self):
        v12.PIPELINES.creators._snapshot = self.previous
        v12.v6._CONTEXT_BY_MINT.clear()
        v12.v6._PROFILE_BY_MINT.clear()

    def profiles(self, *rows):
        v12.PIPELINES.creators._snapshot = CreatorSnapshot(
            MappingProxyType({row.creator: row for row in rows}), time.time_ns(), "v12-test"
        )

    def policy(self):
        return core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def test_creator_tier_alone_no_longer_authorizes(self):
        self.profiles(CreatorProfile("elite", CreatorTier.ELITE, 0.95, 7, 1, 8, 0.875, source="e4-history"))
        accepted, _, _, reason, _ = self.policy().entry(state_for("elite", seed=0.05))
        self.assertFalse(accepted)
        self.assertIn("unsupported launch", reason)

    def test_elite_history_plus_meaningful_seed_can_authorize(self):
        self.profiles(CreatorProfile("elite", CreatorTier.ELITE, 0.95, 7, 1, 8, 0.875, source="e4-history"))
        accepted, score, fraction, reason, features = self.policy().entry(state_for("elite", seed=2.0))
        self.assertTrue(accepted, reason)
        self.assertIn("v12_elite_creator_quality_launch", reason)
        self.assertGreaterEqual(features["v12_launch_quality"], 0.65)
        self.assertGreater(score, 0.85)
        self.assertGreater(fraction, 0)

    def test_proven_creator_requires_launch_level_confirmation(self):
        self.profiles(CreatorProfile("proven", CreatorTier.ELITE, 0.90, 3, 1, 4, 0.75, source="e4-history"))
        accepted, _, _, reason, features = self.policy().entry(
            state_for("proven", seed=0.2, buyers=2, buyer_sol=0.1)
        )
        self.assertTrue(accepted, reason)
        self.assertEqual(features["v12_public_confirmation"], 1.0)

    def test_two_wins_one_loss_is_not_enough_anymore(self):
        self.profiles(CreatorProfile("mixed", CreatorTier.APPROVED, 0.82, 2, 1, 3, 2/3, source="e4-history"))
        accepted, _, _, reason, _ = self.policy().entry(state_for("mixed", seed=2.0, buyers=4))
        self.assertFalse(accepted)
        self.assertIn("permits observation but not autonomous entry", reason)

    def test_external_runner_without_closed_e4_history_does_not_auto_buy(self):
        self.profiles(CreatorProfile("external", CreatorTier.APPROVED, 0.94, 0, 0, 0, 0.0, source="external-runner"))
        accepted, _, _, reason, _ = self.policy().entry(state_for("external", seed=2.0, buyers=4))
        self.assertFalse(accepted)
        self.assertIn("permits observation but not autonomous entry", reason)

    def test_any_pre_entry_sell_vetoes_creator_path(self):
        self.profiles(CreatorProfile("elite", CreatorTier.ELITE, 0.95, 7, 1, 8, 0.875, source="e4-history"))
        accepted, _, _, reason, _ = self.policy().entry(state_for("elite", seed=2.0, buyers=3, sell=True))
        self.assertFalse(accepted)
        self.assertIn("sell observed", reason)

    def test_hot_decision_remains_below_36ms(self):
        self.profiles(CreatorProfile("elite", CreatorTier.ELITE, 0.95, 7, 1, 8, 0.875, source="e4-history"))
        state = state_for("elite", seed=2.0)
        maximum = 0
        for _ in range(10_000):
            started = time.perf_counter_ns()
            accepted, _, _, reason, _ = self.policy().entry(state)
            maximum = max(maximum, time.perf_counter_ns() - started)
            self.assertTrue(accepted, reason)
        self.assertLess(maximum, 36_000_000)


if __name__ == "__main__":
    unittest.main()
