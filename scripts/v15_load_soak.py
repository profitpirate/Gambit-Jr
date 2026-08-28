from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memecoin_bot.alpha_engine import BoundedLaunchQueue, LaunchEvent
from memecoin_bot.database import Store


async def run(
    database: Path, *, events: int, queue_size: int, burst_multiplier: int
) -> dict[str, Any]:
    started = time.perf_counter()
    tracemalloc.start()
    store = Store(database)
    store.migrate()
    queue = BoundedLaunchQueue(queue_size)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    inserted = 0
    duplicates = 0

    # Sustained expected-rate simulation: each event is consumed and persisted.
    for index in range(events):
        timestamp = (base + timedelta(milliseconds=index)).isoformat()
        event = LaunchEvent.deterministic(
            "soak",
            "solana" if index % 2 == 0 else "bsc",
            f"token-{index}",
            timestamp,
            transaction_id=f"tx-{index}",
        )
        assert queue.offer(event) == "QUEUED"
        queued = await queue.queue.get()
        _, created = store.record_launch_event(queued)
        inserted += int(created)
        _, created_again = store.record_launch_event(queued)
        duplicates += int(not created_again)
        queue.task_done(queued)

    # A 3x (configurable) burst proves bounded backpressure and fresh recovery.
    burst = []
    for index in range(queue_size * burst_multiplier):
        timestamp = (base - timedelta(seconds=index + 1)).isoformat()
        event = LaunchEvent.deterministic(
            "burst",
            "solana" if index % 2 == 0 else "bsc",
            f"burst-{index}",
            timestamp,
            transaction_id=f"burst-tx-{index}",
        )
        if queue.offer(event) == "QUEUED":
            burst.append(event)
    while not queue.queue.empty():
        queued = await queue.queue.get()
        store.record_launch_event(queued)
        queue.task_done(queued)

    before_restart = queue.stats()
    store.close()
    restarted = Store(database)
    restarted.migrate()
    integrity = restarted.database_integrity()
    reconciliation = restarted.state_reconciliation()
    persisted = int(restarted.conn.execute("SELECT COUNT(*) FROM launch_events").fetchone()[0])
    duplicate_event_keys = int(
        restarted.conn.execute(
            "SELECT COUNT(*) FROM (SELECT event_key FROM launch_events GROUP BY event_key "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
    )
    restarted.close()
    _current, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = max(time.perf_counter() - started, 1e-9)
    expected_persisted = events + len(burst)
    state = (
        "PASS"
        if (
            inserted == events
            and duplicates == events
            and persisted == expected_persisted
            and duplicate_event_keys == 0
            and before_restart["size"] == 0
            and before_restart["dropped"] == queue_size * (burst_multiplier - 1)
            and integrity["healthy"]
            and reconciliation["difference"] == 0
        )
        else "FAIL"
    )
    return {
        "state": state,
        "events": events,
        "burst_multiplier": burst_multiplier,
        "queue": before_restart,
        "persisted": persisted,
        "duplicate_replays_suppressed": duplicates,
        "duplicate_event_keys": duplicate_event_keys,
        "database_integrity": integrity,
        "state_reconciliation": reconciliation,
        "elapsed_seconds": elapsed,
        "throughput_per_second": persisted / elapsed,
        "peak_traced_memory_bytes": peak_memory,
        "database_bytes": database.stat().st_size,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Accelerated V1.5 bounded load/restart soak")
    value.add_argument("--database", type=Path)
    value.add_argument("--events", type=int, default=10_000)
    value.add_argument("--queue-size", type=int, default=256)
    value.add_argument("--burst-multiplier", type=int, default=3)
    return value


def main() -> None:
    args = parser().parse_args()
    if args.events <= 0 or args.queue_size <= 0 or args.burst_multiplier < 2:
        raise SystemExit("events/queue-size must be positive and burst-multiplier >=2")
    if args.database is None:
        with tempfile.TemporaryDirectory(prefix="gambit-v15-soak-") as directory:
            report = asyncio.run(
                run(
                    Path(directory) / "soak.db",
                    events=args.events,
                    queue_size=args.queue_size,
                    burst_multiplier=args.burst_multiplier,
                )
            )
    else:
        report = asyncio.run(
            run(
                args.database,
                events=args.events,
                queue_size=args.queue_size,
                burst_multiplier=args.burst_multiplier,
            )
        )
    print(json.dumps(report, indent=2, default=str))
    raise SystemExit(0 if report["state"] == "PASS" else 1)


if __name__ == "__main__":
    main()
