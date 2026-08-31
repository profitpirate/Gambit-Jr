from __future__ import annotations

import time
import unittest
from pathlib import Path

from memecoin_bot import e4_hardening_v8 as v8

core = v8.core


def event(event_id: int, kind: core.EventKind, mint: str, at_ns: int, *, trader=None, creator=None, sol=0.0, price=1e-6, fdv=4_500.0):
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
        signature=f"sig-{event_id}",
    )


def seeded_state(mint: str, creator: str, *, public_burst: bool = False) -> core.TokenState:
    now = time.time_ns()
    state = core.TokenState(mint)
    state.apply(event(1, core.EventKind.CREATE, mint, now, trader=creator, creator=creator, fdv=3_500), None)
    state.apply(event(2, core.EventKind.BUY, mint, now + 5_000_000, trader=creator, creator=creator, sol=2.0, price=1.05e-6, fdv=3_800), None)
    if public_burst:
        for index, buyer in enumerate(("a", "b", "c", "d"), start=3):
            state.apply(event(index, core.EventKind.BUY, mint, now + index * 8_000_000, trader=buyer, sol=2.0, price=(1.05 + index * 0.08) * 1e-6, fdv=5_000), None)
    return state


class V8IdentityTests(unittest.TestCase):
    def setUp(self):
        v8.v6._CONTEXT_BY_MINT.clear()
        v8.v6._PROFILE_BY_MINT.clear()
        v8._WINNER_CREATORS.clear()
        v8._SOCIAL_HANDLES.clear()
        self.policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def test_repeat_e4_winner_creator_can_enter_immediately(self):
        creator = "repeat-dev"
        v8._WINNER_CREATORS[creator] = {
            "score": 0.88,
            "e4_observed_wins": 2,
            "e4_gross_pnl_sol": 8.0,
            "instant_repeat_candidate": True,
        }
        state = seeded_state("repeat-mint", creator, public_burst=False)
        accepted, score, fraction, reason, features = self.policy.entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn("repeat_e4_winning_creator", reason)
        self.assertGreaterEqual(score, 0.88)
        self.assertGreater(features["winner_creator_score"], 0.8)
        self.assertGreater(fraction, 0)

    def test_public_burst_alone_is_rejected_after_300_launch_failure(self):
        state = seeded_state("public-only", "unknown-dev", public_burst=True)
        accepted, _, _, reason, _ = self.policy.entry(state)
        self.assertFalse(accepted)
        self.assertIn("public flow alone", reason)

    def test_prelaunch_social_can_authorize_entry(self):
        mint = "social-mint"
        creator = "social-dev"
        v8.v6._CONTEXT_BY_MINT[mint] = {
            "creator": creator,
            "prelaunch_social": True,
            "social_authority_score": 0.82,
            "social_followers": 500_000,
        }
        state = seeded_state(mint, creator)
        accepted, _, _, reason, _ = self.policy.entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn("preannounced_social_community_launch", reason)

    def test_j7_metadata_alone_is_not_advance_permission(self):
        mint = "j7-only"
        creator = "j7-dev"
        v8.v6._CONTEXT_BY_MINT[mint] = {
            "creator": creator,
            "metadata_host": "metadata.j7tracker.io",
            "prearmed": False,
        }
        state = seeded_state(mint, creator)
        accepted, _, _, reason, features = self.policy.entry(state)
        self.assertFalse(accepted)
        self.assertEqual(features["j7_source"], 1.0)
        self.assertIn("public flow alone", reason)

    def test_explicit_j7_prearm_is_fastest_family(self):
        mint = "j7-prearmed"
        creator = "j7-authorized-dev"
        v8.v6._CONTEXT_BY_MINT[mint] = {
            "creator": creator,
            "metadata_host": "metadata.j7tracker.io",
            "prearmed": True,
        }
        state = seeded_state(mint, creator)
        accepted, score, fraction, reason, _ = self.policy.entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn("authorized_prearmed_launch", reason)
        self.assertGreaterEqual(score, 0.90)
        self.assertGreaterEqual(fraction, 0.05)


if __name__ == "__main__":
    unittest.main()
