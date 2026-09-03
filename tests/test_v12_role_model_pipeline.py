from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from memecoin_bot import e4_role_model_v12 as role_model
from memecoin_bot.e4_pipelines_v10 import CreatorProfile, CreatorSnapshot, CreatorTier, E4_WALLET

core = role_model.core
v12 = role_model.v12
PIPELINES = role_model.PIPELINES


def event(
    event_id: int,
    kind,
    mint: str,
    at_ns: int,
    *,
    creator: str | None = None,
    trader: str | None = None,
    sol: float = 0.0,
    tokens: float = 1_000.0,
    price: float = 4.5e-8,
    fdv: float = 4_500.0,
):
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        creator=creator,
        trader=trader,
        sol_amount=sol,
        token_amount=tokens,
        price_sol=price,
        fdv_usd=fdv,
        signature=f"sig-{mint}-{event_id}",
    )


def apply(state, row) -> None:
    role_model.observe_market_event(row)
    state.apply(row, None)


def launch_state(
    mint: str,
    creator: str,
    *,
    age_ms: float,
    seed_sol: float,
    profile_fdv: float = 4_500.0,
):
    created_ns = time.time_ns()
    state = core.TokenState(mint)
    apply(
        state,
        event(
            1,
            core.EventKind.CREATE,
            mint,
            created_ns,
            creator=creator,
            trader=creator,
            fdv=2_785.0,
        ),
    )
    apply(
        state,
        event(
            2,
            core.EventKind.BUY,
            mint,
            created_ns + int(age_ms * 1_000_000),
            trader=creator,
            sol=seed_sol,
            fdv=profile_fdv,
        ),
    )
    return state


