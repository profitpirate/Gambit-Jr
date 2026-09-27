from __future__ import annotations

import time
from collections import deque
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
async def test_stale_event_feed_blocks_entries_and_emergency_exits(tmp_path: Path) -> None:
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
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY
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

def test_transient_exit_only_recovers_after_stable_health(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    engine = SimpleNamespace(
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
        v12_tx_failures=deque(),
        v12_journal=SimpleNamespace(recoverable=lambda states: []),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            recovery_healthy_cycles=2,
            heartbeat_path=tmp_path / "hb.json",
            kill_switch_path=tmp_path / "kill",
        ),
    )
    breaker.exit_only("rpc_health_check_failed")

    manager._maybe_restore_entries()
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY
    manager._maybe_restore_entries()

    assert safety.snapshot().mode == SafetyMode.ACTIVE
    assert safety.snapshot().reason == "transient_health_recovered"


def test_transaction_failure_window_expires_and_unblocks_entries(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    failures = deque([time.monotonic() - 61.0 for _ in range(5)])
    engine = SimpleNamespace(
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
        v12_tx_failures=failures,
        v12_journal=SimpleNamespace(recoverable=lambda states: []),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            recovery_healthy_cycles=1,
            heartbeat_path=tmp_path / "hb.json",
            kill_switch_path=tmp_path / "kill",
        ),
    )
    safety.set_tx_failures(5)
    breaker.exit_only("transaction_failure_burst")

    manager._refresh_tx_failure_window()
    manager._maybe_restore_entries()

    assert safety.snapshot().tx_failures_window == 0
    assert safety.snapshot().mode == SafetyMode.ACTIVE

@pytest.mark.asyncio
async def test_fresh_marketdata_heartbeat_keeps_quiet_feed_active(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    heartbeat = tmp_path / "marketdata.json"
    heartbeat.write_text(
        __import__("json").dumps(
            {
                "ts_ns": time.time_ns(),
                "realtime_sources": 1,
                "providers": [
                    {
                        "provider": "solana_pumpfun_native",
                        "healthy": 1,
                        "state": "CONNECTED",
                    }
                ],
            }
        )
    )
    engine = SimpleNamespace(
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            event_warn_seconds=3.0,
            event_halt_seconds=10.0,
            heartbeat_path=tmp_path / "hb.json",
            marketdata_heartbeat_path=heartbeat,
            kill_switch_path=tmp_path / "kill",
        ),
    )
    manager.state.last_event_ns = time.time_ns() - 60_000_000_000

    await manager._check_event_feed(time.time_ns())

    assert manager.state.event_healthy is True
    assert safety.snapshot().mode == SafetyMode.ACTIVE

@pytest.mark.asyncio
async def test_marketdata_cursor_lag_blocks_entries_after_grace(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    heartbeat = tmp_path / "marketdata.json"
    heartbeat.write_text(
        __import__("json").dumps(
            {
                "ts_ns": time.time_ns(),
                "realtime_sources": 1,
                "canonical_max_rowid": 100,
                "providers": [
                    {
                        "provider": "solana_pumpfun_native",
                        "healthy": 1,
                        "state": "CONNECTED",
                    }
                ],
            }
        )
    )
    engine = SimpleNamespace(
        source=SimpleNamespace(last_id=95, table="canonical_events"),
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            event_warn_seconds=0.01,
            event_halt_seconds=10.0,
            heartbeat_path=tmp_path / "hb.json",
            marketdata_heartbeat_path=heartbeat,
            kill_switch_path=tmp_path / "kill",
        ),
    )

    manager.state.marketdata_cursor_lag_started_ns = time.time_ns() - 3_000_000_000
    manager.state.marketdata_source_cursor_seen = 95
    manager.state.marketdata_source_progress_ns = time.time_ns() - 3_000_000_000
    await manager._check_event_feed(time.time_ns())

    assert manager.state.event_healthy is False
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY
    assert "event_feed_stale" in safety.snapshot().reason


@pytest.mark.asyncio
async def test_marketdata_cursor_catches_up_and_recovers_health(tmp_path: Path) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    heartbeat = tmp_path / "marketdata.json"
    heartbeat.write_text(
        __import__("json").dumps(
            {
                "ts_ns": time.time_ns(),
                "realtime_sources": 1,
                "canonical_max_rowid": 100,
                "pump_provider_seen": True,
                "pump_provider_ok": True,
                "providers": [],
            }
        )
    )
    engine = SimpleNamespace(
        source=SimpleNamespace(last_id=100, table="canonical_events"),
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            heartbeat_path=tmp_path / "hb.json",
            marketdata_heartbeat_path=heartbeat,
            kill_switch_path=tmp_path / "kill",
        ),
    )
    manager.state.marketdata_cursor_lag_started_ns = time.time_ns() - 5_000_000_000
    manager.state.marketdata_source_cursor_seen = 99
    manager.state.marketdata_source_progress_ns = time.time_ns() - 5_000_000_000

    await manager._check_event_feed(time.time_ns())

    assert manager.state.event_healthy is True
    assert manager.state.marketdata_cursor_lag_started_ns == 0
    assert manager.state.marketdata_source_cursor_seen == 100

@pytest.mark.asyncio
async def test_marketdata_heartbeat_without_connected_pump_feed_blocks_entries(
    tmp_path: Path,
) -> None:
    safety = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(safety)
    routes = RouteHealthStore(safety.conn)
    heartbeat = tmp_path / "marketdata.json"
    heartbeat.write_text(
        __import__("json").dumps(
            {
                "ts_ns": time.time_ns(),
                "realtime_sources": 1,
                "canonical_max_rowid": 0,
                "pump_provider_seen": False,
                "pump_provider_ok": False,
                "providers": [],
            }
        )
    )
    engine = SimpleNamespace(
        source=SimpleNamespace(last_id=0, table="canonical_events"),
        positions={},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("r1", "https://x")]),
        rpc=FakeRpc(),
        signer=SimpleNamespace(wallet="w"),
        settings=SimpleNamespace(execution_db=tmp_path / "e4.db"),
        store=FakeStore(safety.conn),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        routes,
        emergency_exit=lambda reason: None,
        config=WatchdogConfig(
            heartbeat_path=tmp_path / "hb.json",
            marketdata_heartbeat_path=heartbeat,
            kill_switch_path=tmp_path / "kill",
        ),
    )

    await manager._check_event_feed(time.time_ns())

    assert manager.state.event_healthy is False
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY

