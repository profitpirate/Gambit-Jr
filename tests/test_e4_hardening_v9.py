from __future__ import annotations

import time
import unittest
from pathlib import Path

from memecoin_bot import e4_hardening_v9 as v9

core = v9.core
v6 = v9.v6


def event(event_id: int, kind: core.EventKind, mint: str, at_ns: int, *, trader: str | None = None, creator: str | None = None, sol: float = 0.0, price: float = 1e-6, fdv: float = 4_000.0) -> core.Event:
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        trader=trader,
        creator=creator,
        sol_amount=sol,
        token_amount=1_000.0,
        price_sol=price,
        fdv_usd=fdv,
        signature=f"sig-{mint}-{event_id}",
    )


def seeded_state(mint: str, creator: str) -> core.TokenState:
    now = time.time_ns()
    state = core.TokenState(mint)
    state.apply(event(1, core.EventKind.CREATE, mint, now, trader=creator, creator=creator, fdv=3_500.0), None)
    state.apply(event(2, core.EventKind.BUY, mint, now + 5_000_000, trader=creator, creator=creator, sol=2.5, price=1.05e-6, fdv=4_100.0), None)
    return state


class CreatorExpectancyEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = dict(v9._EXPECTANCY_CREATORS)
        v9._EXPECTANCY_CREATORS.clear()
        v6._PROFILE_BY_MINT.clear()
        v6._CONTEXT_BY_MINT.clear()
        self.policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def tearDown(self) -> None:
        v9._EXPECTANCY_CREATORS.clear()
        v9._EXPECTANCY_CREATORS.update(self.original)

    def test_proven_repeat_creator_enters_immediately(self) -> None:
        creator = "proven"
        v9._EXPECTANCY_CREATORS[creator] = {"wins": 5, "losses": 1, "trades": 6, "gross_win_rate": 5 / 6}
        accepted, score, fraction, reason, features = self.policy.entry(seeded_state("proven-mint", creator))
        self.assertTrue(accepted, reason)
        self.assertIn("proven_repeat_e4_creator", reason)
        self.assertGreaterEqual(score, 0.93)
        self.assertGreaterEqual(fraction, 0.03)
        self.assertEqual(features["creator_prior_wins"], 5.0)

    def test_negative_repeat_creator_is_vetoed(self) -> None:
        creator = "negative"
        v9._EXPECTANCY_CREATORS[creator] = {"wins": 0, "losses": 4, "trades": 4, "gross_win_rate": 0.0}
        accepted, _, _, reason, _ = self.policy.entry(seeded_state("negative-mint", creator))
        self.assertFalse(accepted)
        self.assertIn("negative creator history", reason)

    def test_single_prior_winner_is_watch_only_without_repeat_history(self) -> None:
        # One historical E4 win is discovery evidence, not enough causal history
        # to authorize an independent future entry. V11 requires >=3 observed
        # trades and >=2 wins before this creator-history path can authorize.
        creator = "one-win"
        v9._EXPECTANCY_CREATORS[creator] = {"wins": 1, "losses": 0, "trades": 1, "gross_win_rate": 1.0}
        accepted, score, fraction, reason, _ = self.policy.entry(seeded_state("one-win-mint", creator))
        self.assertFalse(accepted)
        self.assertEqual(score, 0.0)
        self.assertEqual(fraction, 0.0)
        self.assertIn("identity-only gate", reason)

    def test_unknown_creator_public_flow_has_no_authority(self) -> None:
        creator = "unknown"
        state = seeded_state("unknown-mint", creator)
        now = state.latest_ns
        for index, wallet in enumerate(("a", "b", "c", "d"), start=3):
            state.apply(event(index, core.EventKind.BUY, state.mint, now + index * 2_000_000, trader=wallet, sol=3.0, price=1.2e-6 + index * 1e-8, fdv=5_000.0), None)
        accepted, _, _, reason, _ = self.policy.entry(state)
        self.assertFalse(accepted)
        self.assertIn("identity-only gate", reason)

    def test_explicit_prearmed_overrides_negative_history(self) -> None:
        creator = "authorized-negative"
        mint = "authorized-negative-mint"
        v9._EXPECTANCY_CREATORS[creator] = {"wins": 0, "losses": 4, "trades": 4, "gross_win_rate": 0.0}
        v6._CONTEXT_BY_MINT[mint] = {"creator": creator, "prearmed": True}
        accepted, _, fraction, reason, _ = self.policy.entry(seeded_state(mint, creator))
        self.assertTrue(accepted, reason)
        self.assertIn("authorized_prearmed_launch", reason)
        self.assertGreaterEqual(fraction, 0.05)


if __name__ == "__main__":
    unittest.main()
