from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memecoin_bot.database import Store
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType
from memecoin_bot.realtime.features import RealtimeFeatureProjector
from memecoin_bot.realtime.lanes import TokenLaneExecutor


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def hot_token(database: Path, event_count: int) -> dict[str, Any]:
    store = Store(database, Path("migrations"))
    store.migrate()
    fabric = CanonicalEventFabric(store)
    projector = RealtimeFeatureProjector(store)
    base = datetime.now(UTC) - timedelta(minutes=10)
    created = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "BenchmarkHotToken",
        "solana",
        "pumpfun",
        "benchmark",
        base.isoformat(),
        source_event_id="created",
        payload={"creator": "BenchmarkCreator"},
    )
    fabric.publish(created)
    token_id, _ = fabric.project(created)
    assert token_id is not None
    projector.apply(token_id, created)
    samples: list[float] = []
    cpu_started = time.process_time()
    started = time.perf_counter()
    tracemalloc.start()
    for index in range(event_count):
        event = CanonicalEvent.create(
            CanonicalEventType.TOKEN_TRADE,
            "BenchmarkHotToken",
            "solana",
            "pumpfun",
            "benchmark",
            (base + timedelta(milliseconds=index + 1)).isoformat(),
            source_event_id=f"trade:{index}",
            transaction_signature=f"benchmark:{index}",
            payload={
                "side": "sell" if index % 17 == 0 else "buy",
                "actor": f"buyer:{index % max(1, event_count // 5)}",
                "sol_amount": 0.001,
            },
        )
        one = time.perf_counter()
        projector.apply(token_id, event)
        if index < 10_000 or index % 100 == 0:
            samples.append((time.perf_counter() - one) * 1000)
    feature_started = time.perf_counter()
    feature = projector.compute(
        token_id,
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    )
    feature_ms = (time.perf_counter() - feature_started) * 1000
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu_started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    state_bytes = len(
        store.conn.execute(
            "SELECT state_json FROM incremental_feature_state_v15 WHERE token_id=?",
            (token_id,),
        ).fetchone()[0]
    )
    integrity = store.database_integrity()
    store.close()
    return {
        "events": event_count,
        "elapsed_seconds": elapsed,
        "events_per_second": event_count / max(elapsed, 1e-9),
        "cpu_seconds": cpu,
        "write_latency_ms": {
            "mean": statistics.fmean(samples),
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
        },
        "feature_compute_ms": feature_ms,
        "incremental_state_bytes": state_bytes,
        "raw_buyers": feature["buyer_arrival"]["raw_buyers"],
        "peak_traced_memory_bytes": peak,
        "database_integrity": integrity,
    }


async def token_lane(token_count: int, lane_count: int) -> dict[str, Any]:
    executor = TokenLaneExecutor(lane_count, queue_size=max(1_024, token_count))
    now = datetime.now(UTC) - timedelta(seconds=1)
    events = [
        CanonicalEvent.create(
            CanonicalEventType.TOKEN_TRADE,
            f"token:{index}",
            "solana",
            "pumpfun",
            "benchmark",
            now.isoformat(),
            source_event_id=str(index),
            payload={"side": "buy", "actor": str(index), "sol_amount": 0.001},
        )
        for index in range(token_count)
    ]
    claimed = False
    stop = asyncio.Event()
    wake = asyncio.Event()
    active = 0
    maximum_active = 0

    def claim(_limit: int) -> list[CanonicalEvent]:
        nonlocal claimed
        if claimed:
            return []
        claimed = True
        return events

    async def handle(_event: CanonicalEvent) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        if executor.processed + 1 == token_count:
            stop.set()

    started = time.perf_counter()
    await executor.run(
        claim=claim,
        handle=handle,
        fail=lambda *_args: None,
        wake=wake,
        stop=stop,
        batch_size=token_count,
    )
    elapsed = time.perf_counter() - started
    return {
        "tokens": token_count,
        "lanes": lane_count,
        "elapsed_seconds": elapsed,
        "events_per_second": token_count / max(elapsed, 1e-9),
        "maximum_parallel_handlers": maximum_active,
        "processed": executor.processed,
        "failures": executor.failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="V1.5 authoritative hot-path benchmark")
    parser.add_argument(
        "--event-counts",
        default="10000",
        help="comma-separated HOT-token sizes; use 10000,50000,100000 for the extended run",
    )
    parser.add_argument("--token-counts", default="100,1000")
    parser.add_argument("--lanes", type=int, default=8)
    args = parser.parse_args()
    event_counts = [int(value) for value in args.event_counts.split(",") if value]
    token_counts = [int(value) for value in args.token_counts.split(",") if value]
    with tempfile.TemporaryDirectory(prefix="gambit-v15-authority-") as directory:
        report = {
            "measured_at": datetime.now(UTC).isoformat(),
            "hot_token": [
                hot_token(Path(directory) / f"hot-{count}.db", count) for count in event_counts
            ],
            "token_lanes": [asyncio.run(token_lane(count, args.lanes)) for count in token_counts],
            "scope": {
                "source_to_discord_network_excluded": True,
                "extended_command": "--event-counts 10000,50000,100000",
            },
        }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
