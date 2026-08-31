from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v6

core = e4_hardening_v6.core


def event(
    event_id: int,
    kind: core.EventKind,
    mint: str,
    at_ns: int,
    *,
    trader: str | None = None,
    creator: str | None = None,
    sol: float = 0.0,
    price: float = 0.000001,
    fdv: float = 5_000.0,
    signature: str | None = None,
) -> core.Event:
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
        signature=signature or f"sig-{event_id}",
    )


def public_burst_state(mint: str = "public-burst") -> core.TokenState:
    now = time.time_ns()
    creator = "creator"
    state = core.TokenState(mint)
    state.apply(event(1, core.EventKind.CREATE, mint, now, creator=creator, trader=creator, price=1e-6, fdv=3_000), None)
    state.apply(event(2, core.EventKind.BUY, mint, now, creator=creator, trader=creator, sol=2.5, price=1.12e-6, fdv=3_400, signature="create"), None)
    state.apply(event(3, core.EventKind.BUY, mint, now + 20_000_000, trader="a", sol=2.0, price=1.25e-6, fdv=4_000), None)
    state.apply(event(4, core.EventKind.BUY, mint, now + 35_000_000, trader="b", sol=2.0, price=1.42e-6, fdv=4_600), None)
    state.apply(event(5, core.EventKind.BUY, mint, now + 50_000_000, trader="c", sol=2.0, price=1.60e-6, fdv=5_200), None)
    return state


