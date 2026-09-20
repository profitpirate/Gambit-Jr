"""24/7 health watchdogs for V12 live execution."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .v12_route_health import RouteHealthStore
from .v12_safety import CircuitBreaker, SafetyMode

LOGGER = logging.getLogger("gambit.v12.watchdogs")


@dataclass(slots=True)
class WatchdogConfig:
    interval_seconds: float = 1.0
    event_warn_seconds: float = 3.0
    event_halt_seconds: float = 10.0
    position_price_stale_seconds: float = 3.0
    clock_warn_seconds: float = 5.0
    clock_halt_seconds: float = 15.0
    minimum_disk_free_bytes: int = 2 * 1024 * 1024 * 1024
    database_check_seconds: float = 60.0
    rpc_check_seconds: float = 5.0
    balance_check_seconds: float = 5.0
    heartbeat_path: Path = Path("run/v12-heartbeat.json")
    kill_switch_path: Path = Path("run/V12_KILL")


@dataclass(slots=True)
class WatchdogState:
    last_event_ns: int = field(default_factory=time.time_ns)
    last_db_check_ns: int = 0
    last_rpc_check_ns: int = 0
    last_balance_check_ns: int = 0
    last_balance_sol: float | None = None
    iteration: int = 0


class WatchdogManager:
    def __init__(
        self,
        engine: Any,
        breaker: CircuitBreaker,
        route_health: RouteHealthStore,
        *,
        emergency_exit: Callable[[str], Awaitable[None]],
        config: WatchdogConfig | None = None,
    ):
        self.engine = engine
        self.breaker = breaker
        self.route_health = route_health
        self.emergency_exit = emergency_exit
        self.config = config or WatchdogConfig(
            heartbeat_path=Path(os.getenv("V12_HEARTBEAT_PATH", "run/v12-heartbeat.json")),
            kill_switch_path=Path(os.getenv("V12_KILL_SWITCH_PATH", "run/V12_KILL")),
        )
        self.state = WatchdogState()
        self.stop_event = asyncio.Event()

    def touch_event(self) -> None:
        self.state.last_event_ns = time.time_ns()

    def stop(self) -> None:
        self.stop_event.set()

    async def _write_heartbeat(self) -> None:
        path = self.config.heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "ts_ns": time.time_ns(),
            "mode": self.breaker.store.snapshot().mode.value,
            "open_positions": len(self.engine.positions),
            "pending_entries": len(self.engine.pending_entries),
            "pending_exits": len(self.engine.pending_exits),
            "iteration": self.state.iteration,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    async def _check_event_feed(self, now_ns: int) -> None:
        age = (now_ns - self.state.last_event_ns) / 1e9
        if age >= self.config.event_halt_seconds:
            self.breaker.halt(f"event_feed_stale_{age:.1f}s")
            if self.engine.positions:
                await self.emergency_exit("watchdog_event_feed_halt")
        elif age >= self.config.event_warn_seconds:
            self.breaker.exit_only(f"event_feed_stale_{age:.1f}s")

    async def _check_positions(self, now_ns: int) -> None:
        stale = []
        for mint, position in self.engine.positions.items():
            token_state = self.engine.tokens.get(mint)
            latest = int(getattr(token_state, "latest_ns", 0) or 0)
            if not latest:
                latest = int(getattr(position, "opened_ns", now_ns))
            age = (now_ns - latest) / 1e9
            if age >= self.config.position_price_stale_seconds:
                stale.append((mint, age))
        if stale:
            self.breaker.exit_only("position_price_feed_stale")
            await self.emergency_exit(
                "watchdog_position_stale:" + ",".join(f"{mint}:{age:.1f}s" for mint, age in stale)
            )

    async def _check_rpc_and_clock(self, now_ns: int) -> None:
        if now_ns - self.state.last_rpc_check_ns < int(self.config.rpc_check_seconds * 1e9):
            return
        self.state.last_rpc_check_ns = now_ns
        try:
            slot = await self.engine.rpc.call("getSlot", [{"commitment": "processed"}])
            block_time = await self.engine.rpc.call("getBlockTime", [slot])
        except RuntimeError as exc:
            self.breaker.exit_only("rpc_health_check_failed")
            self.breaker.store.event("RPC_HEALTH", "ERROR", {"error": str(exc)})
            return
        if block_time:
            drift = abs(time.time() - float(block_time))
            if drift >= self.config.clock_halt_seconds:
                self.breaker.halt(f"clock_drift_{drift:.1f}s")
            elif drift >= self.config.clock_warn_seconds:
                self.breaker.exit_only(f"clock_drift_{drift:.1f}s")

    async def _check_database_disk(self, now_ns: int) -> None:
        if now_ns - self.state.last_db_check_ns < int(self.config.database_check_seconds * 1e9):
            return
        self.state.last_db_check_ns = now_ns
        try:
            row = self.engine.store.conn.execute("PRAGMA quick_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise sqlite3.DatabaseError(str(row))
        except sqlite3.Error as exc:
            self.breaker.halt("database_integrity_failure")
            self.breaker.store.event("DATABASE", "CRITICAL", {"error": str(exc)})
            if self.engine.positions:
                await self.emergency_exit("watchdog_database_failure")
            return
        free = shutil.disk_usage(self.engine.settings.execution_db.parent).free
        if free < self.config.minimum_disk_free_bytes:
            self.breaker.exit_only("disk_space_low")

    async def _check_balance(self, now_ns: int) -> None:
        if now_ns - self.state.last_balance_check_ns < int(self.config.balance_check_seconds * 1e9):
            return
        self.state.last_balance_check_ns = now_ns
        try:
            balance = await self.engine.rpc.balance(self.engine.signer.wallet)
        except RuntimeError:
            self.breaker.exit_only("balance_rpc_failure")
            return
        self.breaker.evaluate_equity(balance)
        previous = self.state.last_balance_sol
        self.state.last_balance_sol = balance
        if (
            previous is not None
            and previous > 0
            and not self.engine.pending_entries
            and not self.engine.pending_exits
            and balance < previous * 0.70
        ):
            self.breaker.halt("unexpected_balance_drop")
            self.breaker.store.event(
                "BALANCE_DROP",
                "CRITICAL",
                {"before": previous, "after": balance},
            )

    async def _check_routes(self) -> None:
        names = [str(name) for name, _url in getattr(self.engine.sender, "routes", [])]
        if not names:
            self.breaker.halt("no_transaction_routes")
            return
        healthy = self.route_health.healthy(names)
        if not healthy:
            self.breaker.exit_only("all_transaction_routes_degraded")

    async def run(self) -> None:
        while not self.stop_event.is_set():
            now_ns = time.time_ns()
            self.state.iteration += 1
            if self.config.kill_switch_path.exists():
                self.breaker.halt("operator_kill_switch")
                if self.engine.positions:
                    await self.emergency_exit("operator_kill_switch")
            await self._check_event_feed(now_ns)
            await self._check_positions(now_ns)
            await self._check_rpc_and_clock(now_ns)
            await self._check_database_disk(now_ns)
            await self._check_balance(now_ns)
            await self._check_routes()
            self.breaker.evaluate_operational()
            await self._write_heartbeat()
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(0.1, self.config.interval_seconds),
                )
            except TimeoutError:
                pass
