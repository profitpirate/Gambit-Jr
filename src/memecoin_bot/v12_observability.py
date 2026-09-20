"""Local-only metrics/health server for V12 production."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web


@dataclass(slots=True)
class MetricsServer:
    engine: Any
    host: str = "127.0.0.1"
    port: int = 9464

    def _snapshot(self) -> dict[str, float]:
        conn: sqlite3.Connection = self.engine.store.conn
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM e4_positions WHERE status IN ('OPEN','PARTIAL','EXITING')"
        ).fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM e4_positions WHERE status='CLOSED'"
        ).fetchone()[0]
        pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_sol-entry_sol),0) FROM e4_positions WHERE status='CLOSED'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM v12_execution_journal WHERE state IN ('PREPARED','SIGNED','SUBMITTED','UNCERTAIN')"
        ).fetchone()[0]
        safety = conn.execute(
            "SELECT mode,consecutive_losses,tx_failures_window FROM v12_safety_state WHERE singleton=1"
        ).fetchone()
        route_rows = conn.execute(
            "SELECT route,ewma_success,ewma_latency_ms,consecutive_failures FROM v12_route_health"
        ).fetchall()
        mode = str(safety["mode"]) if safety else "UNKNOWN"
        values: dict[str, float] = {
            "v12_up": 1.0,
            "v12_open_positions": float(open_positions),
            "v12_closed_trades_total": float(closed),
            "v12_closed_pnl_sol": float(pnl),
            "v12_pending_transactions": float(pending),
            "v12_safety_active": 1.0 if mode == "ACTIVE" else 0.0,
            "v12_safety_exit_only": 1.0 if mode == "EXIT_ONLY" else 0.0,
            "v12_safety_halted": 1.0 if mode == "HALTED" else 0.0,
            "v12_consecutive_losses": float(safety["consecutive_losses"]) if safety else 0.0,
            "v12_tx_failures_window": float(safety["tx_failures_window"]) if safety else 0.0,
        }
        for row in route_rows:
            safe = "".join(ch if ch.isalnum() else "_" for ch in str(row["route"]))
            values[f"v12_route_success_{safe}"] = float(row["ewma_success"])
            values[f"v12_route_latency_ms_{safe}"] = float(row["ewma_latency_ms"])
            values[f"v12_route_failures_{safe}"] = float(row["consecutive_failures"])
        return values

    async def metrics(self, _request: web.Request) -> web.Response:
        values = self._snapshot()
        lines = [f"{name} {value}" for name, value in sorted(values.items())]
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="text/plain",
        )

    async def health(self, _request: web.Request) -> web.Response:
        safety = self.engine.v12_safety_store.snapshot()
        heartbeat = getattr(self.engine, "v12_watchdog", None)
        return web.json_response(
            {
                "ok": safety.mode.value != "HALTED",
                "safety": safety.mode.value,
                "reason": safety.reason,
                "watchdog": bool(heartbeat),
                "ts_ns": time.time_ns(),
            },
            status=200 if safety.mode.value != "HALTED" else 503,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("metrics server must remain local-only")
        app = web.Application()
        app.add_routes(
            [
                web.get("/metrics", self.metrics),
                web.get("/healthz", self.health),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        try:
            await stop_event.wait()
        finally:
            await runner.cleanup()


def from_env(engine: Any) -> MetricsServer:
    return MetricsServer(
        engine,
        host=os.getenv("V12_METRICS_HOST", "127.0.0.1"),
        port=int(os.getenv("V12_METRICS_PORT", "9464")),
    )
