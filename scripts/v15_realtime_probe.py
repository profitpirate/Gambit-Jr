from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from memecoin_bot.database import Store
from memecoin_bot.models import iso
from memecoin_bot.providers.base import ResilientJsonClient
from memecoin_bot.realtime import CanonicalEventFabric
from memecoin_bot.realtime.providers import NativePumpFunSource


async def capture(args: argparse.Namespace) -> dict[str, object]:
    store = Store(args.database)
    store.migrate()
    client = ResilientJsonClient(
        "realtime_probe_solana",
        timeout=args.timeout,
        retries=1,
        circuit_failures=4,
        circuit_cooldown=10,
        health_callback=store.set_provider_health,
    )
    source = NativePumpFunSource(
        args.rpc_url,
        client,
        silence_seconds=max(10, args.seconds / 2),
        backfill_limit=25,
    )
    fabric = CanonicalEventFabric(store)
    stop = asyncio.Event()

    async def emit(event: object) -> None:
        result = fabric.publish(event)
        if result.is_new:
            fabric.project(event)
            fabric.complete(event.event_id, feature_ready_timestamp=iso())

    task = asyncio.create_task(source.run_events(emit, stop))
    try:
        await asyncio.sleep(args.seconds)
    finally:
        stop.set()
        await task
    report: dict[str, object] = {
        "evidence_type": "LIVE_NATIVE_SHADOW_CAPTURE_ATTEMPT",
        "duration_seconds": args.seconds,
        "rpc_url_has_embedded_secret": "api-key=" in args.rpc_url.lower(),
        "event_types": {
            str(row[0]): int(row[1])
            for row in store.conn.execute(
                "SELECT event_type,COUNT(*) FROM canonical_events GROUP BY event_type"
            )
        },
        "providers": [dict(row) for row in store.conn.execute("SELECT * FROM provider_health")],
        "fabric": store.realtime_fabric_health(),
        "real_data_coverage": {
            "tokens": int(store.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]),
            "trade_events": int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM canonical_events WHERE event_type='TOKEN_TRADE'"
                ).fetchone()[0]
            ),
            "real_sol_observations": int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM curve_observations_v15 "
                    "WHERE real_sol_reserves IS NOT NULL"
                ).fetchone()[0]
            ),
            "creators": int(
                store.conn.execute(
                    "SELECT COUNT(*) FROM token_realtime_state WHERE creator_address IS NOT NULL"
                ).fetchone()[0]
            ),
        },
        "limitations": [
            "capture duration is bounded and is not sustained prospective acceptance",
            "public RPC policy may reject high-volume transaction enrichment",
            "curve accounts require the full service dynamic subscription worker",
            "Discord delivery is intentionally absent from this read-only probe",
        ],
    }
    store.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded no-trading native Pump realtime probe")
    parser.add_argument(
        "--rpc-url", default="https://api.mainnet-beta.solana.com"
    )
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(capture(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