class V12RoleModelPipelineTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = PIPELINES.creators._snapshot
        self.e4_entries = PIPELINES._e4_entries
        self.learning = dict(PIPELINES._learning)
        self.social = PIPELINES._social_by_ca
        v12.v6._CONTEXT_BY_MINT.clear()
        v12.v6._PROFILE_BY_MINT.clear()
        PIPELINES._e4_entries = MappingProxyType({})
        PIPELINES._learning.clear()
        PIPELINES._social_by_ca = MappingProxyType({})

    def tearDown(self):
        PIPELINES.creators._snapshot = self.snapshot
        PIPELINES._e4_entries = self.e4_entries
        PIPELINES._learning.clear()
        PIPELINES._learning.update(self.learning)
        PIPELINES._social_by_ca = self.social
        v12.v6._CONTEXT_BY_MINT.clear()
        v12.v6._PROFILE_BY_MINT.clear()

    def policy(self):
        return core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def profiles(self, *rows: CreatorProfile) -> None:
        PIPELINES.creators._snapshot = CreatorSnapshot(
            MappingProxyType({row.creator: row for row in rows}),
            time.time_ns(),
            "v12-role-model-test",
        )

    def test_direct_e4_buy_on_primary_event_path_authorizes_same_mint(self):
        mint = "mint-direct-copy"
        creator = "creator-direct-copy"
        state = launch_state(mint, creator, age_ms=0.5, seed_sol=2.8)
        e4_buy = event(
            3,
            core.EventKind.BUY,
            mint,
            state.latest_ns + 500_000,
            trader=E4_WALLET,
            sol=3.0,
            tokens=60_000_000.0,
            fdv=4_750.0,
        )
        apply(state, e4_buy)

        accepted, score, fraction, reason, features = self.policy().entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn(role_model.ROLE_MODEL_FAMILY, reason)
        self.assertEqual(features["v12_role_model_copy"], 1.0)
        self.assertGreaterEqual(score, 0.94)
        self.assertGreater(fraction, 0.0)
        signal = PIPELINES.e4_signal(mint)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signature, e4_buy.signature)

    def test_future_e4_signal_cannot_leak_into_an_earlier_decision(self):
        mint = "mint-future-copy"
        state = launch_state(mint, "creator-future-copy", age_ms=1.0, seed_sol=2.0)
        PIPELINES.observe_e4_entry(
            {
                "mint": mint,
                "observed_ns": state.latest_ns + 100_000_000,
                "entry_price_sol": state.price_sol,
                "entry_sol": 3.0,
                "token_amount": 1_000.0,
                "signature": "future-e4-buy",
            }
        )
        accepted, _, _, reason, _ = self.policy().entry(state)
        self.assertFalse(accepted, reason)
        self.assertNotIn(role_model.ROLE_MODEL_FAMILY, reason)

    def test_non_e4_buy_does_not_activate_copy_pipeline(self):
        mint = "mint-not-e4"
        state = launch_state(mint, "creator-not-e4", age_ms=1.0, seed_sol=2.0)
        apply(
            state,
            event(
                3,
                core.EventKind.BUY,
                mint,
                state.latest_ns + 1_000_000,
                trader="ordinary-buyer",
                sol=3.0,
                fdv=4_900.0,
            ),
        )
        accepted, _, _, reason, _ = self.policy().entry(state)
        self.assertFalse(accepted, reason)
        self.assertIsNone(PIPELINES.e4_signal(mint))

    def test_copy_exit_mirrors_cumulative_e4_sells_and_full_exit(self):
        mint = "mint-copy-exit"
        creator = "creator-copy-exit"
        state = launch_state(mint, creator, age_ms=0.5, seed_sol=2.5)
        apply(
            state,
            event(
                3,
                core.EventKind.BUY,
                mint,
                state.latest_ns + 500_000,
                trader=E4_WALLET,
                sol=3.0,
                tokens=1_000.0,
                fdv=4_700.0,
            ),
        )
        accepted, _, _, reason, _ = self.policy().entry(state)
        self.assertTrue(accepted, reason)
        position = core.Position(
            position_id="position-copy-exit",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=state.price_sol,
            max_price=state.price_sol,
            last_price=state.price_sol,
            entry_signature="simulation",
        )

        e4_partial = event(
            4,
            core.EventKind.SELL,
            mint,
            state.latest_ns + 1_000_000,
            trader=E4_WALLET,
            sol=0.4,
            tokens=300.0,
            fdv=5_100.0,
        )
        apply(state, e4_partial)
        action, fraction, exit_reason = self.policy().exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL", exit_reason)
        self.assertAlmostEqual(fraction, 0.30, places=6)

        position.remaining = 700.0
        position.first_partial_done = True
        e4_full = event(
            5,
            core.EventKind.SELL,
            mint,
            state.latest_ns + 1_000_000,
            trader=E4_WALLET,
            sol=0.9,
            tokens=700.0,
            fdv=4_900.0,
        )
        apply(state, e4_full)
        action, fraction, exit_reason = self.policy().exit(position, state)
        self.assertEqual(action, "SELL_ALL", exit_reason)
        self.assertEqual(fraction, 1.0)

    def test_recent_live_e4_creator_plus_strong_current_seed_can_assist(self):
        now = time.time_ns()
        creator = "creator-recent-live-e4"
        self.profiles(
            CreatorProfile(
                creator,
                CreatorTier.APPROVED,
                0.84,
                wins=1,
                losses=0,
                trades=1,
                gross_win_rate=1.0,
                gross_pnl_sol=0.5,
                source="live-e4-teacher",
                updated_ns=now,
            )
        )
        state = launch_state("mint-recent-live-e4", creator, age_ms=350.0, seed_sol=1.5)
        accepted, _, _, reason, features = self.policy().entry(state)
        self.assertTrue(accepted, reason)
        self.assertIn("v12_recent_e4_repeat_launch", reason)
        self.assertEqual(features["v12_recent_e4_repeat"], 1.0)

    def test_static_single_prior_win_still_cannot_authorize(self):
        creator = "creator-static-one-win"
        self.profiles(
            CreatorProfile(
                creator,
                CreatorTier.APPROVED,
                0.84,
                wins=1,
                losses=0,
                trades=1,
                gross_win_rate=1.0,
                gross_pnl_sol=0.5,
                source="e4-history",
                updated_ns=time.time_ns(),
            )
        )
        state = launch_state("mint-static-one-win", creator, age_ms=350.0, seed_sol=1.5)
        accepted, _, _, reason, _ = self.policy().entry(state)
        self.assertFalse(accepted, reason)

    def test_profitable_historical_creator_uses_observed_400ms_horizon(self):
        creator = "creator-observed-horizon"
        self.profiles(
            CreatorProfile(
                creator,
                CreatorTier.ELITE,
                0.95,
                wins=5,
                losses=1,
                trades=6,
                gross_win_rate=5 / 6,
                gross_pnl_sol=2.0,
                source="e4-history",
                updated_ns=time.time_ns(),
            )
        )
        accepted_state = launch_state("mint-350ms", creator, age_ms=350.0, seed_sol=2.0)
        accepted, _, _, reason, _ = self.policy().entry(accepted_state)
        self.assertTrue(accepted, reason)

        rejected_state = launch_state("mint-401ms", creator, age_ms=401.0, seed_sol=2.0)
        accepted, _, _, reason, _ = self.policy().entry(rejected_state)
        self.assertFalse(accepted, reason)
        self.assertIn("outside early-entry horizon", reason)

    def test_runtime_derives_wallet_websocket_from_existing_rpc_configuration(self):
        clean = {
            "E4_PIPELINE_SOLANA_RPC_URLS": "",
            "E4_PIPELINE_SOLANA_WS_URLS": "",
            "E4_PRIMARY_RPC_URL": "https://example-rpc.invalid/path?key=test",
            "E4_FALLBACK_RPC_URLS": "https://fallback.invalid",
            "HELIUS_RPC_URL": "",
            "SOLANA_RPC_URL": "",
        }
        with patch.dict(os.environ, clean, clear=False):
            runtime = role_model.pipeline_runtime.PipelineRuntime()
        self.assertIn("https://example-rpc.invalid/path?key=test", runtime.rpc_urls)
        self.assertIn("wss://example-rpc.invalid/path?key=test", runtime.ws_urls)
        self.assertGreaterEqual(len(runtime.ws_urls), 1)

    def test_policy_module_is_pinned_by_hashed_v12_entrypoints(self):
        digest = role_model.policy_fingerprint()
        role_model.assert_policy_fingerprint(digest)
        entrypoint = Path("src/memecoin_bot/e4_exec/__main__.py").read_text(encoding="utf-8")
        holdout = Path("scripts/e4_300_launch_holdout_v12.py").read_text(encoding="utf-8")
        self.assertIn(digest, entrypoint)
        self.assertIn(digest, holdout)


if __name__ == "__main__":
    unittest.main()
