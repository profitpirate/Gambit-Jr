from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v6
from memecoin_bot import e4_v6_state as s

core = e4_hardening_v6.core


def event(event_id: int, kind: core.EventKind, t: int, *, price: float = 0.001,
          trader: str | None = None, sol: float = 0.0, creator: str = "dev") -> core.Event:
    return core.Event(
        event_id=event_id, kind=kind, mint="mint", source_ns=t, received_ns=t,
        signature=f"sig-{event_id}", trader=trader, sol_amount=sol,
        token_amount=1000.0, price_sol=price, fdv_usd=6000.0, creator=creator,
        virtual_sol=35_000_000_000, virtual_tokens=900_000_000_000_000,
        real_sol=5_000_000_000, real_tokens=700_000_000_000_000,
    )


class V6SizingTests(unittest.TestCase):
    def test_relative_size_ladder_matches_observed_e4_confidence_bands(self) -> None:
        self.assertEqual(s.size_tier(0.70), ("BASE", 0.0075))
        self.assertEqual(s.size_tier(0.75), ("NORMAL", 0.0125))
        self.assertEqual(s.size_tier(0.81), ("STRONG", 0.0185))
        self.assertEqual(s.size_tier(0.87), ("HIGH", 0.0300))
        self.assertEqual(s.size_tier(0.92), ("VERY_HIGH", 0.0480))
        self.assertEqual(s.size_tier(0.97), ("ELITE", 0.1000))
        self.assertLessEqual(max(value for _, _, value in s.SIZE_LADDER), s.MAX_POSITION_FRACTION)


class V6PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        s.CREATOR_PROFILES.clear()
        s.FUNDER_BY_CREATOR.clear()
        s.POLICY_BY_MINT.clear()
        self.settings = core.Settings(model_path=Path("missing.json"), max_position_fraction=0.20)
        self.policy = core.E4Policy(self.settings)

    def test_public_burst_does_not_require_bundle_signature_pattern(self) -> None:
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, now, trader="dev", sol=0), None)
        state.apply(event(2, core.EventKind.BUY, now + 10_000_000, trader="dev", sol=2.0, price=0.00105), None)
        for index in range(3, 7):
            state.apply(event(index, core.EventKind.BUY, now + index * 20_000_000,
                              trader=f"buyer-{index}", sol=2.0, price=0.00125 + index * 0.00002), None)
        accepted, score, fraction, reason, _ = self.policy.entry(state)
        self.assertTrue(accepted, reason)
        self.assertGreaterEqual(score, 0.74)
        self.assertIn(fraction, {0.0125, 0.0185, 0.03, 0.048, 0.10})

    def test_cached_positive_creator_can_take_identity_fast_path(self) -> None:
        s.CREATOR_PROFILES["dev"] = {
            "quality": "PROVEN", "launches": 20, "survived": 15,
            "rugs": 1, "runners": 8, "runner_rate": 0.4, "rug_rate": 0.05,
        }
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, now), None)
        state.apply(event(2, core.EventKind.BUY, now + 20_000_000, trader="dev", sol=0.2, price=0.00101), None)
        accepted, score, _, reason, features = self.policy.entry(state)
        self.assertTrue(accepted, reason)
        self.assertEqual(features["creator_quality"], "PROVEN")
        self.assertGreaterEqual(score, 0.76)

    def test_toxic_creator_is_hard_rejected(self) -> None:
        s.CREATOR_PROFILES["dev"] = {
            "quality": "TOXIC", "launches": 20, "survived": 1,
            "rugs": 18, "runners": 0, "runner_rate": 0.0, "rug_rate": 0.9,
        }
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, now), None)
        state.apply(event(2, core.EventKind.BUY, now + 10_000_000, trader="dev", sol=3.0, price=0.0011), None)
        for index in range(3, 8):
            state.apply(event(index, core.EventKind.BUY, now + index * 10_000_000,
                              trader=f"b{index}", sol=2.0, price=0.0014), None)
        accepted, _, _, reason, _ = self.policy.entry(state)
        self.assertFalse(accepted)
        self.assertIn("creator/funder", reason)

    def test_confidence_tier_overrides_first_partial_fraction(self) -> None:
        now = time.time_ns()
        state = core.TokenState("mint")
        state.apply(event(1, core.EventKind.CREATE, now), None)
        state.apply(event(2, core.EventKind.BUY, now + 1_000_000, trader="dev", sol=2.0), None)
        state.apply(event(3, core.EventKind.BUY, now + 2_000_000, trader="b", sol=3.0, price=0.0013), None)
        position = core.Position(
            position_id="p", mint="mint", status=core.PositionStatus.OPEN,
            opened_ns=now, entry_sol=6.0, tokens=1000, remaining=1000,
            entry_price=0.001, max_price=0.001, last_price=0.001,
            entry_signature="entry",
        )
        s.POLICY_BY_MINT["mint"] = {"first_partial_fraction": 0.20, "tier": "HIGH"}
        with patch.object(core.time, "time_ns", return_value=now + 50_000_000):
            action, fraction, _ = self.policy.exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL")
        self.assertAlmostEqual(fraction, 0.20)


