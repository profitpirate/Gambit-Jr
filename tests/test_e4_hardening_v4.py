from __future__ import annotations

import asyncio
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v4

core = e4_hardening_v4.core


class DecisionStore:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, str, dict]] = []

    def decision(
        self,
        mint: str,
        event_id: int | None,
        action: str,
        score: float | None,
        reason: str,
        payload: dict,
    ) -> None:
        self.decisions.append((mint, action, reason, dict(payload)))


class GuardianHarness:
    def __init__(self, *, failure_ms: int = 20, max_hold_ms: int = 1_000) -> None:
        self.settings = core.Settings(
            failure_window_ms=failure_ms,
            max_hold_ms=max_hold_ms,
            minimum_sol_inflow_1s=0.1,
            minimum_buy_sell_ratio=1.25,
            model_path=Path("missing-model.json"),
        )
        self.positions: dict[str, core.Position] = {}
        self.tokens: dict[str, core.TokenState] = {}
        self.pending_exits: set[str] = set()
        self.pending_entries: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.stop_event = asyncio.Event()
        self.store = DecisionStore()
        self.sell_calls: list[tuple[str, float, str]] = []
        self.active_sells = 0
        self.max_active_sells = 0
        self.failures_before_success: dict[str, int] = {}

        async def execute_sell(
            this: GuardianHarness,
            position: core.Position,
            fraction: float,
            reason: str,
        ) -> None:
            mint = position.mint
            this.sell_calls.append((mint, fraction, reason))
            this.active_sells += 1
            this.max_active_sells = max(this.max_active_sells, this.active_sells)
            try:
                await asyncio.sleep(0.01)
                remaining_failures = this.failures_before_success.get(mint, 0)
                if remaining_failures > 0:
                    this.failures_before_success[mint] = remaining_failures - 1
                    return
                position.status = core.PositionStatus.CLOSED
                position.remaining = 0.0
                this.positions.pop(mint, None)
            finally:
                this.active_sells -= 1
                this.pending_exits.discard(mint)

        self.execute_sell = types.MethodType(execute_sell, self)
        self.spawn = types.MethodType(core.Engine.spawn, self)

    def add_position(
        self,
        mint: str,
        *,
        age_ms: int,
        price: float = 1.0,
        first_partial: bool = False,
        latest_trade_age_ms: int | None = None,
    ) -> core.Position:
        now = time.time_ns()
        position = core.Position(
            position_id=f"position-{mint}",
            mint=mint,
            status=(
                core.PositionStatus.PARTIAL
                if first_partial
                else core.PositionStatus.OPEN
            ),
            opened_ns=now - age_ms * 1_000_000,
            entry_sol=0.05,
            tokens=1_000.0,
            remaining=700.0 if first_partial else 1_000.0,
            entry_price=1.0,
            max_price=max(1.0, price),
            last_price=price,
            entry_signature=f"entry-{mint}",
            first_partial_done=first_partial,
            first_partial_fraction=0.30 if first_partial else None,
        )
        state = core.TokenState(mint)
        state.price_sol = price
        state.fdv_usd = 5_000.0
        if latest_trade_age_ms is not None:
            event_time = now - latest_trade_age_ms * 1_000_000
            state.apply(
                core.Event(
                    event_id=1,
                    kind=core.EventKind.BUY,
                    mint=mint,
                    source_ns=event_time,
                    received_ns=event_time,
                    signature=f"trade-{mint}",
                    trader=f"buyer-{mint}",
                    sol_amount=0.2,
                    token_amount=100.0,
                    price_sol=price,
                    fdv_usd=5_000.0,
                ),
                None,
            )
        self.positions[mint] = position
        self.tokens[mint] = state
        return position


