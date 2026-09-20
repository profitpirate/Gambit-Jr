"""Dedicated non-trading realtime market-data/intelligence service for V12."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

from memecoin_bot.config import Settings
from memecoin_bot.main import build
from memecoin_bot.observability.logging import configure_logging


async def heartbeat(
    store,
    service,
    path: Path,
    stop: asyncio.Event,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        provider_rows = [
            dict(row)
            for row in store.conn.execute(
                "SELECT * FROM provider_health ORDER BY provider"
            )
        ]
        canonical_count = 0
        pending = 0
        try:
            canonical_count = int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM canonical_events"
                ).fetchone()[0]
            )
            pending = int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM canonical_events "
                    "WHERE processing_state!='DONE'"
                ).fetchone()[0]
            )
        except Exception:
            # The health file still proves process liveness during migrations.
            pass
        payload = {
            "pid": os.getpid(),
            "ts_ns": time.time_ns(),
            "canonical_events": canonical_count,
            "pending_canonical_events": pending,
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
        ),
        name="v12-marketdata-heartbeat",
    )
    try:
        await service.run()
        return 0
    finally:
        service.stop()
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)
        service.close()
        store.close()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