class V6RuntimeTests(unittest.TestCase):
    def test_engine_guardrails_cap_relative_size_and_allow_observed_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = core.Settings(
                operational_db=Path(tmp) / "ops.db",
                execution_db=Path(tmp) / "e4.db",
                model_path=Path(tmp) / "missing.json",
                max_position_fraction=0.20,
                max_hold_ms=60_000,
            )
            # Minimal operational DB is optional for identity cache.
            engine = core.Engine(settings)
            try:
                self.assertLessEqual(engine.settings.max_position_fraction, 0.115)
                self.assertGreaterEqual(engine.settings.max_hold_ms, 300_000)
            finally:
                engine.store.close()

    def test_fee_policy_is_expensive_on_buy_and_cheap_on_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = core.Settings(
                operational_db=Path(tmp) / "ops.db",
                execution_db=Path(tmp) / "e4.db",
                model_path=Path(tmp) / "missing.json",
            )
            engine = core.Engine(settings)
            try:
                buy = engine.fee_bid(2.0, 0.9)
                from memecoin_bot import e4_v6_runtime as runtime
                token = runtime._fee_side.set("SELL")
                try:
                    sell = engine.fee_bid(2.0, 1.0)
                finally:
                    runtime._fee_side.reset(token)
                self.assertGreater(sum(buy), 0.05)
                self.assertLess(sum(sell), 0.002)
            finally:
                engine.store.close()


class V6RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_headers_keep_unsuffixed_name_and_fast_confirmation(self) -> None:
        class Sender:
            settings = type("Settings", (), {"confirmation_timeout_seconds": 1.0})()
            routes = [("helius", "h"), ("nozomi", "n")]
            names: list[str] = []
            checks = 0

            async def _send(self, index, name, url, tx, signature):
                self.names.append(name)
                await asyncio.sleep(0.001 if name == "helius" else 0.2)
                return type("Result", (), {
                    "name": name, "accepted": True, "error": None,
                    "submitted_ns": time.time_ns(), "completed_ns": time.time_ns(),
                    "result": signature,
                })()

            async def _status(self, signature):
                self.checks += 1
                return (self.checks >= 2, 123 if self.checks >= 2 else None, None)

        sender = Sender()
        started = time.perf_counter()
        route, confirmed, slot, _, _ = await core.RouteSender.submit(sender, "tx", "sig")
        elapsed = time.perf_counter() - started
        self.assertTrue(confirmed)
        self.assertEqual(route, "helius")
        self.assertEqual(slot, 123)
        self.assertTrue(all("#" not in name for name in sender.names))
        self.assertLess(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