class IndependentGuardianTests(unittest.IsolatedAsyncioTestCase):
    async def run_guardian(self, engine: GuardianHarness, seconds: float = 0.12) -> None:
        task = asyncio.create_task(e4_hardening_v4._guardian_v4(engine))
        await asyncio.sleep(seconds)
        engine.stop_event.set()
        await asyncio.wait_for(task, timeout=1)
        if engine.tasks:
            await asyncio.gather(*tuple(engine.tasks), return_exceptions=True)

    async def test_quiet_unconfirmed_position_exits_without_new_market_event(self) -> None:
        engine = GuardianHarness(failure_ms=20, max_hold_ms=1_000)
        engine.add_position("quiet", age_ms=35, latest_trade_age_ms=35)
        with patch.dict(
            os.environ,
            {
                "E4_GUARDIAN_INTERVAL_SECONDS": "0.005",
                "E4_CONFIRMATION_QUIET_MS": "5",
            },
            clear=False,
        ):
            await self.run_guardian(engine)
        self.assertNotIn("quiet", engine.positions)
        self.assertEqual(len(engine.sell_calls), 1)
        self.assertIn("confirmation watchdog", engine.sell_calls[0][2])

    async def test_two_positions_liquidate_concurrently_and_exactly_once(self) -> None:
        engine = GuardianHarness(failure_ms=20, max_hold_ms=1_000)
        engine.add_position("one", age_ms=35, latest_trade_age_ms=35)
        engine.add_position("two", age_ms=35, latest_trade_age_ms=35)
        with patch.dict(
            os.environ,
            {
                "E4_GUARDIAN_INTERVAL_SECONDS": "0.005",
                "E4_CONFIRMATION_QUIET_MS": "5",
            },
            clear=False,
        ):
            await self.run_guardian(engine)
        self.assertEqual({mint for mint, _, _ in engine.sell_calls}, {"one", "two"})
        self.assertEqual(len(engine.sell_calls), 2)
        self.assertEqual(engine.max_active_sells, 2)
        self.assertFalse(engine.positions)

    async def test_failed_exit_is_retried_with_backoff_until_closed(self) -> None:
        engine = GuardianHarness(failure_ms=10, max_hold_ms=1_000)
        engine.add_position("retry", age_ms=30, latest_trade_age_ms=30)
        engine.failures_before_success["retry"] = 1
        with patch.dict(
            os.environ,
            {
                "E4_GUARDIAN_INTERVAL_SECONDS": "0.005",
                "E4_CONFIRMATION_QUIET_MS": "5",
                "E4_EXIT_RETRY_BASE_MS": "25",
                "E4_EXIT_RETRY_CAP_MS": "50",
            },
            clear=False,
        ):
            await self.run_guardian(engine, 0.18)
        self.assertNotIn("retry", engine.positions)
        self.assertEqual(len(engine.sell_calls), 2)
        self.assertEqual([row[3]["attempt"] for row in engine.store.decisions], [1, 2])

    async def test_absolute_horizon_closes_even_with_fresh_buy_flow(self) -> None:
        engine = GuardianHarness(failure_ms=20, max_hold_ms=25)
        engine.add_position("horizon", age_ms=40, latest_trade_age_ms=0)
        with patch.dict(os.environ, {"E4_GUARDIAN_INTERVAL_SECONDS": "0.005"}, clear=False):
            await self.run_guardian(engine)
        self.assertNotIn("horizon", engine.positions)
        self.assertIn("absolute hold horizon", engine.sell_calls[0][2])


class RapidMarketPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_rapid_partial_signal_is_rate_limited(self) -> None:
        settings = core.Settings(model_path=Path("missing-model.json"))
        policy = core.E4Policy(settings)
        now = time.time_ns()
        state = core.TokenState("rapid")
        state.apply(
            core.Event(
                event_id=1,
                kind=core.EventKind.BUY,
                mint="rapid",
                source_ns=now,
                received_ns=now,
                signature="buy",
                trader="buyer",
                sol_amount=1.0,
                token_amount=100.0,
                price_sol=1.10,
                fdv_usd=5_000.0,
            ),
            None,
        )
        position = core.Position(
            position_id="rapid-position",
            mint="rapid",
            status=core.PositionStatus.OPEN,
            opened_ns=now - 100_000_000,
            entry_sol=0.05,
            tokens=1_000.0,
            remaining=1_000.0,
            entry_price=1.0,
            max_price=1.10,
            last_price=1.10,
            entry_signature="entry",
        )
        with patch.dict(os.environ, {"E4_PARTIAL_COOLDOWN_MS": "25"}, clear=False):
            first = policy.exit(position, state)
            second = policy.exit(position, state)
            await asyncio.sleep(0.03)
            third = policy.exit(position, state)
        self.assertEqual(first[0], "SELL_PARTIAL")
        self.assertEqual(second[0], "HOLD")
        self.assertEqual(third[0], "SELL_PARTIAL")


class BurstAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_simultaneous_opportunities_open_at_most_two_positions(self) -> None:
        # Reuse the full execution fake used by the existing stress suite so
        # this test exercises allocation locking, persistence and token-delta
        # reconciliation rather than a toy counter.
        from tests.test_e4_stress_hardening import make_engine

        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 0.30)
            try:
                class AcceptAll:
                    @staticmethod
                    def entry(state: core.TokenState):
                        return True, 0.95, 0.20, "burst accepted", state.features()

                    @staticmethod
                    def exit(position: core.Position, state: core.TokenState):
                        return "HOLD", 0.0, "hold"

                engine.policy = AcceptAll()
                now = time.time_ns()
                events = [
                    core.Event(
                        event_id=index + 1,
                        kind=core.EventKind.CREATE,
                        mint=f"mint-{index}",
                        source_ns=now + index,
                        received_ns=now + index,
                        signature=f"create-{index}",
                        trader=f"creator-{index}",
                        price_sol=0.00005,
                        fdv_usd=4_878.0,
                    )
                    for index in range(64)
                ]
                await asyncio.gather(*(engine.on_event(event) for event in events))
                for _ in range(20):
                    if not engine.tasks:
                        break
                    await asyncio.gather(*tuple(engine.tasks), return_exceptions=True)
                    await asyncio.sleep(0)

                self.assertLessEqual(len(engine.positions), 2)
                self.assertEqual(len(engine.positions), 2)
                self.assertGreaterEqual(rpc.balance_sol, engine.settings.reserve_sol - 1e-9)
                self.assertEqual(engine.reserved_sol, 0.0)
                self.assertEqual(
                    engine.store.conn.execute(
                        "SELECT COUNT(*) FROM e4_seen_mints WHERE entry_count=1"
                    ).fetchone()[0],
                    2,
                )
            finally:
                engine.store.close()


if __name__ == "__main__":
    unittest.main()
