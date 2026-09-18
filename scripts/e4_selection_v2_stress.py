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
    price: float | None = None,
    creator: str | None = None,
    complete: bool = False,
    signature: str | None = None,
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
        price_sol=price,
        creator=creator,
        complete=complete,
        signature=signature,
    )


def build_state(
    rng: random.Random,
    index: int,
    creators: list[str],
    wallet: str,
) -> core.TokenState:
    mint = f"stress-{index}"
    creator = (
        rng.choice(creators)
        if creators and rng.random() < 0.35
        else f"unknown-{index % 1000}"
    )
    start = 1_800_000_000_000_000_000 + index * 1_000_000_000
    fdv = rng.uniform(900.0, 12_500.0)
    state = core.TokenState(mint)
    state.apply(
        event(
            1,
            core.EventKind.CREATE,
            mint,
            start,
            fdv=fdv,
            price=1.0,
            creator=creator,
        ),
        wallet,
    )

    # Most real E4 selections had a creator seed, but fuzz the missing-seed path.
    if rng.random() < 0.88:
        seed = rng.uniform(0.01, 3.5)
        state.apply(
            event(
                2,
                core.EventKind.BUY,
                mint,
                start + rng.randint(2_000_000, 25_000_000),
                trader=creator,
                sol=seed,
                fdv=fdv,
                price=rng.uniform(1.0, 1.08),
                creator=creator,
                signature=f"seed-{index}",
            ),
            wallet,
        )

    # A minority is deliberately shaped like an evidence-backed public E4 family;
    # the rest is noisy launch flow. This catches both degenerate reject-all and
    # degenerate accept-everything implementations.
    strong = rng.random() < 0.24
    buyer_count = rng.randint(3, 8) if strong else rng.randint(0, 6)
    public_price = rng.uniform(1.16, 1.65) if strong else rng.uniform(0.92, 1.22)
    for offset in range(buyer_count):
        amount = (
            rng.uniform(1.4, 3.2)
            if strong
            else min(1.5, rng.expovariate(2.0))
        )
        state.apply(
            event(
                10 + offset,
                core.EventKind.BUY,
                mint,
                start + 35_000_000 + offset * rng.randint(8_000_000, 28_000_000),
                trader=f"buyer-{rng.randint(0, 1000)}",
                sol=amount,
                fdv=fdv,
                price=public_price,
                creator=creator,
                signature=f"bundle-{index}-{offset // 2}",
            ),
            wallet,
        )

    # Any visible sell before confirmation must hard-veto.
    if rng.random() < 0.16:
        state.apply(
            event(
                100,
                core.EventKind.SELL,
                mint,
                start + rng.randint(60_000_000, 280_000_000),
                trader=f"seller-{rng.randint(0, 500)}",
                sol=min(2.0, rng.expovariate(2.0)),
                fdv=fdv,
                price=public_price,
                creator=creator,
            ),
            wallet,
        )

    # Exercise stale-decision and terminal-state rejection.
    if rng.random() < 0.05:
        state.apply(
            event(
                150,
                core.EventKind.CURVE,
                mint,
                start + rng.randint(400_000_000, 900_000_000),
                fdv=fdv,
                price=public_price,
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
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
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
        family_counts: dict[str, int] = {}

        for index in range(args.iterations):
            state = build_state(rng, index, creators, settings.wallet or "")
            started = time.perf_counter_ns()
            try:
                decision = policy.decision(state)
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - fuzz telemetry
                exceptions.append(f"{type(exc).__name__}:{exc}")
                continue
            timings.append(time.perf_counter_ns() - started)
            accepted += int(decision.accepted)
            if decision.accepted:
                family_counts[decision.family] = family_counts.get(decision.family, 0) + 1
            has_sell = any(
                row.kind in {core.EventKind.SELL, core.EventKind.PUMPSWAP_SELL}
                for row in state.events
            )
            creator = str(state.creator or "")
            has_seed = any(
                row.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
                and row.trader == creator
                and row.sol_amount >= policy.config.minimum_creator_seed_sol
                for row in state.events
            )
            age_ms = (
                (state.latest_ns - state.created_ns) / 1_000_000
                if state.created_ns is not None
                else float("inf")
            )
            if decision.accepted and (
                state.complete
                or state.migrated
                or state.wallet_touched
                or not state.fdv_usd
                or state.fdv_usd
                > min(settings.max_entry_fdv_usd, policy.config.maximum_entry_fdv_usd)
                or has_sell
                or not has_seed
                or age_ms > policy.config.maximum_entry_age_ms
            ):
                invalid_accepts += 1
            if decision.fraction > min(
                settings.max_position_fraction,
                policy.config.maximum_position_fraction,
            ) + 1e-12:
                oversized += 1
            if index < 500:
                second = policy.decision(state)
                if (
                    second.accepted != decision.accepted
                    or second.family != decision.family
                    or abs(second.score - decision.score) > 1e-12
                    or abs(second.fraction - decision.fraction) > 1e-12
                ):
                    nondeterministic += 1

        # Causal-memory load test: no duplicate outcome may be counted twice.
        memory_updates = 0
        duplicate_updates = 0
        discard_updates = 0
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
            if index % 10 == 0:
                discard_updates += int(policy.memory.discard_pending(mint))
                continue
            outcome = 0.20 if index % 3 else -0.10
            memory_updates += int(policy.memory.resolve(mint, outcome))
            duplicate_updates += int(policy.memory.resolve(mint, outcome))

        p50 = percentile(timings, 0.50)
        p95 = percentile(timings, 0.95)
        p99 = percentile(timings, 0.99)
        accept_rate = accepted / max(len(timings), 1)
        report: dict[str, Any] = {
            "version": "e4-selection-v2-stress-v2",
            "iterations": args.iterations,
            "seed": args.seed,
            "decisions_completed": len(timings),
            "accepted": accepted,
            "accept_rate": accept_rate,
            "accepted_families": family_counts,
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
                "discarded_failed_entries": discard_updates,
                "duplicate_memory_updates": duplicate_updates,
            },
            "limits": {
                "max_p95_us": args.max_p95_us,
                "max_p99_us": args.max_p99_us,
                "minimum_accepts": max(10, args.iterations // 1000),
                "maximum_accept_rate": 0.25,
            },
        }
        passed = (
            not exceptions
            and invalid_accepts == 0
            and oversized == 0
            and nondeterministic == 0
            and memory_updates == 1_800
            and discard_updates == 200
            and duplicate_updates == 0
            and accepted >= max(10, args.iterations // 1000)
            and 0 < accept_rate <= 0.25
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
