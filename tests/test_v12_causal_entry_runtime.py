from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_causal_entry_v12 as causal

core = causal.core
v6 = causal.v6


def model_payload(
    *,
    features=("log_seed",),
    scale=(1.0,),
    coefficient=(1.0,),
    minimum_utility=0.5,
    minimum_margin=0.05,
):
    return {
        "version": "e4-v12-conditional-choice-ranker-v1",
        "status": "LIVE_HOLDOUT_CONFIRMED",
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2_750.0,
            "maximum_fdv_usd": 10_000.0,
            "maximum_age_ms": 1_500.0,
            "create_competition_window_ms": 750.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
        },
        "gate": {
            "minimum_utility": minimum_utility,
            "minimum_margin": minimum_margin,
            "maximum_rank": 1,
        },
        "ranker": {
            "features": list(features),
            "scale": list(scale),
            "coefficient": list(coefficient),
            "intercept": 0.0,
        },
    }


def event(
    mint: str,
    kind: str,
    at_ns: int,
    *,
    trader: str = "",
    creator: str = "",
    sol: float = 0.0,
    fdv: float = 4_500.0,
    signature: str = "",
    slot: int = 100,
):
    return SimpleNamespace(
        mint=mint,
        kind=kind,
        received_ns=at_ns,
        source_ns=at_ns,
        trader=trader,
        creator=creator,
        sol_amount=sol,
        token_amount=1_000.0,
        fdv_usd=fdv,
        price_sol=4.5e-8,
        signature=signature or f"{mint}-{kind}-{at_ns}",
        slot=slot,
    )


class CausalModelTests(unittest.TestCase):
    def test_unconfirmed_model_is_rejected(self):
        payload = model_payload()
        payload["status"] = "NOT_CONCLUSIVE"
        with self.assertRaises(ValueError):
            causal.ConditionalChoiceModel(payload)

    def test_dimension_mismatch_is_rejected(self):
        payload = model_payload(scale=(1.0, 2.0))
        with self.assertRaises(ValueError):
            causal.ConditionalChoiceModel(payload)


class CausalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.model = causal.ConditionalChoiceModel(model_payload())
        self.runtime = causal.CausalChoiceRuntime(self.model, {})
        self.enabled_patch = patch.object(causal, "ENABLED", True)
        self.enabled_patch.start()
        self.base_ns = time.time_ns()

    def tearDown(self):
        self.enabled_patch.stop()
        v6._PROFILE_BY_MINT.clear()
        v6._CONTEXT_BY_MINT.clear()

    def launch(self, mint: str, creator: str, seed: float, offset_ms: float = 0.0):
        created = self.base_ns + int(offset_ms * 1e6)
        self.runtime.observe_pre(
            event(mint, "CREATE", created, trader=creator, creator=creator, signature=f"create-{mint}")
        )
        self.runtime.observe_pre(
            event(
                mint,
                "BUY",
                created + 10_000_000,
                trader=creator,
                creator=creator,
                sol=seed,
                signature=f"seed-{mint}",
            )
        )
        return created

    def test_top_contemporaneous_seed_is_selected_not_every_candidate(self):
        self.launch("mint-low", "creator-low", 0.30, 0.0)
        self.launch("mint-high", "creator-high", 2.00, 1.0)
        high = self.runtime.decision("mint-high", self.base_ns + 12_000_000)
        low = self.runtime.decision("mint-low", self.base_ns + 12_000_000)
        self.assertIsNotNone(high)
        self.assertTrue(low is None or high.utility > low.utility)
        self.assertEqual(high.rank, 1)

    def test_pre_entry_sell_invalidates_decision(self):
        created = self.launch("mint-sold", "creator-sold", 2.00)
        self.assertIsNotNone(self.runtime.decision("mint-sold", created + 11_000_000))
        self.runtime.observe_pre(
            event(
                "mint-sold",
                "SELL",
                created + 12_000_000,
                trader="outside-seller",
                signature="sell-before-entry",
            )
        )
        self.assertIsNone(self.runtime.decision("mint-sold", created + 13_000_000))

    def test_e4_buy_cannot_create_a_preimpact_decision(self):
        mint = "mint-e4-only"
        creator = "creator-e4-only"
        created = self.base_ns
        self.runtime.observe_pre(event(mint, "CREATE", created, trader=creator, creator=creator))
        self.runtime.observe_pre(
            event(
                mint,
                "BUY",
                created + 1_000_000,
                trader=causal.E4_WALLET,
                creator=creator,
                sol=3.0,
            )
        )
        self.assertIsNone(self.runtime.decision(mint, created + 1_000_000))

    def test_equal_timestamp_intent_does_not_leak_into_same_timestamp(self):
        mint = "mint-pending"
        creator = "creator-pending"
        created = self.launch(mint, creator, 2.0)
        e4_buy = event(
            mint,
            "BUY",
            created + 20_000_000,
            trader=causal.E4_WALLET,
            creator=creator,
            sol=3.0,
        )
        self.runtime.observe_post(e4_buy)
        self.assertEqual(self.runtime.creator_attempts[creator], 0)
        self.runtime.observe_pre(
            event(
                "same-time",
                "CREATE",
                created + 20_000_000,
                trader="other",
                creator="other",
            )
        )
        self.assertEqual(self.runtime.creator_attempts[creator], 0)
        self.runtime.observe_pre(
            event(
                "later",
                "CREATE",
                created + 20_000_001,
                trader="other-2",
                creator="other-2",
            )
        )
        self.assertEqual(self.runtime.creator_attempts[creator], 1)
        self.assertEqual(self.runtime.creator_successes[creator], 1)

    def test_seeded_runtime_state_populates_whitelist_and_buyer_graph(self):
        payload = {
            "version": "e4-v12-causal-runtime-state-v1",
            "creators": {"known-creator": {"attempts": 5, "successes": 4, "failed_attempts": 1}},
            "buyers": {"known-buyer": {"attempts": 7, "successes": 5}},
            "creator_buyer_pairs": {"known-creator|known-buyer": 3},
            "signature_shapes": {"1|0": 9},
        }
        runtime = causal.CausalChoiceRuntime(self.model, payload)
        self.assertEqual(runtime.creator_attempts["known-creator"], 5)
        self.assertEqual(runtime.buyer_successes["known-buyer"], 5)
        self.assertEqual(runtime.creator_buyer_attempts["known-creator|known-buyer"], 3)

    def test_policy_entry_uses_stored_preimpact_decision(self):
        mint = "mint-policy"
        creator = "creator-policy"
        created = self.launch(mint, creator, 2.0)
        state = core.TokenState(mint)
        state.created_ns = created
        state.latest_ns = created + 11_000_000
        state.price_sol = 4.5e-8
        state.fdv_usd = 4_500.0
        old_runtime = causal.RUNTIME
        causal.RUNTIME = self.runtime
        try:
            policy = core.E4Policy(core.Settings(model_path=Path("missing-model.json")))
            accepted, _, fraction, reason, features = causal._entry_causal_v12(policy, state)
        finally:
            causal.RUNTIME = old_runtime
        self.assertTrue(accepted, reason)
        self.assertGreater(fraction, 0.0)
        self.assertEqual(features["v12_causal_preimpact"], 1.0)
        profile = v6._PROFILE_BY_MINT[mint]
        self.assertEqual(profile.family, causal.FAMILY)

    def test_confirmed_causal_position_mirrors_source_partial(self):
        mint = "mint-causal-exit"
        v6._PROFILE_BY_MINT[mint] = SimpleNamespace(family=causal.FAMILY)
        position = core.Position(
            position_id="position-causal-exit",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=self.base_ns,
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=4.5e-8,
            max_price=4.5e-8,
            last_price=4.5e-8,
            entry_signature="simulation",
        )
        state = core.TokenState(mint)
        state.created_ns = self.base_ns
        state.latest_ns = self.base_ns + 100_000_000
        state.price_sol = 5e-8
        source = SimpleNamespace(
            entry_tokens=1_000.0,
            remaining_tokens=700.0,
            fully_exited=False,
        )
        with patch.object(causal.PIPELINES, "e4_signal", return_value=source):
            policy = core.E4Policy(core.Settings(model_path=Path("missing-model.json")))
            action, fraction, reason = causal._exit_causal_v12(policy, position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30, places=8)


if __name__ == "__main__":
    unittest.main()
