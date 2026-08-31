from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v6

core = e4_hardening_v6.core


def event(
    event_id: int,
    kind: core.EventKind,
    *,
    mint: str = "mint",
    at_ns: int,
    trader: str | None = None,
    creator: str | None = None,
    sol: float = 0.0,
    price: float | None = None,
    fdv: float | None = 5_000.0,
    signature: str | None = None,
    slot: int = 1,
) -> core.Event:
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=at_ns,
        received_ns=at_ns,
        slot=slot,
        signature=signature or f"sig-{event_id}",
        trader=trader,
        creator=creator,
        sol_amount=sol,
        token_amount=1_000.0,
        price_sol=price,
        fdv_usd=fdv,
    )


def position(
    mint: str,
    *,
    entry_sol: float = 1.0,
    opened_ns: int | None = None,
    first_partial_done: bool = False,
    entry_price: float = 1.0,
) -> core.Position:
    return core.Position(
        position_id=f"position-{mint}",
        mint=mint,
        status=(
            core.PositionStatus.PARTIAL
            if first_partial_done
            else core.PositionStatus.OPEN
        ),
        opened_ns=opened_ns or time.time_ns(),
        entry_sol=entry_sol,
        tokens=1_000.0,
        remaining=700.0 if first_partial_done else 1_000.0,
        entry_price=entry_price,
        max_price=entry_price,
        last_price=entry_price,
        entry_signature="entry",
        first_partial_done=first_partial_done,
    )


class E4LikelyEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        e4_hardening_v6.PREARMED_MINTS.clear()
        e4_hardening_v6.CREATOR_PROFILES.clear()
        e4_hardening_v6.WALLET_PROFILES.clear()
        e4_hardening_v6._ENTRY_FRACTION_BY_MINT.clear()
        self.policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def test_public_capital_burst_is_an_entry_family(self) -> None:
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, at_ns=now, creator="creator", price=1.0), None)
        state.apply(event(2, core.EventKind.BUY, at_ns=now + 20_000_000, trader="creator", creator="creator", sol=2.0, price=1.01), None)
        state.apply(event(3, core.EventKind.BUY, at_ns=now + 60_000_000, trader="a", sol=3.0, price=1.05), None)
        state.apply(event(4, core.EventKind.BUY, at_ns=now + 100_000_000, trader="b", sol=3.0, price=1.09), None)
        state.apply(event(5, core.EventKind.BUY, at_ns=now + 160_000_000, trader="c", sol=4.5, price=1.15), None)

        accepted, score, fraction, reason, features = self.policy.entry(state)

        self.assertTrue(accepted, reason)
        self.assertIn("public_capital_burst", reason)
        self.assertGreaterEqual(score, 0.70)
        self.assertLessEqual(fraction, 0.04)
        self.assertEqual(features["noncreator_buyers"], 3.0)

    def test_known_creator_family_can_act_before_public_burst(self) -> None:
        e4_hardening_v6.CREATOR_PROFILES["creator"] = {"confidence": 0.96}
        now = time.time_ns()
        state = core.TokenState("known")
        state.apply(event(1, core.EventKind.CREATE, mint="known", at_ns=now, creator="creator", price=1.0), None)
        state.apply(event(2, core.EventKind.BUY, mint="known", at_ns=now + 10_000_000, trader="creator", creator="creator", sol=0.05, price=1.01), None)
        state.apply(event(3, core.EventKind.BUY, mint="known", at_ns=now + 30_000_000, trader="buyer", sol=0.10, price=1.04), None)

        accepted, score, fraction, reason, _ = self.policy.entry(state)

        self.assertTrue(accepted, reason)
        self.assertIn("elite_creator", reason)
        self.assertGreaterEqual(score, 0.90)
        self.assertGreaterEqual(fraction, 0.04)

    def test_prearmed_launch_can_act_without_public_flow(self) -> None:
        e4_hardening_v6.PREARMED_MINTS["prearmed"] = {"confidence": 1.0}
        now = time.time_ns()
        state = core.TokenState("prearmed")
        state.apply(event(1, core.EventKind.CREATE, mint="prearmed", at_ns=now, creator="creator", price=1.0), None)

        accepted, score, fraction, reason, _ = self.policy.entry(state)

        self.assertTrue(accepted, reason)
        self.assertIn("prearmed_launch", reason)
        self.assertGreaterEqual(score, 0.99)
        self.assertEqual(fraction, e4_hardening_v6.EMPIRICAL_MAX_RELATIVE_FRACTION)

    def test_random_launch_is_rejected(self) -> None:
        now = time.time_ns()
        state = core.TokenState("random")
        state.apply(event(1, core.EventKind.CREATE, mint="random", at_ns=now, creator="creator", price=1.0), None)
        state.apply(event(2, core.EventKind.BUY, mint="random", at_ns=now + 100_000_000, trader="creator", creator="creator", sol=0.05, price=1.01), None)

        accepted, _, fraction, reason, _ = self.policy.entry(state)

        self.assertFalse(accepted)
        self.assertEqual(fraction, 0.0)
        self.assertIn("no observed E4 entry family", reason)

    def test_sell_before_entry_is_always_rejected(self) -> None:
        e4_hardening_v6.PREARMED_MINTS["mint"] = 1.0
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, at_ns=now, creator="creator", price=1.0), None)
        state.apply(event(2, core.EventKind.SELL, at_ns=now + 10_000_000, trader="seller", sol=0.1, price=0.99), None)

        accepted, _, _, reason, _ = self.policy.entry(state)

        self.assertFalse(accepted)
        self.assertIn("sell appeared", reason)


