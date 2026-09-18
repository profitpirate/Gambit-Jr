#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from memecoin_bot import e4_live as core
from memecoin_bot.e4_selection_v2 import UnifiedE4Policy


def event(
    event_id: int,
    kind: core.EventKind,
    mint: str,
    ns: int,
    *,
    trader: str | None = None,
    sol: float = 0.0,
    fdv: float | None = None,
    creator: str | None = None,
    complete: bool = False,
) -> core.Event:
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=ns,
        received_ns=ns,
        trader=trader,
        sol_amount=sol,
        fdv_usd=fdv,
        creator=creator,
        complete=complete,
    )


def build_state(
    rng: random.Random,
    index: int,
    creators: list[str],
    wallet: str,
) -> core.TokenState:
    mint = f"stress-{index}"
    creator = rng.choice(creators) if creators and rng.random() < 0.55 else f"unknown-{index % 1000}"
    start = 1_800_000_000_000_000_000 + index * 20_000_000
    fdv = rng.uniform(900.0, 12_500.0)
    state = core.TokenState(mint)
    state.apply(
        event(1, core.EventKind.CREATE, mint, start, fdv=fdv, creator=creator),
        wallet,
    )
    buyer_count = rng.randint(0, 10)
    for offset in range(buyer_count):
        amount = min(3.0, rng.expovariate(1.5))
        state.apply(
            event(
                10 + offset,
                core.EventKind.BUY,
                mint,
                start + (offset + 1) * rng.randint(2_000_000, 80_000_000),
                trader=f"buyer-{rng.randint(0, 500)}",
                sol=amount,
                fdv=fdv,
                creator=creator,
            ),
            wallet,
        )
    for offset in range(rng.randint(0, 4)):
        state.apply(
            event(
                100 + offset,
                core.EventKind.SELL,
                mint,
                start + rng.randint(20_000_000, 900_000_000),
                trader=f"seller-{rng.randint(0, 500)}",
                sol=min(2.0, rng.expovariate(2.0)),
                fdv=fdv,
                creator=creator,
            ),
            wallet,
        )
    if rng.random() < 0.003:
        state.complete = True
    if rng.random() < 0.002:
        state.wallet_touched = True
    return state


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))
    return ordered[index] / 1_000.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50_000)
    parser.add_argument("--seed", type=int, default=56_201)
    parser.add_argument("--max-p95-us", type=float, default=2_500.0)
    parser.add_argument("--max-p99-us", type=float, default=5_000.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="e4-selection-v2-") as tmp:
        os.environ["E4_SELECTION_MEMORY_PATH"] = str(Path(tmp) / "memory.json")
        os.environ["E4_SELECTION_MEMORY_PERSIST"] = "0"
        os.environ["E4_SELECTION_V2_ENABLED"] = "true"
        settings = core.Settings(
            live=False,
            wallet="stress-wallet",
            model_path=Path(tmp) / "no-logistic-model.json",
            max_position_fraction=0.20,
        )
        policy = UnifiedE4Policy(settings)
        creators = list(policy.library.expectancy) + list(policy.library.discovered)
        timings: list[int] = []
        accepted = 0
        invalid_accepts = 0
        oversized = 0
        nondeterministic = 0
        exceptions: list[str] = []

        for index in range(args.iterations):
            state = build_state(rng, index, creators, settings.wallet or "")
            started = time.perf_counter_ns()
            try:
                decision = policy.decision(state)
            except Exception as exc:  # pragma: no cover - stress telemetry
                exceptions.append(f"{type(exc).__name__}:{exc}")
                continue
            timings.append(time.perf_counter_ns() - started)
            accepted += int(decision.accepted)
            if decision.accepted and (
                state.complete
                or state.migrated
                or state.wallet_touched
                or not state.fdv_usd
                or state.fdv_usd > settings.max_entry_fdv_usd
            ):
                invalid_accepts += 1
            if decision.fraction > min(
                settings.max_position_fraction, policy.config.maximum_position_fraction
            ) + 1e-12:
                oversized += 1
            if index < 500:
                second = policy.decision(state)
                if (
                    second.accepted != decision.accepted
                    or abs(second.score - decision.score) > 1e-12
                    or abs(second.fraction - decision.fraction) > 1e-12
                ):
                    nondeterministic += 1

        # Causal-memory load test: no duplicate outcome may be counted twice.
        memory_updates = 0
        duplicate_updates = 0
        for index in range(2_000):
            mint = f"memory-{index}"
            buyers = [f"buyer-{index % 200}", f"buyer-{(index + 7) % 200}"]
            policy.memory.register_entry(
                mint,
                creator=f"creator-{index % 80}",
                buyers=buyers,
                score=0.70,
                decision_ns=index,
            )
            outcome = 0.20 if index % 3 else -0.10
            memory_updates += int(policy.memory.resolve(mint, outcome))
            duplicate_updates += int(policy.memory.resolve(mint, outcome))

        p50 = percentile(timings, 0.50)
        p95 = percentile(timings, 0.95)
        p99 = percentile(timings, 0.99)
        report: dict[str, Any] = {
            "version": "e4-selection-v2-stress-v1",
            "iterations": args.iterations,
            "seed": args.seed,
            "decisions_completed": len(timings),
            "accepted": accepted,
            "accept_rate": accepted / max(len(timings), 1),
            "latency_microseconds": {
                "median": statistics.median(timings) / 1_000.0 if timings else None,
                "p50": p50,
                "p95": p95,
                "p99": p99,
            },
            "invariants": {
                "exceptions": len(exceptions),
                "invalid_accepts": invalid_accepts,
                "oversized_positions": oversized,
                "nondeterministic_decisions": nondeterministic,
                "memory_updates": memory_updates,
                "duplicate_memory_updates": duplicate_updates,
            },
            "limits": {
                "max_p95_us": args.max_p95_us,
                "max_p99_us": args.max_p99_us,
            },
        }
        passed = (
            not exceptions
            and invalid_accepts == 0
            and oversized == 0
            and nondeterministic == 0
            and memory_updates == 2_000
            and duplicate_updates == 0
            and p95 <= args.max_p95_us
            and p99 <= args.max_p99_us
        )
        report["passed"] = passed
        if exceptions:
            report["exception_examples"] = exceptions[:5]
        encoded = json.dumps(report, indent=2, sort_keys=True)
        print(encoded)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
