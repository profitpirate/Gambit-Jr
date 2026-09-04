from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_copy_fidelity_v12 as fidelity
from memecoin_bot.e4_pipeline_manager_v11 import E4Signal

core = fidelity.core
PIPELINES = fidelity.PIPELINES
v6 = fidelity.v6


class V12CopyFidelityTests(unittest.TestCase):
    def setUp(self):
        self.entries = PIPELINES._e4_entries
        self.profiles = dict(v6._PROFILE_BY_MINT)
        PIPELINES._e4_entries = MappingProxyType({})
        v6._PROFILE_BY_MINT.clear()

    def tearDown(self):
        PIPELINES._e4_entries = self.entries
        v6._PROFILE_BY_MINT.clear()
        v6._PROFILE_BY_MINT.update(self.profiles)

    def _policy(self):
        return core.E4Policy(core.Settings(model_path=Path("missing-model.json")))

    def _state(self, mint: str, price: float, latest_ns: int):
        state = core.TokenState(mint)
        state.created_ns = latest_ns - 1_000_000_000
        state.latest_ns = latest_ns
        state.price_sol = price
        state.fdv_usd = 4_500.0
        return state

    def _position(self, mint: str, entry_price: float = 1.0):
        return core.Position(
            position_id=f"position-{mint}",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns() - 1_000_000_000,
            entry_sol=1.0,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=entry_price,
            max_price=entry_price,
            last_price=entry_price,
            entry_signature="simulation",
        )

    def _source(self, mint: str, observed_ns: int, remaining: float = 1_000.0, *, fully_exited: bool = False):
        return E4Signal(
            mint=mint,
            creator="creator",
            observed_ns=observed_ns,
            entry_price_sol=1.0,
            entry_sol=3.0,
            signature="e4-entry",
            entry_tokens=1_000.0,
            remaining_tokens=remaining,
            last_sell_fraction=0.0,
            last_sell_ns=0,
            last_sell_signature="",
            sell_count=0,
            fully_exited=fully_exited,
            sold=fully_exited,
        )

    def _install_direct(self, mint: str, source: E4Signal):
        v6._PROFILE_BY_MINT[mint] = SimpleNamespace(family=fidelity.role_model.ROLE_MODEL_FAMILY)
        PIPELINES._e4_entries = MappingProxyType({mint: source})

    def test_worse_copy_entry_cannot_trigger_independent_failure_exit(self):
        mint = "mint-hold-through-copy-basis"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed))
        position = self._position(mint, entry_price=1.0)
        state = self._state(mint, price=0.50, latest_ns=observed + 1_000_000_000)
        action, fraction, reason = self._policy().exit(position, state)
        self.assertEqual(action, "HOLD", reason)
        self.assertEqual(fraction, 0.0)
        self.assertIn("awaiting source exit", reason)

    def test_first_e4_partial_is_mirrored_as_thirty_percent(self):
        mint = "mint-partial-copy"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed, remaining=700.0))
        position = self._position(mint)
        state = self._state(mint, price=1.20, latest_ns=observed + 1_000_000)
        action, fraction, reason = self._policy().exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30, places=9)

    def test_after_matching_partial_v12_holds_until_e4_sells_again(self):
        mint = "mint-hold-after-partial"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed, remaining=700.0))
        position = self._position(mint)
        position.remaining = 700.0
        position.first_partial_done = True
        state = self._state(mint, price=0.50, latest_ns=observed + 2_000_000_000)
        action, fraction, reason = self._policy().exit(position, state)
        self.assertEqual(action, "HOLD", reason)
        self.assertEqual(fraction, 0.0)

    def test_token_accounted_multileg_exit_is_not_collapsed_after_second_sell(self):
        mint = "mint-four-leg-source"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed))

        first = PIPELINES.observe_e4_exit(
            mint,
            token_amount=300.0,
            observed_ns=observed + 1_000_000,
            signature="sell-1",
        )
        self.assertFalse(first.fully_exited)
        self.assertAlmostEqual(first.remaining_tokens, 700.0)

        # This is 50% of the remaining position. The previous manager heuristic
        # incorrectly declared a full exit here and erased later E4 sell legs.
        second = PIPELINES.observe_e4_exit(
            mint,
            token_amount=350.0,
            observed_ns=observed + 2_000_000,
            signature="sell-2",
        )
        self.assertFalse(second.fully_exited)
        self.assertAlmostEqual(second.remaining_tokens, 350.0)

        third = PIPELINES.observe_e4_exit(
            mint,
            token_amount=200.0,
            observed_ns=observed + 3_000_000,
            signature="sell-3",
        )
        self.assertFalse(third.fully_exited)
        self.assertAlmostEqual(third.remaining_tokens, 150.0)

        fourth = PIPELINES.observe_e4_exit(
            mint,
            token_amount=150.0,
            observed_ns=observed + 4_000_000,
            signature="sell-4",
        )
        self.assertTrue(fourth.fully_exited)
        self.assertEqual(fourth.remaining_tokens, 0.0)
        self.assertEqual(fourth.sell_count, 4)

    def test_e4_full_exit_remains_authoritative(self):
        mint = "mint-full-copy"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed, remaining=0.0, fully_exited=True))
        position = self._position(mint)
        state = self._state(mint, price=1.10, latest_ns=observed + 2_000_000)
        action, fraction, reason = self._policy().exit(position, state)
        self.assertEqual(action, "SELL_ALL", reason)
        self.assertEqual(fraction, 1.0)

    def test_source_staleness_is_the_independent_fail_safe(self):
        mint = "mint-stale-source"
        observed = time.time_ns()
        self._install_direct(mint, self._source(mint, observed))
        position = self._position(mint)
        state = self._state(mint, price=1.0, latest_ns=observed + 66_000_000_000)
        action, fraction, reason = self._policy().exit(position, state)
        self.assertEqual(action, "SELL_ALL", reason)
        self.assertEqual(fraction, 1.0)
        self.assertIn("stale", reason)

    def test_allenhark_relay_is_optional_and_prepended_when_configured(self):
        settings = SimpleNamespace(
            route_urls={"other": "https://other.invalid"},
            direct_rpc_route=True,
            rpc_url="https://rpc.invalid",
            route_headers={},
            route_stagger_ms=8,
            confirmation_timeout_seconds=1.0,
        )
        with patch.dict(
            os.environ,
            {"E4_ALLENHARK_RELAY_URL": "https://fra.relay.allenhark.com/v1/sendTx"},
            clear=False,
        ):
            sender = fidelity.FastPersistentRouteSender(settings, SimpleNamespace())
        self.assertEqual(sender.routes[0][0], "allenhark_relay")
        self.assertEqual(sender._relay_keepalive_url, "https://fra.relay.allenhark.com/keepalive")

    def test_production_and_replay_pin_copy_fidelity_module(self):
        digest = fidelity.policy_fingerprint()
        fidelity.assert_policy_fingerprint(digest)
        entrypoint = Path("src/memecoin_bot/e4_exec/__main__.py").read_text(encoding="utf-8")
        holdout = Path("scripts/e4_300_launch_holdout_v12.py").read_text(encoding="utf-8")
        replay = Path("scripts/e4_v12_direct_copy_replay.py").read_text(encoding="utf-8")
        self.assertIn(digest, entrypoint)
        self.assertIn(digest, holdout)
        self.assertIn(digest, replay)
        self.assertIs(type(PIPELINES).observe_e4_exit, fidelity._observe_e4_exit_copy_fidelity_v12)
        self.assertIs(core.E4Policy.exit, fidelity._exit_copy_fidelity_v12)
        self.assertIs(core.RouteSender, fidelity.FastPersistentRouteSender)


if __name__ == "__main__":
    unittest.main()
