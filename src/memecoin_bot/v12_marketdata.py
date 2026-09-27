"""Dedicated non-trading realtime market-data/intelligence service for V12."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import time
from pathlib import Path

from memecoin_bot.config import Settings
from memecoin_bot.main import build
from memecoin_bot.observability.logging import configure_logging


def _marketdata_db_snapshot(store) -> tuple[list[dict], int, int, int]:
    provider_rows = [
        dict(row)
        for row in store.conn.execute(
            "SELECT * FROM provider_health ORDER BY provider"
        )
    ]
    canonical_count = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM canonical_events"
        ).fetchone()[0]
    )
    canonical_max_rowid = int(
        store.conn.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM canonical_events"
        ).fetchone()[0]
    )
    pending = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM canonical_events "
            "WHERE processing_status IN ('PENDING','PROCESSING')"
        ).fetchone()[0]
    )
    return provider_rows, canonical_count, canonical_max_rowid, pending


async def heartbeat(
    store,
    service,
    path: Path,
    stop: asyncio.Event,
    *,
    stale_seconds: float,
    max_pending: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_count = -1
    last_progress = time.monotonic()
    last_pump_provider_healthy = time.monotonic()
    started = time.monotonic()
    while not stop.is_set():
        try:
            (
                provider_rows,
                canonical_count,
                canonical_max_rowid,
                pending,
            ) = _marketdata_db_snapshot(store)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"canonical market-data heartbeat query failed: {exc}"
            ) from exc

        now = time.monotonic()
        if canonical_count > last_count:
            last_count = canonical_count
            last_progress = now

        pump_rows = [
            row
            for row in provider_rows
            if "pump" in str(row.get("provider") or "").lower()
        ]
        pump_provider_seen = bool(pump_rows)
        pump_provider_connected = pump_provider_seen and any(
            bool(int(row.get("healthy") or 0))
            or str(row.get("state") or "").upper() == "CONNECTED"
            for row in pump_rows
        )
        startup_grace = now - started < stale_seconds
        pump_provider_ok = pump_provider_connected
        if pump_provider_connected:
            last_pump_provider_healthy = now
        elif (
            not startup_grace
            and now - last_pump_provider_healthy >= stale_seconds
        ):
            raise RuntimeError(
                f"Pump market-data provider never connected or remained unhealthy "
                f"for >= {stale_seconds:.1f}s"
            )

        if pending > max_pending:
            raise RuntimeError(
                f"canonical processing backlog exceeded limit: {pending}>{max_pending}"
            )
        payload = {
            "pid": os.getpid(),
            "ts_ns": time.time_ns(),
            "canonical_events": canonical_count,
            "canonical_max_rowid": canonical_max_rowid,
            "canonical_quiet_seconds": max(0.0, now - last_progress),
            "pending_canonical_events": pending,
            "pump_provider_seen": pump_provider_seen,
            "pump_provider_ok": pump_provider_ok,
            "providers": provider_rows,
            "realtime_sources": len(service.realtime_sources),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except TimeoutError:
            pass


async def run() -> int:
    # Keep the market-data service and live execution engine on the same
    # operator-supplied Solana endpoints. E4 uses E4_PRIMARY_RPC_URL while the
    # V1.5 market-data stack historically used SOLANA_RPC_URL.
    primary_rpc = os.getenv("E4_PRIMARY_RPC_URL", "").strip()
    if primary_rpc and not os.getenv("SOLANA_RPC_URL", "").strip():
        os.environ["SOLANA_RPC_URL"] = primary_rpc
    fallback_rpcs = [
        value.strip()
        for value in os.getenv("E4_FALLBACK_RPC_URLS", "").split(",")
        if value.strip()
    ]
    if fallback_rpcs and not os.getenv("SOLANA_FALLBACK_RPC_URL", "").strip():
        os.environ["SOLANA_FALLBACK_RPC_URL"] = fallback_rpcs[0]

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if not settings.realtime_fabric_enabled:
        raise RuntimeError("V12 market-data service requires realtime_fabric_enabled")
    if not settings.pumpfun_native_enabled:
        raise RuntimeError("V12 market-data service requires native Pump feed")
    # This service owns intelligence/evidence only; it can never route public
    # alerts or transactions.
    settings.public_alerts_enabled = False
    settings.operator_shadow_alerts_enabled = False
    settings.shadow_mode = True

    store, service = build(settings)
    if not service.realtime_sources:
        raise RuntimeError("V12 market-data service has no realtime sources")

    stop = service.stop_event
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.stop)
        except (NotImplementedError, RuntimeError):
            pass

    hb = asyncio.create_task(
        heartbeat(
            store,
            service,
            Path(
                os.getenv(
                    "V12_MARKETDATA_HEARTBEAT",
                    "run/v12-marketdata-heartbeat.json",
                )
            ),
            stop,
            stale_seconds=float(
                os.getenv("V12_MARKETDATA_STALE_SECONDS", "90")
            ),
            max_pending=int(
                os.getenv("V12_MARKETDATA_MAX_PENDING", "10000")
            ),
        ),
        name="v12-marketdata-heartbeat",
    )
    runner = asyncio.create_task(service.run(), name="v12-marketdata-service")
    try:
        done, _pending = await asyncio.wait(
            {runner, hb},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if hb in done:
            error = hb.exception()
            service.stop()
            await asyncio.gather(runner, return_exceptions=True)
            if error is not None:
                raise error
            raise RuntimeError("market-data heartbeat exited unexpectedly")
        return 0
    finally:
        service.stop()
        hb.cancel()
        runner.cancel()
        await asyncio.gather(hb, runner, return_exceptions=True)
        service.close()
        store.close()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