class EntryModelTests(unittest.TestCase):
    def setUp(self) -> None:
        e4_hardening_v6._PROFILE_BY_MINT.clear()
        e4_hardening_v6._CONTEXT_BY_MINT.clear()

    def test_public_capital_burst_is_rejected_by_current_identity_policy(self) -> None:
        state = public_burst_state()
        accepted, _, _, reason, features = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertIn("identity", reason.lower())
        self.assertGreaterEqual(features["creator_buy_sol"], 2.5)

    def test_unseeded_bot_spray_is_rejected(self) -> None:
        now = time.time_ns()
        state = core.TokenState("spray")
        state.apply(event(1, core.EventKind.CREATE, "spray", now, creator="creator", trader="creator", fdv=3_000), None)
        for index in range(2, 11):
            state.apply(
                event(
                    index,
                    core.EventKind.BUY,
                    "spray",
                    now + index * 1_000_000,
                    trader=f"buyer-{index}",
                    sol=2.0,
                    price=1e-6 * (1 + index * 0.1),
                    fdv=5_000,
                ),
                None,
            )
        accepted, _, _, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertIn("creator seed", reason)

    def test_explicit_prearmed_launch_can_act_on_tiny_public_flow(self) -> None:
        now = time.time_ns()
        mint = "prearmed"
        creator = "known-creator"
        e4_hardening_v6._CONTEXT_BY_MINT[mint] = {
            "prearmed": True,
            "creator": creator,
            "metadata_host": "metadata.j7tracker.io",
        }
        state = core.TokenState(mint)
        state.apply(event(1, core.EventKind.CREATE, mint, now, creator=creator, trader=creator, fdv=3_000), None)
        state.apply(event(2, core.EventKind.BUY, mint, now + 20_000_000, creator=creator, trader=creator, sol=0.05, price=1.05e-6, fdv=3_500), None)
        accepted, score, fraction, reason, _ = core.E4Policy(
            core.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertTrue(accepted, reason)
        self.assertGreaterEqual(score, 0.93)
        self.assertGreaterEqual(fraction, 0.05)
        self.assertIn("authorized_prearmed_launch", reason)

    def test_relative_size_ladder_is_discrete_and_capped(self) -> None:
        cases = [
            (0.73, "probe", 0.0075),
            (0.78, "standard", 0.0125),
            (0.85, "strong", 0.0185),
            (0.90, "high", 0.03),
            (0.95, "elite", 0.05),
            (0.99, "exceptional", 0.10),
        ]
        for score, expected_tier, expected_fraction in cases:
            tier, fraction = e4_hardening_v6.relative_fraction_for_score(score)
            self.assertEqual(tier, expected_tier)
            self.assertAlmostEqual(fraction, expected_fraction)
        self.assertLessEqual(max(e4_hardening_v6._TIER_FRACTIONS.values()), 0.10)


class ExitFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        e4_hardening_v6._PROFILE_BY_MINT.clear()
        e4_hardening_v6._v6_partial_requested_ns.clear()

    def _position_and_state(self, mint: str, tier: str, price_multiple: float) -> tuple[core.Position, core.TokenState]:
        now = time.time_ns()
        state = core.TokenState(mint)
        state.apply(event(1, core.EventKind.CREATE, mint, now, creator="creator", trader="creator", price=1e-6, fdv=3_000), None)
        state.apply(event(2, core.EventKind.BUY, mint, now, creator="creator", trader="creator", sol=2, price=1e-6, fdv=3_200), None)
        state.apply(event(3, core.EventKind.BUY, mint, now + 10_000_000, trader="buyer", sol=3, price=price_multiple * 1e-6, fdv=5_000), None)
        fraction = e4_hardening_v6._TIER_FRACTIONS[tier]
        e4_hardening_v6._PROFILE_BY_MINT[mint] = e4_hardening_v6.EntryProfile(
            family="test",
            tier=tier,
            fraction=fraction,
            score=0.95,
            first_partial_fraction=0.20 if tier in {"high", "elite", "exceptional"} else 0.30,
            features={},
        )
        position = core.Position(
            position_id=f"position-{mint}",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=now,
            entry_sol=1.0,
            tokens=1_000,
            remaining=1_000,
            entry_price=1e-6,
            max_price=1e-6,
            last_price=1e-6,
            entry_signature="entry",
        )
        return position, state

    def test_standard_tier_takes_thirty_percent(self) -> None:
        position, state = self._position_and_state("standard-partial", "standard", 1.10)
        with patch.object(core.time, "time_ns", return_value=state.latest_ns):
            action, fraction, reason = core.E4Policy(core.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30)

    def test_high_tier_waits_for_and_takes_twenty_percent(self) -> None:
        position, state = self._position_and_state("high-partial", "high", 1.16)
        with patch.object(core.time, "time_ns", return_value=state.latest_ns):
            action, fraction, reason = core.E4Policy(core.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.20)

    def test_confirmed_runner_is_not_killed_at_legacy_sixty_seconds(self) -> None:
        position, state = self._position_and_state("long-runner", "high", 1.25)
        position.first_partial_done = True
        position.first_partial_fraction = 0.20
        position.opened_ns = time.time_ns() - 70_000_000_000
        position.max_price = state.price_sol or position.max_price
        with patch.object(core.time, "time_ns", return_value=time.time_ns()):
            action, _, reason = core.E4Policy(
                core.Settings(model_path=Path("missing.json"), max_hold_ms=60_000)
            ).exit(position, state)
        self.assertEqual(action, "HOLD", reason)
        self.assertIn("legacy horizon", reason)


class ProfilePersistenceTests(unittest.TestCase):
    def test_profile_table_round_trip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            settings = core.Settings(
                operational_db=Path(directory) / "operational.db",
                execution_db=Path(directory) / "execution.db",
                model_path=Path("missing.json"),
            )
            engine = core.Engine.__new__(core.Engine)
            engine.settings = settings
            engine.store = core.Store(settings.execution_db)
            try:
                e4_hardening_v6._PROFILE_BY_MINT["persisted"] = e4_hardening_v6.EntryProfile(
                    family="public_capital_burst",
                    tier="strong",
                    fraction=0.0185,
                    score=0.85,
                    first_partial_fraction=0.30,
                    features={"creator_buy_sol": 2.0},
                )
                e4_hardening_v6._ensure_profile_table(engine)
                e4_hardening_v6._persist_profile(engine, "persisted")
                e4_hardening_v6._PROFILE_BY_MINT.clear()
                e4_hardening_v6._ensure_profile_table(engine)
                restored = e4_hardening_v6._PROFILE_BY_MINT["persisted"]
                self.assertEqual(restored.tier, "strong")
                self.assertAlmostEqual(restored.fraction, 0.0185)
                self.assertAlmostEqual(restored.first_partial_fraction, 0.30)
            finally:
                engine.store.close()


if __name__ == "__main__":
    unittest.main()
