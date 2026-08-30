from __future__ import annotations

import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path

from memecoin_bot import e4_hardening_v4 as v4
from tests.test_e4_stress_hardening import make_engine

core = v4.core


async def drain(engine: core.Engine, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = tuple(engine.tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not engine.tasks and not engine.pending_entries and not engine.pending_exits:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("E4 tasks did not drain")


def position(mint: str, opened_ns: int, *, partial: bool = False) -> core.Position:
    return core.Position(
        position_id=f"position-{mint}",
        mint=mint,
        status=core.PositionStatus.PARTIAL if partial else core.PositionStatus.OPEN,
        opened_ns=opened_ns,
        entry_sol=0.05,
        tokens=1_000.0,
        remaining=1_000.0,
        entry_price=0.00005,
        max_price=0.00005,
        last_price=0.00005,
        entry_signature=f"entry-{mint}",
        first_partial_done=partial,
        first_partial_fraction=0.30 if partial else None,
    )


class V4SelectionSafetyTests(unittest.TestCase):
    def test_live_engine_fails_closed_without_calibrated_selection_model(self) -> None:
        settings = core.Settings(
            live=True,
            model_path=Path("missing-selection-model.json"),
        )
        policy = core.E4Policy(settings)
        state = core.TokenState("mint")
        now = time.time_ns()
        state.created_ns = now
        state.latest_ns = now
        state.fdv_usd = 4_878
        accepted, score, fraction, reason, _ = policy.entry(state)
        self.assertFalse(accepted)
        self.assertEqual(score, 0.0)
        self.assertEqual(fraction, 0.0)
        self.assertIn("not calibrated", reason)


class V4IndependentGuardianTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_event_confirmation_timeout_closes_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 1.0)
            try:
                v4.ensure_runtime_state(engine)
                engine.settings.failure_window_ms = 20
                engine.settings.max_hold_ms = 1_000
                engine.policy = core.E4Policy(engine.settings)
                opened = time.time_ns() - 40_000_000
                item = position("quiet", opened)
                rpc.tokens[item.mint] = item.tokens
                engine.positions[item.mint] = item
                engine.store.save_position(item)
                state = core.TokenState(item.mint)
                state.created_ns = opened - 5_000_000
                state.latest_ns = opened - 1
                state.price_sol = item.entry_price
                state.fdv_usd = 4_878
                engine.tokens[item.mint] = state

                guardian = asyncio.create_task(v4._guardian_v4(engine))
                deadline = time.monotonic() + 1
                while item.mint in engine.positions and time.monotonic() < deadline:
                    await asyncio.sleep(0.005)
                engine.stop_event.set()
                await guardian
                await drain(engine)

                self.assertNotIn(item.mint, engine.positions)
                self.assertAlmostEqual(rpc.tokens[item.mint], 0.0, places=9)
                row = engine.store.conn.execute(
                    "SELECT reason FROM e4_orders WHERE mint=? AND side='SELL' "
                    "ORDER BY created_ns DESC LIMIT 1",
                    (item.mint,),
                ).fetchone()
                self.assertIn("confirmation window", str(row[0]).lower())
            finally:
                engine.store.close()

    async def test_absolute_horizon_closes_partial_position_without_market_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 1.0)
            try:
                v4.ensure_runtime_state(engine)
                engine.settings.failure_window_ms = 10
                engine.settings.max_hold_ms = 25
                engine.policy = core.E4Policy(engine.settings)
                item = position(
                    "horizon",
                    time.time_ns() - 50_000_000,
                    partial=True,
                )
                rpc.tokens[item.mint] = item.tokens
                engine.positions[item.mint] = item
                engine.store.save_position(item)
                engine.tokens[item.mint] = core.TokenState(item.mint)

                guardian = asyncio.create_task(v4._guardian_v4(engine))
                deadline = time.monotonic() + 1
                while item.mint in engine.positions and time.monotonic() < deadline:
                    await asyncio.sleep(0.005)
                engine.stop_event.set()
                await guardian
                await drain(engine)

                self.assertNotIn(item.mint, engine.positions)
                self.assertEqual(item.status, core.PositionStatus.CLOSED)
            finally:
                engine.store.close()


