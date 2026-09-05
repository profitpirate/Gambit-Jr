#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import e4_v12_true_latency_replay as replay


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def audit_events(paths: Sequence[Path]) -> dict[str, Any]:
    total = 0
    reversible = 0
    ratios: list[float] = []
    errors_bps: list[float] = []
    examples: list[dict[str, Any]] = []
    for path in paths:
        grouped = replay.load_events(path)
        for mint, rows in grouped.items():
            states = replay.reserve_states(rows)
            for event in replay.e4_rows(rows):
                if str(event.get("kind") or "").upper() not in replay.BUY_KINDS:
                    continue
                total += 1
                timestamp = replay.integer(event.get("received_ns"))
                post = replay.state_at_or_before(states, timestamp)
                source_sol = max(0.0, finite(event.get("sol_amount")))
                source_tokens = max(0.0, finite(event.get("token_amount")))
                if post is None or source_sol <= 0 or source_tokens <= 0:
                    continue
                pre_sol = post.virtual_sol - source_sol
                pre_tokens = post.virtual_tokens + source_tokens
                pre_real = post.real_tokens + source_tokens if math.isfinite(post.real_tokens) else float("inf")
                if pre_sol <= 0 or pre_tokens <= 0:
                    continue
                reconstructed = replay.buy_tokens(
                    source_sol,
                    replay.CurveState(timestamp, pre_sol, pre_tokens, pre_real, post.fdv_usd),
                )
                if reconstructed <= 0:
                    continue
                reversible += 1
                ratio = reconstructed / source_tokens
                ratios.append(ratio)
                error_bps = abs(ratio - 1.0) * 10_000.0
                errors_bps.append(error_bps)
                if len(examples) < 20:
                    examples.append(
                        {
                            "mint": mint,
                            "source_sol": source_sol,
                            "source_tokens": source_tokens,
                            "reconstructed_tokens": reconstructed,
                            "ratio": ratio,
                            "absolute_error_bps": error_bps,
                        }
                    )
    within_100_bps = sum(error <= 100.0 for error in errors_bps)
    within_500_bps = sum(error <= 500.0 for error in errors_bps)
    result = {
        "version": "e4-v12-curve-unit-audit-v1",
        "total_e4_buys": total,
        "reversible_buys": reversible,
        "coverage": reversible / total if total else 0.0,
        "median_ratio": statistics.median(ratios) if ratios else None,
        "median_absolute_error_bps": statistics.median(errors_bps) if errors_bps else None,
        "p95_absolute_error_bps": (
            sorted(errors_bps)[min(len(errors_bps) - 1, round((len(errors_bps) - 1) * 0.95))]
            if errors_bps
            else None
        ),
        "within_100_bps_fraction": within_100_bps / reversible if reversible else 0.0,
        "within_500_bps_fraction": within_500_bps / reversible if reversible else 0.0,
        "examples": examples,
    }
    result["passed"] = bool(
        total >= 20
        and result["coverage"] >= 0.80
        and result["median_ratio"] is not None
        and 0.90 <= float(result["median_ratio"]) <= 1.10
        and result["within_500_bps_fraction"] >= 0.75
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate event/reserve units using E4's observed buy exchange")
    parser.add_argument("--events", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_events(args.events)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: result[key] for key in result if key != "examples"}, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
