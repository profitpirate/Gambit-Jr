"""Adaptive transaction-route health scoring for V12."""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_route_health(
    route TEXT PRIMARY KEY,
    ewma_latency_ms REAL NOT NULL,
    ewma_success REAL NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    observations INTEGER NOT NULL,
    cooldown_until_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class RouteHealth:
    route: str
    ewma_latency_ms: float
    ewma_success: float
    consecutive_failures: int
    observations: int
    cooldown_until_ns: int

    def score(self, now_ns: int | None = None) -> float:
        now = int(now_ns or time.time_ns())
        if now < self.cooldown_until_ns:
            return -math.inf
        latency_component = 1.0 / (1.0 + max(0.0, self.ewma_latency_ms) / 100.0)
        failure_penalty = min(0.8, self.consecutive_failures * 0.15)
        return 0.65 * self.ewma_success + 0.35 * latency_component - failure_penalty


class RouteHealthStore:
    def __init__(self, database: Path | sqlite3.Connection, *, alpha: float = 0.20):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(database, timeout=5, isolation_level=None, check_same_thread=False)
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.alpha = min(1.0, max(0.01, float(alpha)))

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    def record(self, route: str, *, accepted: bool, latency_ms: float) -> RouteHealth:
        now = time.time_ns()
        row = self.conn.execute(
            "SELECT * FROM v12_route_health WHERE route=?",
            (str(route),),
        ).fetchone()
        if row is None:
            ewma_latency = max(0.0, latency_ms)
            ewma_success = 1.0 if accepted else 0.0
            failures = 0 if accepted else 1
            observations = 1
        else:
            ewma_latency = (
                self.alpha * max(0.0, latency_ms)
                + (1.0 - self.alpha) * float(row["ewma_latency_ms"])
            )
            ewma_success = (
                self.alpha * (1.0 if accepted else 0.0)
                + (1.0 - self.alpha) * float(row["ewma_success"])
            )
            failures = 0 if accepted else int(row["consecutive_failures"]) + 1
            observations = int(row["observations"]) + 1
        cooldown = 0
        if failures >= 5:
            cooldown = now + int(min(60.0, 2 ** min(6, failures - 4)) * 1e9)
        self.conn.execute(
            """
            INSERT INTO v12_route_health(
                route,ewma_latency_ms,ewma_success,consecutive_failures,
                observations,cooldown_until_ns,updated_ns
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(route) DO UPDATE SET
                ewma_latency_ms=excluded.ewma_latency_ms,
                ewma_success=excluded.ewma_success,
                consecutive_failures=excluded.consecutive_failures,
                observations=excluded.observations,
                cooldown_until_ns=excluded.cooldown_until_ns,
                updated_ns=excluded.updated_ns
            """,
            (
                str(route),
                ewma_latency,
                ewma_success,
                failures,
                observations,
                cooldown,
                now,
            ),
        )
        return self.get(route)

    def get(self, route: str) -> RouteHealth:
        row = self.conn.execute(
            "SELECT * FROM v12_route_health WHERE route=?",
            (str(route),),
        ).fetchone()
        if row is None:
            return RouteHealth(str(route), 0.0, 1.0, 0, 0, 0)
        return RouteHealth(
            str(row["route"]),
            float(row["ewma_latency_ms"]),
            float(row["ewma_success"]),
            int(row["consecutive_failures"]),
            int(row["observations"]),
            int(row["cooldown_until_ns"]),
        )

    def ranked(self, routes: list[str]) -> list[str]:
        return sorted(
            routes,
            key=lambda route: self.get(route).score(),
            reverse=True,
        )

    def healthy(self, routes: list[str], minimum_score: float = 0.25) -> list[str]:
        now = time.time_ns()
        return [
            route
            for route in self.ranked(routes)
            if self.get(route).score(now) >= minimum_score
        ]
