from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_hardening_v5

core = e4_hardening_v5.core
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "e4" / "real_market_rapid_paths.json"


class _Store:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    def decision(self, mint, event_id, action, score, reason, payload) -> None:
        self.decisions.append(
            {
                "mint": mint,
                "event_id": event_id,
                "action": action,
                "reason": reason,
                "payload": dict(payload),
            }
        )


class _Engine:
    def __init__(self) -> None:
        self.settings = core.Settings(
            model_path=Path("missing-model.json"),
            failure_window_ms=5_000,
            max_hold_ms=60_000,
        )
        self.policy = core.E4Policy(self.settings)
        self.store = _Store()
        self.tokens: dict[str, core.TokenState] = {}
        self.positions: dict[str, core.Position] = {}
        self.pending_entries: set[str] = set()
        self.pending_exits: set[str] = set()
        self.position_locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()
        self.stop_event = asyncio.Event()
        self.sell_calls: list[tuple[str, float, str, float]] = []
        self.sold: dict[str, float] = {}

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def execute_sell(self, position: core.Position, fraction: float, reason: str) -> None:
        await asyncio.sleep(0)
        mint = position.mint
        before = position.remaining
        sold = before * min(1.0, max(0.0, fraction))
        self.sold[mint] = self.sold.get(mint, 0.0) + sold
        self.assertions(before, sold, position.tokens)
        position.remaining = max(0.0, before - sold)
        self.sell_calls.append((mint, fraction, reason, position.remaining))
        if not position.first_partial_done and fraction < 0.999:
            position.first_partial_done = True
            position.first_partial_fraction = sold / before if before else fraction
            position.status = core.PositionStatus.PARTIAL
        if fraction >= 0.999 or position.remaining <= position.tokens * 1e-8:
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            self.positions.pop(mint, None)
        self.pending_exits.discard(mint)

    @staticmethod
    def assertions(before: float, sold: float, original: float) -> None:
        if sold < -1e-12 or sold > before + 1e-9 or sold > original + 1e-9:
            raise AssertionError((before, sold, original))

    async def drain(self) -> None:
        for _ in range(20):
            pending = tuple(self.tasks)
            if not pending:
                return
            await asyncio.gather(*pending)
            await asyncio.sleep(0)


def _event(row: dict, base_ns: int) -> core.Event:
    return core.Event(
        event_id=abs(hash((row["mint"], row["t_us"], row["signature"], row["kind"]))) % (2**63 - 1),
        kind=core.EventKind(row["kind"]),
        mint=row["mint"],
        source_ns=base_ns + int(row["t_us"]) * 1_000,
        received_ns=base_ns + int(row["t_us"]) * 1_000,
        slot=row.get("slot"),
        signature=row.get("signature"),
        trader=row.get("trader"),
        sol_amount=float(row.get("sol_amount") or 0),
        token_amount=float(row.get("token_amount") or 0),
        price_sol=row.get("price_sol"),
        fdv_usd=row.get("fdv_usd"),
        creator=row.get("creator"),
        complete=row["kind"] == "MIGRATION",
    )


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def _seed_position(engine: _Engine, path: dict, base_ns: int) -> tuple[core.TokenState, core.Position, list[dict]]:
    rows = list(path["events"])
    state = core.TokenState(path["mint"])
    engine.tokens[path["mint"]] = state
    first_buy_index = next(index for index, row in enumerate(rows) if row["kind"] == "BUY")
    for row in rows[: first_buy_index + 1]:
        state.apply(_event(row, base_ns), None)
    entry_price = float(rows[first_buy_index]["price_sol"])
    position = core.Position(
        position_id=f"position-{path['mint']}",
        mint=path["mint"],
        status=core.PositionStatus.OPEN,
        opened_ns=base_ns,
        entry_sol=entry_price * 1_000,
        tokens=1_000.0,
        remaining=1_000.0,
        entry_price=entry_price,
        max_price=entry_price,
        last_price=entry_price,
        entry_signature="fixture-entry",
    )
    engine.positions[path["mint"]] = position
    return state, position, rows[first_buy_index + 1 :]


class RealMarketRapidPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_real_launches_are_managed_independently_under_concurrent_bursts(self) -> None:
        fixture = _load()["scenarios"]["two_concurrent_launches"]
        engine = _Engine()
        base_ns = time.time_ns()
        remaining: list[dict] = []
        originals: dict[str, core.Position] = {}
        for path in fixture["paths"]:
            _, position, rows = _seed_position(engine, path, base_ns)
            originals[path["mint"]] = position
            remaining.extend(rows)
        remaining.sort(key=lambda row: (row["t_us"], row["mint"], row["signature"]))

        for row in remaining:
            state = engine.tokens[row["mint"]]
            event = _event(row, base_ns)
            with patch.object(core.time, "time_ns", return_value=event.source_ns):
                state.apply(event, None)
                position = engine.positions.get(row["mint"])
                if position is not None:
                    await e4_hardening_v5._evaluate_current_position(
                        engine,
                        position,
                        "real-market event",
                        event_id=event.event_id,
                    )
            await engine.drain()

        self.assertEqual(set(engine.sold), set(originals))
        self.assertFalse(engine.positions)
        for mint, position in originals.items():
            self.assertLessEqual(engine.sold[mint], position.tokens + 1e-9)
            self.assertAlmostEqual(position.remaining, 0.0, places=9)
            fractions = [fraction for call_mint, fraction, _, _ in engine.sell_calls if call_mint == mint]
            self.assertTrue(any(abs(value - 0.20) < 1e-9 or abs(value - 0.30) < 1e-9 for value in fractions))
            self.assertAlmostEqual(fractions[-1], 1.0, places=9)

    async def test_post_buy_catchup_sees_real_five_x_move_without_waiting_for_next_event(self) -> None:
        path = _load()["scenarios"]["extreme_same_slot_move"]["paths"][0]
        engine = _Engine()
        base_ns = time.time_ns()
        state, position, rows = _seed_position(engine, path, base_ns)
        for row in rows:
            if row["t_us"] > 100_000:
                break
            state.apply(_event(row, base_ns), None)
        now = base_ns + 100_000_000
        with patch.object(core.time, "time_ns", return_value=now):
            scheduled = await e4_hardening_v5._evaluate_current_position(
                engine,
                position,
                "E4 post-buy catch-up",
            )
        self.assertTrue(scheduled)
        await engine.drain()
        self.assertTrue(position.first_partial_done)
        self.assertAlmostEqual(position.first_partial_fraction or 0.0, 0.20, places=9)
        self.assertAlmostEqual(position.remaining, 800.0, places=9)

    async def test_post_partial_catchup_exits_real_reversal_without_new_event(self) -> None:
        path = _load()["scenarios"]["two_concurrent_launches"]["paths"][0]
        engine = _Engine()
        base_ns = time.time_ns()
        state, position, rows = _seed_position(engine, path, base_ns)
        for row in rows:
            state.apply(_event(row, base_ns), None)
        position.first_partial_done = True
        position.first_partial_fraction = 0.20
        position.remaining = 800.0
        position.max_price = max(
            float(row["price_sol"])
            for row in path["events"]
            if row.get("price_sol")
        )
        position.last_price = state.price_sol or position.last_price
        with patch.object(core.time, "time_ns", return_value=base_ns + 6_000_000_000):
            scheduled = await e4_hardening_v5._evaluate_current_position(
                engine,
                position,
                "E4 post-fill catch-up",
            )
        self.assertTrue(scheduled)
        await engine.drain()
        self.assertNotIn(position.mint, engine.positions)
        self.assertAlmostEqual(position.remaining, 0.0, places=9)

    async def test_guardian_closes_two_quiet_positions_at_confirmation_deadline(self) -> None:
        engine = _Engine()
        now = time.time_ns()
        for index in range(2):
            mint = f"quiet-{index}"
            state = core.TokenState(mint)
            state.price_sol = 0.00005
            engine.tokens[mint] = state
            engine.positions[mint] = core.Position(
                position_id=f"p-{index}",
                mint=mint,
                status=core.PositionStatus.OPEN,
                opened_ns=now - 5_100_000_000,
                entry_sol=0.05,
                tokens=1_000.0,
                remaining=1_000.0,
                entry_price=0.00005,
                max_price=0.00005,
                last_price=0.00005,
                entry_signature="entry",
            )
        task = asyncio.create_task(e4_hardening_v5._guardian_v5(engine))
        try:
            for _ in range(50):
                await asyncio.sleep(0.002)
                await engine.drain()
                if not engine.positions:
                    break
        finally:
            engine.stop_event.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertFalse(engine.positions)
        self.assertEqual(len(engine.sell_calls), 2)
        self.assertTrue(all(abs(fraction - 1.0) < 1e-9 for _, fraction, _, _ in engine.sell_calls))


if __name__ == "__main__":
    unittest.main()
