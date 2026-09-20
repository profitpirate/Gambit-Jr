"""Persistent circuit breaker and safety state for V12 live trading."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_safety_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    mode TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_ns INTEGER NOT NULL,
    peak_equity_sol REAL,
    day_start_equity_sol REAL,
    day_start_epoch INTEGER,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    tx_failures_window INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO v12_safety_state(
    singleton,mode,reason,updated_ns,consecutive_losses,tx_failures_window
) VALUES(1,'ACTIVE','initial',0,0,0);
CREATE TABLE IF NOT EXISTS v12_safety_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    severity TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
"""


class SafetyMode(StrEnum):
    ACTIVE = "ACTIVE"
    EXIT_ONLY = "EXIT_ONLY"
    HALTED = "HALTED"


@dataclass(frozen=True, slots=True)
class SafetySnapshot:
    mode: SafetyMode
    reason: str
    peak_equity_sol: float | None
    day_start_equity_sol: float | None
    consecutive_losses: int
    tx_failures_window: int

    @property
    def entries_allowed(self) -> bool:
        return self.mode == SafetyMode.ACTIVE

    @property
    def exits_allowed(self) -> bool:
        return True


class SafetyStore:
    def __init__(self, database: Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(
                database,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    def snapshot(self) -> SafetySnapshot:
        row = self.conn.execute(
            "SELECT * FROM v12_safety_state WHERE singleton=1"
        ).fetchone()
        assert row is not None
        return SafetySnapshot(
            SafetyMode(str(row["mode"])),
            str(row["reason"]),
            float(row["peak_equity_sol"]) if row["peak_equity_sol"] is not None else None,
            float(row["day_start_equity_sol"]) if row["day_start_equity_sol"] is not None else None,
            int(row["consecutive_losses"]),
            int(row["tx_failures_window"]),
        )

    def set_mode(self, mode: SafetyMode, reason: str) -> None:
        current = self.snapshot()
        # HALTED cannot be automatically relaxed. It requires explicit operator reset.
        if current.mode == SafetyMode.HALTED and mode != SafetyMode.HALTED:
            return
        self.conn.execute(
            "UPDATE v12_safety_state SET mode=?,reason=?,updated_ns=? WHERE singleton=1",
            (mode.value, str(reason), time.time_ns()),
        )
        self.event("SAFETY_MODE", "CRITICAL" if mode == SafetyMode.HALTED else "WARN", {
            "mode": mode.value,
            "reason": reason,
        })

    def operator_reset(self, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE v12_safety_state
            SET mode='ACTIVE',reason=?,updated_ns=?,consecutive_losses=0,
                tx_failures_window=0
            WHERE singleton=1
            """,
            (str(reason), time.time_ns()),
        )
        self.event("OPERATOR_RESET", "WARN", {"reason": reason})

    def event(self, event: str, severity: str, details: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO v12_safety_events(event,severity,details_json,created_ns) VALUES(?,?,?,?)",
            (
                str(event),
                str(severity),
                json.dumps(details, sort_keys=True, separators=(",", ":"), default=str),
                time.time_ns(),
            ),
        )

    def update_equity(self, equity_sol: float) -> None:
        now = int(time.time())
        day = now // 86_400
        row = self.conn.execute(
            "SELECT peak_equity_sol,day_start_equity_sol,day_start_epoch FROM v12_safety_state WHERE singleton=1"
        ).fetchone()
        peak = float(row["peak_equity_sol"]) if row["peak_equity_sol"] is not None else equity_sol
        day_start = float(row["day_start_equity_sol"]) if row["day_start_equity_sol"] is not None else equity_sol
        stored_day = int(row["day_start_epoch"]) if row["day_start_epoch"] is not None else day
        if stored_day != day:
            day_start = equity_sol
            stored_day = day
        peak = max(peak, equity_sol)
        self.conn.execute(
            """
            UPDATE v12_safety_state
            SET peak_equity_sol=?,day_start_equity_sol=?,day_start_epoch=?,updated_ns=?
            WHERE singleton=1
            """,
            (peak, day_start, stored_day, time.time_ns()),
        )

    def record_trade_result(self, pnl_sol: float) -> None:
        if pnl_sol > 0:
            losses = 0
        else:
            losses = self.snapshot().consecutive_losses + 1
        self.conn.execute(
            "UPDATE v12_safety_state SET consecutive_losses=?,updated_ns=? WHERE singleton=1",
            (losses, time.time_ns()),
        )

    def set_tx_failures(self, failures: int) -> None:
        self.conn.execute(
            "UPDATE v12_safety_state SET tx_failures_window=?,updated_ns=? WHERE singleton=1",
            (max(0, int(failures)), time.time_ns()),
        )


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    max_drawdown_fraction: float = 0.15
    reduce_drawdown_fraction: float = 0.10
    max_daily_loss_fraction: float = 0.10
    max_consecutive_losses: int = 6
    max_tx_failures_window: int = 5
    minimum_equity_sol: float = 0.05


class CircuitBreaker:
    def __init__(self, store: SafetyStore, config: CircuitConfig | None = None):
        self.store = store
        self.config = config or CircuitConfig()

    def evaluate_equity(self, equity_sol: float) -> SafetySnapshot:
        self.store.update_equity(equity_sol)
        snap = self.store.snapshot()
        peak = snap.peak_equity_sol or equity_sol
        day_start = snap.day_start_equity_sol or equity_sol
        drawdown = max(0.0, 1.0 - equity_sol / peak) if peak > 0 else 1.0
        daily_loss = max(0.0, 1.0 - equity_sol / day_start) if day_start > 0 else 1.0
        if equity_sol < self.config.minimum_equity_sol:
            self.store.set_mode(SafetyMode.HALTED, "equity_below_minimum")
        elif drawdown >= self.config.max_drawdown_fraction:
            self.store.set_mode(SafetyMode.HALTED, "maximum_drawdown_exceeded")
        elif daily_loss >= self.config.max_daily_loss_fraction:
            self.store.set_mode(SafetyMode.HALTED, "maximum_daily_loss_exceeded")
        elif drawdown >= self.config.reduce_drawdown_fraction:
            self.store.set_mode(SafetyMode.EXIT_ONLY, "drawdown_exit_only")
        return self.store.snapshot()

    def evaluate_operational(self) -> SafetySnapshot:
        snap = self.store.snapshot()
        if snap.consecutive_losses >= self.config.max_consecutive_losses:
            self.store.set_mode(SafetyMode.EXIT_ONLY, "consecutive_loss_limit")
        if snap.tx_failures_window >= self.config.max_tx_failures_window:
            self.store.set_mode(SafetyMode.EXIT_ONLY, "transaction_failure_burst")
        return self.store.snapshot()

    def halt(self, reason: str) -> None:
        self.store.set_mode(SafetyMode.HALTED, reason)

    def exit_only(self, reason: str) -> None:
        self.store.set_mode(SafetyMode.EXIT_ONLY, reason)
