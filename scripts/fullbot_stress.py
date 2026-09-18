#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memecoin_bot.alpha_engine import BoundedLaunchQueue, LaunchEvent
from memecoin_bot.database import Store
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))
    return ordered[index]


def event(
    index: int,
    *,
    source: str,
    creator: str,
    seconds: float = 0,
) -> CanonicalEvent:
    stamp = (
        datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
        + timedelta(seconds=index + seconds)
    ).isoformat()
    return CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        f"SoLaNaStress{index:08d}AbCd",
        "solana",
        "pumpfun",
        source,
        stamp,
        received_timestamp=stamp,
        available_timestamp=stamp,
        confidence=0.60 if source == "primary" else 0.95,
        payload={"creator": creator},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=20_000)
    parser.add_argument("--outbox", type=int, default=5_000)
    parser.add_argument("--queue-events", type=int, default=20_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="gambit-fullbot-stress-") as directory:
        store = Store(Path(directory) / "stress.db", Path("migrations"))
        store.migrate()
        fabric = CanonicalEventFabric(store)

        latencies_ms: list[float] = []
        expected_confirmed = 0
        expected_conflicts = 0
        for index in range(args.events):
            first = event(index, source="primary", creator=f"creator-{index}")
            tick = time.perf_counter_ns()
            result = fabric.publish(first)
            latencies_ms.append((time.perf_counter_ns() - tick) / 1_000_000)
            if not result.is_new:
                raise AssertionError("first publish unexpectedly deduplicated")

            if index % 2 == 0:
                confirmation = event(
                    index,
                    source="confirm",
                    creator=f"creator-{index}",
                )
                result = fabric.publish(confirmation)
                if result.conflict:
                    raise AssertionError("matching semantic payload became conflict")
                expected_confirmed += 1

            if index % 20 == 0:
                conflict = event(
                    index,
                    source="conflict",
                    creator=f"different-{index}",
                )
                result = fabric.publish(conflict)
                if not result.conflict:
                    raise AssertionError("semantic disagreement was not detected")
                expected_conflicts += 1

        canonical_rows = int(
            store.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
        )
        source_rows = int(
            store.conn.execute("SELECT COUNT(*) FROM canonical_event_sources").fetchone()[0]
        )
        conflict_rows = int(
            store.conn.execute("SELECT COUNT(*) FROM canonical_event_conflicts").fetchone()[0]
        )
        elevated_by_conflict = int(
            store.conn.execute(
                "SELECT COUNT(*) FROM canonical_events "
                "WHERE confidence>0.60 AND conflicts_json!='[]'"
            ).fetchone()[0]
        )

        processed = 0
        while True:
            batch = fabric.claim_pending(250)
            if not batch:
                break
            for row in batch:
                fabric.complete(row.event_id)
            processed += len(batch)

        # Stale poison claims must terminate at the ceiling rather than loop forever.
        poison = event(args.events + 1, source="primary", creator="poison")
        fabric.publish(poison)
        fabric.claim_pending(1)
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with store.conn:
            store.conn.execute(
                "UPDATE canonical_events SET claimed_at=?,processing_attempts=5 "
                "WHERE event_id=?",
                (stale, poison.event_id),
            )
        fabric.recover_stale_claims(lease_seconds=1, max_attempts=5)
        poison_state = str(
            store.conn.execute(
                "SELECT processing_status FROM canonical_events WHERE event_id=?",
                (poison.event_id,),
            ).fetchone()[0]
        )

        # Stress outbox leasing with concurrent claimers.
        now = datetime.now(UTC).isoformat()
        with store.conn:
            store.conn.executemany(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) "
                "VALUES(?,?,?,?)",
                [
                    (f"stress:{index}", "STRESS", "{}", now)
                    for index in range(args.outbox)
                ],
            )

        def claim() -> list[int]:
            return [int(row["id"]) for row in store.claim_outbox(100)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed_groups = list(pool.map(lambda _: claim(), range(8)))
        claimed_ids = [value for group in claimed_groups for value in group]
        duplicate_claims = len(claimed_ids) - len(set(claimed_ids))

        for outbox_id in claimed_ids:
            row = store.conn.execute(
                "SELECT claim_token FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            store.mark_outbox_error(
                outbox_id,
                "synthetic outage",
                row["claim_token"],
                base_delay_seconds=60,
            )
        retry_storm_claims = len(
            {
                int(row["id"])
                for row in store.claim_outbox(max(1, len(claimed_ids)))
                if int(row["id"]) in set(claimed_ids)
            }
        )

        # Legacy launch queue remains bounded under a flood.
        queue = BoundedLaunchQueue(512)
        queued = backpressure = 0
        for index in range(args.queue_events):
            launch = LaunchEvent.deterministic(
                "stress",
                "solana",
                f"QueueMint{index:08d}AbCd",
                f"2026-09-18T06:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
                transaction_id=f"Sig{index:08d}CaseSensitive",
            )
            result = queue.offer(launch)
            queued += int(result == "QUEUED")
            backpressure += int(result == "BACKPRESSURE")

        integrity = store.database_integrity()
        reconciliation = store.state_reconciliation()
        p50 = percentile(latencies_ms, 0.50)
        p95 = percentile(latencies_ms, 0.95)
        p99 = percentile(latencies_ms, 0.99)
        report: dict[str, Any] = {
            "version": "fullbot-stress-v1",
            "events_requested": args.events,
            "canonical_rows": canonical_rows,
            "canonical_source_rows": source_rows,
            "expected_confirmations": expected_confirmed,
            "expected_conflicts": expected_conflicts,
            "recorded_conflicts": conflict_rows,
            "conflicts_that_elevated_confidence": elevated_by_conflict,
            "processed_canonical_events": processed,
            "poison_event_state": poison_state,
            "outbox_rows": args.outbox,
            "concurrent_claimed": len(claimed_ids),
            "duplicate_outbox_claims": duplicate_claims,
            "immediate_retry_storm_claims": retry_storm_claims,
            "launch_queue": {
                "capacity": 512,
                "requested": args.queue_events,
                "queued": queued,
                "backpressure": backpressure,
                "actual_size": queue.queue.qsize(),
            },
            "publish_latency_ms": {
                "median": statistics.median(latencies_ms),
                "p50": p50,
                "p95": p95,
                "p99": p99,
                "max": max(latencies_ms),
            },
            "database_integrity": integrity,
            "state_reconciliation": reconciliation,
            "seconds": time.perf_counter() - started,
        }
        report["passed"] = (
            canonical_rows == args.events
            and conflict_rows == expected_conflicts
            and elevated_by_conflict == 0
            and processed == args.events
            and poison_state == "FAILED"
            and duplicate_claims == 0
            and retry_storm_claims == 0
            and queued == 512
            and backpressure == args.queue_events - 512
            and queue.queue.qsize() == 512
            and integrity["healthy"]
            and reconciliation["reconciled"]
            and p99 < 10.0
        )
        store.close()

    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
