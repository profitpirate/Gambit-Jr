from __future__ import annotations

import math
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_sequential_hazard_v12 as sequential

core = sequential.core
v6 = sequential.v6


def logistic_payload(*, threshold=0.55, margin=0.0, cooldown=0.0):
    return {
        "version": "e4-v12-sequential-hazard-v1",
        "status": "LIVE_HOLDOUT_CONFIRMED",
        "horizon_ms": 500.0,
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2_750.0,
            "maximum_fdv_usd": 10_000.0,
            "maximum_age_ms": 1_500.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
        },
        "gate": {
            "threshold": threshold,
            "minimum_probability_margin": margin,
            "cooldown_ms": cooldown,
            "require_identity_top": False,
            "require_seed_or_velocity_top": False,
        },
        "model": {
            "kind": "logistic",
            "features": ["log_seed"],
            "mean": [0.0],
            "scale": [1.0],
            "coefficient": [2.0],
            "intercept": -1.0,
        },
    }


def tree_payload():
    payload = logistic_payload()
    payload["model"] = {
        "kind": "tree_ensemble",
        "features": ["log_seed"],
        "trees": [
            {
                "leaf": False,
                "feature_index": 0,
                "threshold": 0.5,
                "left": {"leaf": True, "probability": 0.10},
                "right": {"leaf": True, "probability": 0.90},
            },
            {
                "leaf": False,
                "feature_index": 0,
                "threshold": 1.0,
                "left": {"leaf": True, "probability": 0.30},
                "right": {"leaf": True, "probability": 0.80},
            },
        ],
    }
    return payload


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
    event_index: int = 0,
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
        event_index=event_index,
        raw={"transaction_index": event_index},
    )


class SequentialHazardModelTests(unittest.TestCase):
    def test_unconfirmed_model_is_rejected(self):
        payload = logistic_payload()
        payload["status"] = "NOT_CONCLUSIVE"
        with self.assertRaises(ValueError):
            sequential.SequentialHazardModel(payload)

    def test_logistic_probability_matches_equation(self):
        model = sequential.SequentialHazardModel(logistic_payload())
        value = model.probability({"log_seed": 1.0})
        self.assertAlmostEqual(value, 1.0 / (1.0 + math.exp(-1.0)), places=9)

    def test_tree_ensemble_probability_is_mean(self):
        model = sequential.SequentialHazardModel(tree_payload())
        self.assertAlmostEqual(model.probability({"log_seed": 2.0}), 0.85, places=9)
        self.assertAlmostEqual(model.probability({"log_seed": 0.0}), 0.20, places=9)


class SequentialHazardRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.enabled = patch.object(sequential, "ENABLED", True)
        self.enabled.start()
        self.model = sequential.SequentialHazardModel(logistic_payload())
        self.runtime = sequential.SequentialHazardRuntime(self.model, {})
        self.now = time.time_ns()
        v6._PROFILE_BY_MINT.clear()
        v6._CONTEXT_BY_MINT.clear()

    def tearDown(self):
        self.enabled.stop()
        v6._PROFILE_BY_MINT.clear()
        v6._CONTEXT_BY_MINT.clear()

    def launch(self, mint: str, creator: str, seed: float, offset_ms: float = 0.0):
        created = self.now + int(offset_ms * 1e6)
        self.runtime.observe_pre(
            event(
                mint,
                "CREATE",
                created,
                trader=creator,
                creator=creator,
                signature=f"create-{mint}",
            )
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
                event_index=1,
            )
        )
        return created

    def test_creator_seed_event_can_trigger_before_e4(self):
        created = self.launch("mint-a", "creator-a", 2.0)
        decision = self.runtime.decision("mint-a", created + 11_000_000)
        self.assertIsNotNone(decision)
        self.assertGreater(decision.probability, self.model.threshold)

    def test_current_e4_buy_cannot_create_preintent_decision(self):
        mint = "mint-e4"
        creator = "creator-e4"
        self.runtime.observe_pre(event(mint, "CREATE", self.now, trader=creator, creator=creator))
        self.runtime.observe_pre(
            event(
                mint,
                "BUY",
                self.now + 1_000_000,
                trader=sequential.E4_WALLET,
                creator=creator,
                sol=8.0,
            )
        )
        self.assertIsNone(self.runtime.decision(mint, self.now + 2_000_000))

    def test_pre_entry_sell_revokes_decision(self):
        created = self.launch("mint-sold", "creator-sold", 2.0)
        self.assertIsNotNone(self.runtime.decision("mint-sold", created + 11_000_000))
        self.runtime.observe_pre(
            event(
                "mint-sold",
                "SELL",
                created + 12_000_000,
                trader="seller",
                event_index=2,
            )
        )
        self.assertIsNone(self.runtime.decision("mint-sold", created + 13_000_000))

    def test_probability_margin_blocks_near_twin(self):
        model = sequential.SequentialHazardModel(
            logistic_payload(threshold=0.50, margin=0.20)
        )
        runtime = sequential.SequentialHazardRuntime(model, {})
        self.runtime = runtime
        first = self.launch("mint-first", "creator-first", 2.0, 0.0)
        second = self.launch("mint-second", "creator-second", 2.05, 1.0)
        # Similar probabilities leave too little margin for the second launch.
        self.assertIsNone(runtime.decision("mint-second", second + 11_000_000))
        self.assertIsNotNone(runtime.decision("mint-first", first + 11_000_000))

    def test_equal_timestamp_intent_updates_only_after_next_timestamp(self):
        mint = "mint-history"
        creator = "creator-history"
        created = self.launch(mint, creator, 2.0)
        source = event(
            mint,
            "BUY",
            created + 20_000_000,
            trader=sequential.E4_WALLET,
            creator=creator,
            sol=3.0,
            signature="e4-source",
        )
        self.runtime.observe_post(source)
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

    def test_duplicate_source_signature_is_counted_once(self):
        mint = "mint-dedupe"
        creator = "creator-dedupe"
        created = self.launch(mint, creator, 2.0)
        source = event(
            mint,
            "BUY",
            created + 20_000_000,
            trader=sequential.E4_WALLET,
            creator=creator,
            sol=3.0,
            signature="same-e4-source",
        )
        self.runtime.observe_post(source)
        self.runtime.observe_post(source)
        self.runtime.observe_pre(
            event("later-dedupe", "CREATE", created + 20_000_001, trader="x", creator="x")
        )
        self.assertEqual(self.runtime.creator_attempts[creator], 1)

    def test_seeded_identity_state_is_loaded(self):
        payload = {
            "version": "e4-v12-causal-runtime-state-v1",
            "creators": {"known": {"attempts": 6, "successes": 4, "failed_attempts": 2}},
            "buyers": {"buyer": {"attempts": 5, "successes": 3}},
            "creator_buyer_pairs": {"known|buyer": 2},
            "signature_shapes": {"1|0": 8},
        }
        runtime = sequential.SequentialHazardRuntime(self.model, payload)
        self.assertEqual(runtime.creator_attempts["known"], 6)
        self.assertEqual(runtime.buyer_successes["buyer"], 3)
        self.assertEqual(runtime.pair_attempts["known|buyer"], 2)

    def test_policy_uses_preintent_decision(self):
        mint = "mint-policy"
        creator = "creator-policy"
        created = self.launch(mint, creator, 2.0)
        state = core.TokenState(mint)
        state.created_ns = created
        state.latest_ns = created + 11_000_000
        state.price_sol = 4.5e-8
        state.fdv_usd = 4_500.0
        old_runtime = sequential.RUNTIME
        sequential.RUNTIME = self.runtime
        try:
            policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))
            accepted, _, fraction, reason, features = sequential._entry_sequential_v12(policy, state)
        finally:
            sequential.RUNTIME = old_runtime
        self.assertTrue(accepted, reason)
        self.assertGreater(fraction, 0.0)
        self.assertEqual(features["v12_sequential_preintent"], 1.0)
        self.assertEqual(v6._PROFILE_BY_MINT[mint].family, sequential.FAMILY)

    def test_confirmed_position_mirrors_e4_partial(self):
        mint = "mint-exit"
        v6._PROFILE_BY_MINT[mint] = SimpleNamespace(family=sequential.FAMILY)
        position = core.Position(
            position_id="p",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=self.now,
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=4.5e-8,
            max_price=4.5e-8,
            last_price=4.5e-8,
            entry_signature="simulation",
        )
        state = core.TokenState(mint)
        state.created_ns = self.now
        state.latest_ns = self.now + 100_000_000
        state.price_sol = 5e-8
        source = SimpleNamespace(
            entry_tokens=1_000.0,
            remaining_tokens=700.0,
            fully_exited=False,
        )
        with patch.object(sequential.PIPELINES, "e4_signal", return_value=source):
            policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))
            action, fraction, reason = sequential._exit_sequential_v12(policy, position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30, places=8)


if __name__ == "__main__":
    unittest.main()