class E4RelativeSizingTests(unittest.TestCase):
    def test_empirical_quantile_ladder_and_family_caps(self) -> None:
        self.assertEqual(e4_hardening_v6._relative_fraction(0.60, "seeded_acceleration"), 0.0075)
        self.assertEqual(e4_hardening_v6._relative_fraction(0.90, "public_capital_burst"), 0.025)
        self.assertEqual(e4_hardening_v6._relative_fraction(0.98, "elite_creator"), 0.08)
        self.assertEqual(
            e4_hardening_v6._relative_fraction(0.999, "prearmed_launch"),
            e4_hardening_v6.EMPIRICAL_MAX_RELATIVE_FRACTION,
        )

    def test_config_cannot_expand_above_empirical_cap(self) -> None:
        with patch.dict(os.environ, {"E4_MAX_RELATIVE_POSITION_FRACTION": "0.50"}):
            self.assertEqual(
                e4_hardening_v6._relative_fraction(0.999, "prearmed_launch"),
                e4_hardening_v6.EMPIRICAL_MAX_RELATIVE_FRACTION,
            )


class E4ExitFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        e4_hardening_v6._ENTRY_FRACTION_BY_MINT.clear()
        self.policy = core.E4Policy(core.Settings(model_path=Path("missing.json")))

    def state_at(self, mint: str, price: float, at_ns: int) -> core.TokenState:
        state = core.TokenState(mint)
        state.apply(event(1, core.EventKind.CREATE, mint=mint, at_ns=at_ns - 1_000_000, creator="creator", price=1.0), None)
        state.apply(event(2, core.EventKind.BUY, mint=mint, at_ns=at_ns, trader="buyer", sol=2.0, price=price), None)
        return state

    def test_normal_relative_size_uses_thirty_percent_first_partial(self) -> None:
        now = time.time_ns()
        trade = position("normal", opened_ns=now - 100_000_000)
        e4_hardening_v6._ENTRY_FRACTION_BY_MINT["normal"] = 0.0125
        state = self.state_at("normal", 1.10, now)

        with patch.object(core.time, "time_ns", return_value=now):
            action, fraction, reason = self.policy.exit(trade, state)

        self.assertEqual(action, "SELL_PARTIAL")
        self.assertEqual(fraction, 0.30)
        self.assertIn("normal", reason)

    def test_high_relative_size_uses_twenty_percent_first_partial(self) -> None:
        now = time.time_ns()
        trade = position("high", opened_ns=now - 100_000_000)
        e4_hardening_v6._ENTRY_FRACTION_BY_MINT["high"] = 0.04
        state = self.state_at("high", 1.16, now)

        with patch.object(core.time, "time_ns", return_value=now):
            action, fraction, reason = self.policy.exit(trade, state)

        self.assertEqual(action, "SELL_PARTIAL")
        self.assertEqual(fraction, 0.20)
        self.assertIn("high-conviction", reason)

    def test_confirmed_runner_is_not_killed_at_old_sixty_second_limit(self) -> None:
        now = time.time_ns()
        trade = position(
            "runner",
            opened_ns=now - 120_000_000_000,
            first_partial_done=True,
        )
        trade.max_price = 1.50
        state = self.state_at("runner", 1.50, now)

        with patch.object(core.time, "time_ns", return_value=now):
            action, fraction, _ = self.policy.exit(trade, state)

        self.assertEqual(action, "HOLD")
        self.assertEqual(fraction, 0.0)

    def test_runner_emergency_horizon_remains_bounded(self) -> None:
        now = time.time_ns()
        trade = position(
            "runner-old",
            opened_ns=now - 301_000_000_000,
            first_partial_done=True,
        )
        trade.max_price = 1.50
        state = self.state_at("runner-old", 1.50, now)

        with patch.object(core.time, "time_ns", return_value=now):
            action, fraction, reason = self.policy.exit(trade, state)

        self.assertEqual(action, "SELL_ALL")
        self.assertEqual(fraction, 1.0)
        self.assertIn("emergency horizon", reason)


if __name__ == "__main__":
    unittest.main()
