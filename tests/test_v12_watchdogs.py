from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_route_health import RouteHealthStore
from memecoin_bot.v12_safety import CircuitBreaker, SafetyMode, SafetyStore
from memecoin_bot.v12_watchdogs import WatchdogConfig, WatchdogManager


class FakeStore:
    def __init__(self, conn):
        self.conn = conn


class FakeRpc:
    async def call(self, method, params):
        del params
        if method == "getSlot":
            return 10
        if method == "getBlockTime":
            return int(time.time())
        raise RuntimeError(method)

    async def balance(self, wallet):
        del wallet
        return 10.0


@pytest.mark.asyncio
async def test_stale_event_feed_halts_and_emergency_exits(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    exits = []
    engine = SimpleNamespace(
        positions={"mint": object()},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )

    async def exit_all(reason):
        exits.append(reason)

    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=exit_all,
        config=WatchdogConfig(
            event_warn_seconds=0.01,
            event_halt_seconds=0.02,
            heartbeat_path=tmp_path / "hb.json",
            kill_switch_path=tmp_path / "kill",
        ),
    )
    manager.state.last_event_ns = time.time_ns() - 1_000_000_000
    await manager._check_event_feed(time.time_ns())
    assert safety.snapshot().mode == SafetyMode.HALTED
    assert exits == [pytest.approx(exits[0])] if False else exits
    assert exits and "watchdog_event_feed_halt" in exits[0]


@pytest.mark.asyncio
async def test_kill_switch_is_persistent_halt(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    exits = []
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    engine = SimpleNamespace(
        positions={"mint": object()},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )

    async def exit_all(reason):
        exits.append(reason)

    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=exit_all,
        config=WatchdogConfig(
            interval_seconds=0.01,
            event_warn_seconds=999,
            event_halt_seconds=999,
            database_check_seconds=999,
            rpc_check_seconds=999,
            balance_check_seconds=999,
            heartbeat_path=tmp_path / "hb.json",
            kill_switch_path=kill,
        ),
    )
    task = __import__("asyncio").create_task(manager.run())
    await __import__("asyncio").sleep(0.03)
    manager.stop()
    await task
    assert safety.snapshot().mode == SafetyMode.HALTED
    assert "operator_kill_switch" in safety.snapshot().reason
    assert exits
