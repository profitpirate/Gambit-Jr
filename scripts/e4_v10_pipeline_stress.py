#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from memecoin_bot import e4_hardening_v10 as v10

core = v10.core


def percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * q))]


def make_state(mint: str, creator: str, *, buyers: int = 0):
    now = time.time_ns()
    state = core.TokenState(mint)
    state.apply(
        core.Event(
            event_id=1,
            kind=core.EventKind.CREATE,
            mint=mint,
            source_ns=now,
            received_ns=now,
            trader=creator,
            creator=creator,
            sol_amount=0.0,
            token_amount=0.0,
            price_sol=1e-6,
            fdv_usd=3_000.0,
            signature=f"create-{mint}",
        ),
        None,
    )
    state.apply(
        core.Event(
            event_id=2,
            kind=core.EventKind.BUY,
            mint=mint,
            source_ns=now + 100_000,
            received_ns=now + 100_000,
            trader=creator,
            creator=creator,
            sol_amount=2.0,
            token_amount=1_000_000.0,
            price_sol=1.04e-6,
            fdv_usd=3_200.0,
            signature=f"seed-{mint}",
        ),
        None,
    )
    for index in range(buyers):
        state.apply(
            core.Event(
                event_id=3 + index,
                kind=core.EventKind.BUY,
                mint=mint,
                source_ns=now + 200_000 + index * 100_000,
                received_ns=now + 200_000 + index * 100_000,
                trader=f"buyer-{index}",
                creator=creator,
                sol_amount=2.0,
                token_amount=500_000.0,
                price_sol=(1.08 + 0.05 * index) * 1e-6,
                fdv_usd=3_500.0 + index * 200.0,
                signature=f"buyer-{mint}-{index}",
            ),
            None,
        )
    return state


def choose_creators() -> tuple[str, str, str]:
    profiles = list(v10.PIPELINES.creators.snapshot.profiles.values())
    elite = next((row.creator for row in profiles if row.tier.name == "ELITE"), None)
    approved = next((row.creator for row in profiles if row.tier.name == "APPROVED"), None)
    negative = next((row.creator for row in profiles if row.tier.name == "NEGATIVE"), None)
    if not elite:
        raise RuntimeError("creator model contains no ELITE creator")
    if not approved:
        approved = elite
    if not negative:
        # The persisted 316-trade expectancy model should contain at least one
        # 0W/4L creator; fail closed if the model was accidentally truncated.
        raise RuntimeError("creator model contains no NEGATIVE creator")
    return elite, approved, negative


def run_one(index: int, elite: str, approved: str, negative: str) -> tuple[int, str, bool, str]:
    selector = index % 4
    creator = elite if selector == 0 else approved if selector == 1 else negative if selector == 2 else f"unknown-{index}"
    buyers = 6 if selector in {2, 3} else 0
    state = make_state(f"stress-{index}", creator, buyers=buyers)
    started = time.perf_counter_ns()
    accepted, _score, _fraction, reason, _features = core.E4Policy(
        core.Settings(model_path=Path("missing.json"))
    ).entry(state)
    elapsed = time.perf_counter_ns() - started
    expected = selector in {0, 1}
    return elapsed, creator, accepted == expected, reason


def narrative_stress(iterations: int) -> dict[str, Any]:
    cache = v10.PIPELINES.narratives
    now = time.time_ns()
    for index in range(2_000):
        cache.observe(
            source="stress-x",
            source_account=f"account-{index % 50}",
            text=f"Unique narrative phrase alpha{index} rocket{index} tonight",
            created_ns=now - 1_000_000_000,
            observed_ns=now - 900_000_000,
            authority=0.90,
            engagement_velocity=0.75,
        )
    latencies: list[int] = []
    matches = 0
    for index in range(iterations):
        started = time.perf_counter_ns()
        match = cache.match_launch(
            name=f"Unique narrative phrase alpha{index % 2_000}",
            symbol=f"ALPHA{index % 2_000}",
            uri=None,
            mint=None,
            launch_ns=now,
        )
        latencies.append(time.perf_counter_ns() - started)
        matches += int(match.matched)
    return {
        "iterations": iterations,
        "matches": matches,
        "p50_ns": percentile(latencies, 0.50),
        "p95_ns": percentile(latencies, 0.95),
        "p99_ns": percentile(latencies, 0.99),
        "max_ns": max(latencies, default=0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress all three E4 V10 decision pipelines")
    parser.add_argument("--iterations", type=int, default=200_000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--narrative-iterations", type=int, default=50_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    elite, approved, negative = choose_creators()
    started = time.perf_counter()
    results: list[tuple[int, str, bool, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(
            lambda index: run_one(index, elite, approved, negative),
            range(args.iterations),
            chunksize=128,
        ):
            results.append(result)
    elapsed_seconds = time.perf_counter() - started
    latencies = [row[0] for row in results]
    failures = [row for row in results if not row[2]]
    reasons = Counter(row[3].split(" decision_ns=", 1)[0] for row in results)
    narrative = narrative_stress(args.narrative_iterations)

    report = {
        "version": "e4-v10-pipeline-stress-v1",
        "creator_registry_profiles": len(v10.PIPELINES.creators.snapshot.profiles),
        "iterations": args.iterations,
        "workers": args.workers,
        "elapsed_seconds": elapsed_seconds,
        "operations_per_second": args.iterations / elapsed_seconds,
        "decision_p50_ns": percentile(latencies, 0.50),
        "decision_p95_ns": percentile(latencies, 0.95),
        "decision_p99_ns": percentile(latencies, 0.99),
        "decision_max_ns": max(latencies, default=0),
        "decision_budget_ns": 36_000_000,
        "decision_budget_pass": bool(latencies and percentile(latencies, 0.99) <= 36_000_000),
        "decision_correctness_failures": len(failures),
        "sample_failures": [
            {"creator": row[1], "reason": row[3], "latency_ns": row[0]}
            for row in failures[:20]
        ],
        "reason_distribution": dict(reasons),
        "narrative": narrative,
        "narrative_budget_pass": narrative["p99_ns"] is not None and narrative["p99_ns"] <= 36_000_000,
        "pipeline_metrics": v10.PIPELINES.metrics.snapshot(),
        "selected_test_creators": {
            "elite": elite,
            "approved": approved,
            "negative": negative,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "decision_p95_ms": report["decision_p95_ns"] / 1e6,
        "decision_p99_ms": report["decision_p99_ns"] / 1e6,
        "decision_max_ms": report["decision_max_ns"] / 1e6,
        "correctness_failures": len(failures),
        "narrative_p99_ms": narrative["p99_ns"] / 1e6,
        "ops_per_second": report["operations_per_second"],
    }), flush=True)
    return 0 if report["decision_budget_pass"] and report["narrative_budget_pass"] and not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
