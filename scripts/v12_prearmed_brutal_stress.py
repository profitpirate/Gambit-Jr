#!/usr/bin/env python3
"""Brutal offline stress/fuzz validation for the frozen V12 Pre-Armed policy.

No network, wallet, signer, RPC, route, or transaction submission is used.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from memecoin_bot.e4_prearmed_policy import FrozenPrearmedPolicy
from memecoin_bot.e4_prearmed_readiness import FROZEN_MODEL_SHA256, stable_hash


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "research" / "v12-pre-armed-100-trade-frozen-model.json"


def selector_expected(
    *,
    creator_known: bool,
    handle_known: bool,
    age_seconds: float,
    prior_attempts: int,
    creator_seed_sol: float,
    mayhem: bool,
) -> bool:
    return (
        creator_known
        and handle_known
        and 0.0 <= age_seconds <= 10.0
        and prior_attempts >= 1
        and creator_seed_sol >= 2.0
        and not mayhem
    )


def run_selector_fuzz(policy: FrozenPrearmedPolicy, iterations: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    creator = "29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8"
    known_handle = "ddddddd8a"
    mismatches = []
    timings = []
    reasons: Counter[str] = Counter()
    base_ns = 2_000_000_000_000
    unknown_creator = "11111111111111111111111111111111"

    for index in range(iterations):
        creator_known = rng.random() > 0.16
        handle_known = rng.random() > 0.18
        age_seconds = rng.uniform(-2.0, 14.0)
        attempts = rng.choice([0, 1, 1, 2, 3])
        seed_sol = rng.uniform(0.0, 4.0)
        mayhem = rng.random() < 0.08

        selected_creator = creator if creator_known else unknown_creator
        selected_handle = known_handle if handle_known else "not_frozen"
        started = time.perf_counter_ns()
        result = policy.select(
            creator=selected_creator,
            social_handle=selected_handle,
            social_status_ns=base_ns - int(age_seconds * 1_000_000_000),
            create_ns=base_ns,
            prior_e4_attempts=attempts,
            creator_seed_sol=seed_sol,
            mayhem_mode=mayhem,
        )
        timings.append(time.perf_counter_ns() - started)
        reasons[result.reason] += 1
        expected = selector_expected(
            creator_known=creator_known,
            handle_known=handle_known,
            age_seconds=age_seconds,
            prior_attempts=attempts,
            creator_seed_sol=seed_sol,
            mayhem=mayhem,
        )
        if result.accepted != expected and len(mismatches) < 20:
            mismatches.append(
                {
                    "index": index,
                    "expected": expected,
                    "actual": result.accepted,
                    "reason": result.reason,
                    "creator_known": creator_known,
                    "handle_known": handle_known,
                    "age_seconds": age_seconds,
                    "attempts": attempts,
                    "creator_seed_sol": seed_sol,
                    "mayhem": mayhem,
                }
            )

    timings.sort()
    return {
        "iterations": iterations,
        "mismatch_count": len(mismatches),
        "mismatch_examples": mismatches,
        "reasons": dict(reasons),
        "median_us": statistics.median(timings) / 1_000 if timings else 0.0,
        "p99_us": timings[min(len(timings) - 1, int(len(timings) * 0.99))] / 1_000 if timings else 0.0,
    }


def run_entry_guard_fuzz(policy: FrozenPrearmedPolicy, iterations: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed ^ 0xA510)
    mismatches = []
    boundary_cases = [
        (0.65, 1.5, True),
        (0.649999999, 1.0, False),
        (1.0, 1.500000001, False),
        (1.0, 1.0, True),
    ]
    for ratio, multiple, expected in boundary_cases:
        result = policy.entry_guard(
            expected_token_output=100.0,
            current_token_output=100.0 * ratio,
            create_price=1.0,
            fill_price=multiple,
        )
        if result.accepted != expected:
            mismatches.append({"ratio": ratio, "multiple": multiple, "expected": expected, "actual": result.accepted})

    for index in range(iterations):
        ratio = rng.uniform(0.3, 1.2)
        multiple = rng.uniform(0.5, 2.2)
        expected = ratio >= 0.65 and multiple <= 1.5
        result = policy.entry_guard(
            expected_token_output=100.0,
            current_token_output=100.0 * ratio,
            create_price=1.0,
            fill_price=multiple,
        )
        if result.accepted != expected and len(mismatches) < 20:
            mismatches.append(
                {
                    "index": index,
                    "ratio": ratio,
                    "multiple": multiple,
                    "expected": expected,
                    "actual": result.accepted,
                    "reason": result.reason,
                }
            )
    return {"iterations": iterations, "mismatch_count": len(mismatches), "mismatch_examples": mismatches}


def run_exit_grid(policy: FrozenPrearmedPolicy) -> dict[str, Any]:
    mismatches = []
    cases = 0
    prices = [0.69, 0.70, 0.71, 1.0, 1.149999, 1.15, 1.16, 1.4]
    peaks = [1.0, 1.15, 1.4, 2.0]
    ages = [0, 500, 1999, 2000, 3000]
    for first_done in (False, True):
        for price in prices:
            for peak in peaks:
                for age in ages:
                    cases += 1
                    result = policy.exit(
                        entry_price=1.0,
                        current_price=price,
                        peak_price=max(peak, price),
                        first_partial_done=first_done,
                        age_ms=age,
                    )
                    if result.action not in {"HOLD", "SELL_PARTIAL", "SELL_ALL"}:
                        mismatches.append({"price": price, "peak": peak, "age": age, "first_done": first_done, "action": result.action})
                    if result.fraction < 0.0 or result.fraction > 1.0:
                        mismatches.append({"price": price, "peak": peak, "age": age, "first_done": first_done, "fraction": result.fraction})
                    if not first_done and price >= 1.15 and result.action != "SELL_PARTIAL":
                        mismatches.append({"case": "first_take", "price": price, "peak": peak, "age": age, "actual": result.action})
                    if age >= 2000 and result.action == "HOLD":
                        mismatches.append({"case": "max_hold", "price": price, "peak": peak, "age": age, "actual": result.action})
    return {"cases": cases, "mismatch_count": len(mismatches), "mismatch_examples": mismatches[:20]}


def run_hash_tamper_test(raw_model: dict[str, Any]) -> dict[str, Any]:
    original = stable_hash(raw_model)
    mutated = json.loads(json.dumps(raw_model))
    mutated["selector"]["minimum_creator_seed_sol"] = 1.999
    tampered = stable_hash(mutated)
    rejected = False
    try:
        FrozenPrearmedPolicy(mutated)
    except ValueError:
        rejected = True
    return {
        "original_hash": original,
        "expected_hash": FROZEN_MODEL_SHA256,
        "tampered_hash": tampered,
        "tamper_changes_hash": tampered != original,
        "tampered_model_rejected": rejected,
    }


def threaded_hot_path(policy: FrozenPrearmedPolicy, calls: int, workers: int) -> dict[str, Any]:
    creator = "29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8"
    handle = "ddddddd8a"
    create_ns = 20_000_000_000

    def one(_: int) -> bool:
        return policy.select(
            creator=creator,
            social_handle=handle,
            social_status_ns=create_ns - 1_000_000_000,
            create_ns=create_ns,
            creator_seed_sol=2.0,
            mayhem_mode=False,
        ).accepted

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, range(calls), chunksize=512))
    elapsed = time.perf_counter() - started
    return {
        "calls": calls,
        "workers": workers,
        "failures": sum(not value for value in results),
        "elapsed_seconds": elapsed,
        "calls_per_second": calls / max(elapsed, 1e-9),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--threaded-calls", type=int, default=50_000)
    parser.add_argument("--seed", type=int, default=120012)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    raw = json.loads(MODEL.read_text(encoding="utf-8"))
    policy = FrozenPrearmedPolicy(raw)
    policy.assert_finite()
    result = {
        "version": "v12-prearmed-brutal-stress-v1",
        "model_sha256": stable_hash(raw),
        "selector": run_selector_fuzz(policy, args.iterations, args.seed),
        "entry_guard": run_entry_guard_fuzz(policy, args.iterations, args.seed),
        "exit_grid": run_exit_grid(policy),
        "tamper": run_hash_tamper_test(raw),
        "threaded_hot_path": threaded_hot_path(policy, args.threaded_calls, args.threads),
    }
    failures = (
        result["selector"]["mismatch_count"]
        + result["entry_guard"]["mismatch_count"]
        + result["exit_grid"]["mismatch_count"]
        + result["threaded_hot_path"]["failures"]
        + (0 if result["tamper"]["tampered_model_rejected"] else 1)
    )
    result["passed"] = failures == 0
    result["failure_count"] = failures
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
