"""24/7 health watchdogs for V12 live execution."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    recovery_healthy_cycles: int = 3
    tx_failure_window_seconds: float = 60.0
    heartbeat_path: Path = Path("run/v12-heartbeat.json")
    marketdata_heartbeat_path: Path | None = None
    kill_switch_path: Path = Path("run/V12_KILL")


@dataclass(slots=True)
class WatchdogState:
    last_event_ns: int = field(default_factory=time.time_ns)
    last_db_check_ns: int = 0
    last_rpc_check_ns: int = 0
    last_balance_check_ns: int = 0
    last_balance_sol: float | None = None
    iteration: int = 0
    event_healthy: bool = True
    positions_healthy: bool = True
    rpc_healthy: bool = True
    clock_healthy: bool = True
    database_healthy: bool = True
    disk_healthy: bool = True
    balance_healthy: bool = True
    routes_healthy: bool = True
    healthy_cycles: int = 0
    marketdata_cursor_lag_started_ns: int = 0
    marketdata_cursor_lag_rowid: int = 0


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
        marketdata_heartbeat = os.getenv("V12_MARKETDATA_HEARTBEAT", "").strip()
        self.config = config or WatchdogConfig(
            heartbeat_path=Path(os.getenv("V12_HEARTBEAT_PATH", "run/v12-heartbeat.json")),
            marketdata_heartbeat_path=(
                Path(marketdata_heartbeat) if marketdata_heartbeat else None
            ),
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
        snapshot = self.breaker.store.snapshot()
        journal = getattr(self.engine, "v12_journal", None)
        unresolved = (
            len(journal.recoverable(("SIGNED", "SUBMITTED", "UNCERTAIN")))
            if journal is not None
            else 0
        )
        payload = {
            "pid": os.getpid(),
            "ts_ns": time.time_ns(),
            "mode": snapshot.mode.value,
            "safety_reason": snapshot.reason,
            "open_positions": len(self.engine.positions),
            "pending_entries": len(self.engine.pending_entries),
            "pending_exits": len(self.engine.pending_exits),
            "unresolved_transactions": unresolved,
            "healthy_recovery_cycles": self.state.healthy_cycles,
            "iteration": self.state.iteration,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def _marketdata_heartbeat_health(
        self,
        now_ns: int,
    ) -> tuple[bool, float, str] | None:
        path = self.config.marketdata_heartbeat_path
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            heartbeat_ns = int(payload.get("ts_ns") or 0)
            if heartbeat_ns <= 0:
                return False, float("inf"), "marketdata heartbeat has no timestamp"
            age = max(0.0, (now_ns - heartbeat_ns) / 1e9)
            sources = int(payload.get("realtime_sources") or 0)
            providers = payload.get("providers") or []
            pump_rows = [
                row
                for row in providers
                if isinstance(row, dict)
                and "pump" in str(row.get("provider") or "").lower()
            ]
            provider_ok = not pump_rows or any(
                bool(int(row.get("healthy") or 0))
                or str(row.get("state") or "").upper() == "CONNECTED"
                for row in pump_rows
            )

            canonical_max_rowid = int(payload.get("canonical_max_rowid") or 0)
            source = getattr(self.engine, "source", None)
            source_cursor = int(getattr(source, "last_id", 0) or 0)
            cursor_lag_age = 0.0
            if canonical_max_rowid > source_cursor:
                if (
                    self.state.marketdata_cursor_lag_started_ns <= 0
                    or self.state.marketdata_cursor_lag_rowid != canonical_max_rowid
                ):
                    self.state.marketdata_cursor_lag_started_ns = now_ns
                    self.state.marketdata_cursor_lag_rowid = canonical_max_rowid
                cursor_lag_age = max(
                    0.0,
                    (now_ns - self.state.marketdata_cursor_lag_started_ns) / 1e9,
                )
            else:
                self.state.marketdata_cursor_lag_started_ns = 0
                self.state.marketdata_cursor_lag_rowid = 0

            heartbeat_limit = max(6.0, self.config.event_warn_seconds * 2.0)
            cursor_ok = cursor_lag_age < max(2.0, self.config.event_warn_seconds)
            healthy = (
                age < heartbeat_limit
                and sources > 0
                and provider_ok
                and cursor_ok
            )
            detail = (
                f"marketdata heartbeat age={age:.1f}s "
                f"sources={sources} pump_provider_ok={provider_ok} "
                f"source_cursor={source_cursor} canonical_max_rowid={canonical_max_rowid} "
                f"cursor_lag_age={cursor_lag_age:.1f}s"
            )
            return healthy, max(age, cursor_lag_age), detail
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return False, float("inf"), f"marketdata heartbeat unreadable: {exc}"

    async def _check_event_feed(self, now_ns: int) -> None:
        heartbeat = self._marketdata_heartbeat_health(now_ns)
        if heartbeat is not None:
            healthy, age, detail = heartbeat
            self.state.event_healthy = healthy
            if healthy:
                return
            reason_age = age if age != float("inf") else self.config.event_halt_seconds
            self.breaker.exit_only(f"event_feed_stale_{reason_age:.1f}s")
            self.breaker.store.event(
                "MARKETDATA_HEALTH",
                "ERROR",
                {"detail": detail},
            )
            if self.engine.positions and age >= self.config.event_halt_seconds:
                await self.emergency_exit("watchdog_event_feed_halt")
            return

        age = (now_ns - self.state.last_event_ns) / 1e9
        self.state.event_healthy = age < self.config.event_warn_seconds
        if age >= self.config.event_halt_seconds:
            self.breaker.exit_only(f"event_feed_stale_{age:.1f}s")
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
        self.state.positions_healthy = not stale
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
            self.state.rpc_healthy = False
            self.state.clock_healthy = False
            self.breaker.exit_only("rpc_health_check_failed")
            self.breaker.store.event("RPC_HEALTH", "ERROR", {"error": str(exc)})
            return
        self.state.rpc_healthy = True
        self.state.clock_healthy = True
        if block_time:
            drift = abs(time.time() - float(block_time))
            if drift >= self.config.clock_warn_seconds:
                self.state.clock_healthy = False
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
            self.state.database_healthy = False
            self.breaker.halt("database_integrity_failure")
            self.breaker.store.event("DATABASE", "CRITICAL", {"error": str(exc)})
            if self.engine.positions:
                await self.emergency_exit("watchdog_database_failure")
            return
        self.state.database_healthy = True
        free = shutil.disk_usage(self.engine.settings.execution_db.parent).free
        self.state.disk_healthy = free >= self.config.minimum_disk_free_bytes
        if not self.state.disk_healthy:
            self.breaker.exit_only("disk_space_low")

    async def _check_balance(self, now_ns: int) -> None:
        if now_ns - self.state.last_balance_check_ns < int(self.config.balance_check_seconds * 1e9):
            return
        self.state.last_balance_check_ns = now_ns
        try:
            balance = await self.engine.rpc.balance(self.engine.signer.wallet)
        except RuntimeError:
            self.state.balance_healthy = False
            self.breaker.exit_only("balance_rpc_failure")
            return
        self.state.balance_healthy = True
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
            self.state.balance_healthy = False
            self.breaker.halt("unexpected_balance_drop")
            self.breaker.store.event(
                "BALANCE_DROP",
                "CRITICAL",
                {"before": previous, "after": balance},
            )

    async def _check_routes(self) -> None:
        names = [str(name) for name, _url in getattr(self.engine.sender, "routes", [])]
        if not names:
            self.state.routes_healthy = False
            self.breaker.halt("no_transaction_routes")
            return
        healthy = self.route_health.healthy(names)
        self.state.routes_healthy = bool(healthy)
        if not healthy:
            self.breaker.exit_only("all_transaction_routes_degraded")

    def _refresh_tx_failure_window(self) -> None:
        window = getattr(self.engine, "v12_tx_failures", None)
        if window is None:
            return
        now = time.monotonic()
        while window and now - window[0] > self.config.tx_failure_window_seconds:
            window.popleft()
        self.breaker.store.set_tx_failures(len(window))

    def _health_is_stable(self) -> bool:
        if self.config.kill_switch_path.exists():
            return False
        if not all(
            (
                self.state.event_healthy,
                self.state.positions_healthy,
                self.state.rpc_healthy,
                self.state.clock_healthy,
                self.state.database_healthy,
                self.state.disk_healthy,
                self.state.balance_healthy,
                self.state.routes_healthy,
            )
        ):
            return False
        journal = getattr(self.engine, "v12_journal", None)
        return not (
            journal is not None
            and journal.recoverable(("SIGNED", "SUBMITTED", "UNCERTAIN"))
        )

    def _maybe_restore_entries(self) -> None:
        snapshot = self.breaker.store.snapshot()
        if snapshot.mode != SafetyMode.EXIT_ONLY or not self._health_is_stable():
            self.state.healthy_cycles = 0
            return
        self.state.healthy_cycles += 1
        if self.state.healthy_cycles < max(1, self.config.recovery_healthy_cycles):
            return
        recovered = self.breaker.recover_exit_only(
            "transient_health_recovered",
            allowed_prefixes=(
                "event_feed_stale_",
                "position_price_feed_stale",
                "rpc_health_check_failed",
                "clock_drift_",
                "disk_space_low",
                "balance_rpc_failure",
                "all_transaction_routes_degraded",
                "transaction_failure_burst",
                "uncertain_",
                "runtime_recovery_failure:",
            ),
        )
        if recovered.mode == SafetyMode.ACTIVE:
            self.breaker.store.event(
                "AUTO_RECOVERY",
                "INFO",
                {"previous_reason": snapshot.reason},
            )
            self.state.healthy_cycles = 0

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
            self._refresh_tx_failure_window()
            self.breaker.evaluate_operational()
            self._maybe_restore_entries()
            await self._write_heartbeat()
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(0.1, self.config.interval_seconds),
                )
            except TimeoutError:
                pass