class V4RapidMovementConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_inflight_partials_escalate_to_precise_full_liquidations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 2.0)
            try:
                v4.ensure_runtime_state(engine)
                engine.policy = core.E4Policy(engine.settings)
                original_execute = engine.execute
                active = 0
                peak_active = 0

                async def delayed_execute(
                    self: core.Engine,
                    request_id: str,
                    request: dict,
                ):
                    nonlocal active, peak_active
                    if request["side"] == "SELL":
                        active += 1
                        peak_active = max(peak_active, active)
                        await asyncio.sleep(0.03)
                    try:
                        return await original_execute(request_id, request)
                    finally:
                        if request["side"] == "SELL":
                            active -= 1

                engine.execute = types.MethodType(delayed_execute, engine)
                now = time.time_ns()
                items = []
                for index in range(2):
                    mint = f"rapid-{index}"
                    item = position(mint, now - 1_000_000_000)
                    rpc.tokens[mint] = item.tokens
                    engine.positions[mint] = item
                    engine.store.save_position(item)
                    state = core.TokenState(mint)
                    state.created_ns = now - 2_000_000_000
                    state.latest_ns = now
                    state.price_sol = item.entry_price
                    state.fdv_usd = 4_878
                    engine.tokens[mint] = state
                    items.append(item)
                    self.assertTrue(
                        v4._queue_exit(
                            engine,
                            item,
                            0.30,
                            "E4 normal first partial",
                        )
                    )

                await asyncio.sleep(0.005)
                for index, item in enumerate(items):
                    event = core.Event(
                        event_id=10 + index,
                        kind=core.EventKind.SELL,
                        mint=item.mint,
                        source_ns=time.time_ns(),
                        received_ns=time.time_ns(),
                        signature=f"collapse-{index}",
                        trader=f"seller-{index}",
                        sol_amount=1.0,
                        token_amount=100.0,
                        price_sol=0.000035,
                        fdv_usd=3_500,
                    )
                    await engine.on_event(event)

                guardian = asyncio.create_task(v4._guardian_v4(engine))
                deadline = time.monotonic() + 2
                while engine.positions and time.monotonic() < deadline:
                    await asyncio.sleep(0.005)
                engine.stop_event.set()
                await guardian
                await drain(engine, timeout=2)

                self.assertEqual(engine.positions, {})
                self.assertGreaterEqual(peak_active, 2)
                self.assertLessEqual(peak_active, 2)
                self.assertEqual(engine.e4_task_failures, 0)
                for item in items:
                    self.assertEqual(item.status, core.PositionStatus.CLOSED)
                    self.assertAlmostEqual(item.remaining, 0.0, places=9)
                    self.assertAlmostEqual(rpc.tokens[item.mint], 0.0, places=9)
                    sells = engine.store.conn.execute(
                        "SELECT fraction FROM e4_orders WHERE mint=? AND side='SELL' "
                        "ORDER BY created_ns",
                        (item.mint,),
                    ).fetchall()
                    self.assertGreaterEqual(len(sells), 2)
                    self.assertAlmostEqual(float(sells[0][0]), 0.30, places=9)
                    self.assertAlmostEqual(float(sells[-1][0]), 1.0, places=9)
            finally:
                engine.store.close()

    async def test_supervisor_surfaces_uncaught_background_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = await make_engine(Path(directory) / "e4.db", 1.0)
            try:
                v4.ensure_runtime_state(engine)

                async def explode() -> None:
                    await asyncio.sleep(0)
                    raise RuntimeError("deliberate task failure")

                engine.spawn(explode())
                await asyncio.gather(*tuple(engine.tasks), return_exceptions=True)
                self.assertEqual(engine.e4_task_failures, 1)
            finally:
                engine.store.close()


if __name__ == "__main__":
    unittest.main()
