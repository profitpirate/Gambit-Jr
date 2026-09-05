#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import e4_v12_reactive_output_guard_search as base
import e4_v12_true_latency_replay as replay


def finite(value: Any, default: float = 0.0) -> float:
    return replay.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return replay.integer(value, default)


@dataclass(frozen=True)
class Rule:
    output_shortfall_bps: int
    minimum_source_sol: float
    maximum_source_sol: float
    minimum_source_per_10k_fdv: float
    maximum_source_per_10k_fdv: float
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    minimum_prior_creator_wins: int
    maximum_prior_creator_losses: int
    minimum_creator_seed_sol: float
    minimum_outside_sol: float
    minimum_unique_buyers: int
    maximum_age_ms: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def accepts(candidate: base.Candidate, rule: Rule) -> bool:
    relative = candidate.source_sol / max(1.0, candidate.fdv_usd) * 10_000.0
    return bool(
        rule.minimum_source_sol <= candidate.source_sol <= rule.maximum_source_sol
        and rule.minimum_source_per_10k_fdv <= relative <= rule.maximum_source_per_10k_fdv
        and rule.minimum_fdv_usd <= candidate.fdv_usd <= rule.maximum_fdv_usd
        and candidate.prior_creator_wins >= rule.minimum_prior_creator_wins
        and candidate.prior_creator_losses <= rule.maximum_prior_creator_losses
        and candidate.creator_seed_sol >= rule.minimum_creator_seed_sol
        and candidate.outside_sol >= rule.minimum_outside_sol
        and candidate.unique_buyers >= rule.minimum_unique_buyers
        and candidate.age_ms <= rule.maximum_age_ms
    )


def permissive(rule: Rule) -> base.Rule:
    return base.Rule(
        output_shortfall_bps=rule.output_shortfall_bps,
        maximum_source_sol=float("inf"),
        minimum_fdv_usd=0.0,
        maximum_fdv_usd=float("inf"),
        minimum_prior_creator_wins=0,
        minimum_creator_seed_sol=0.0,
        maximum_age_ms=float("inf"),
    )


def evaluate(
    run_map: Mapping[str, replay.RunData],
    candidates: Sequence[base.Candidate],
    rule: Rule,
    latency_ms: float,
) -> dict[str, Any]:
    chosen = [row for row in candidates if accepts(row, rule)]
    return base.evaluate(run_map, chosen, permissive(rule), latency_ms)


def passes(grid: Mapping[str, Mapping[str, Any]], minimum_trades: int) -> bool:
    return base.passes(grid, minimum_trades)


def compact(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return base.compact(grid)


def rules(candidates: Sequence[base.Candidate]) -> list[Rule]:
    source_values = sorted({round(row.source_sol, 6) for row in candidates if row.source_sol > 0})
    relative_values = sorted({
        round(row.source_sol / max(1.0, row.fdv_usd) * 10_000.0, 6)
        for row in candidates if row.source_sol > 0 and row.fdv_usd > 0
    })

    def quantiles(values: Sequence[float], probabilities: Sequence[float]) -> list[float]:
        if not values:
            return [0.0]
        return sorted({
            values[min(len(values) - 1, max(0, round((len(values) - 1) * probability)))]
            for probability in probabilities
        })

    source_thresholds = quantiles(source_values, (0.0, 0.15, 0.30, 0.50, 0.70, 0.85, 1.0))
    relative_thresholds = quantiles(relative_values, (0.0, 0.20, 0.40, 0.60, 0.80, 1.0))
    source_bands = []
    for minimum in source_thresholds:
        for maximum in source_thresholds:
            if maximum >= minimum:
                source_bands.append((minimum, maximum))
    relative_bands = []
    for minimum in relative_thresholds:
        for maximum in relative_thresholds:
            if maximum >= minimum:
                relative_bands.append((minimum, maximum))

    output = []
    for floor in (200, 400, 600, 800, 1_000, 1_250, 1_500, 2_000):
        for source_min, source_max in source_bands:
            for relative_min, relative_max in relative_bands:
                for fdv_min, fdv_max in (
                    (2_750.0, 5_000.0),
                    (3_200.0, 7_500.0),
                    (4_000.0, 8_500.0),
                    (2_750.0, 10_000.0),
                ):
                    for creator_wins, creator_losses in ((0, 99), (1, 1), (2, 1)):
                        for seed, outside, buyers in (
                            (0.0, 0.0, 0),
                            (0.5, 0.0, 0),
                            (0.5, 0.25, 1),
                            (1.5, 1.0, 1),
                        ):
                            for age in (50.0, 150.0, 400.0, 1_500.0):
                                output.append(Rule(
                                    floor,
                                    source_min,
                                    source_max,
                                    relative_min,
                                    relative_max,
                                    fdv_min,
                                    fdv_max,
                                    creator_wins,
                                    creator_losses,
                                    seed,
                                    outside,
                                    buyers,
                                    age,
                                ))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Search E4 stake-confidence plus strict-output entry thesis")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(runs) < 8:
        parser.error("at least eight chronological runs are required")
    run_map = {run.run_id: run for run in runs}
    candidates = base.build_candidates(runs)
    run_ids = [run.run_id for run in runs]
    train_ids = set(run_ids[:-4])
    validation_ids = set(run_ids[-4:-2])
    holdout_ids = set(run_ids[-2:])
    train = [row for row in candidates if row.run_id in train_ids]
    validation = [row for row in candidates if row.run_id in validation_ids]
    holdout = [row for row in candidates if row.run_id in holdout_ids]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    seen_train: set[tuple[str, ...]] = set()
    shortlist = []
    for rule in rules(train):
        train_mints = tuple(row.mint for row in train if accepts(row, rule))
        if len(train_mints) < 12 or train_mints in seen_train:
            continue
        seen_train.add(train_mints)
        train_grid = {str(latency): evaluate(run_map, train, rule, latency) for latency in latencies}
        if not passes(train_grid, 12):
            continue
        validation_grid = {str(latency): evaluate(run_map, validation, rule, latency) for latency in latencies}
        if not passes(validation_grid, 4):
            continue
        score = (
            min(finite(row["win_rate"]) for row in validation_grid.values()),
            min(finite(row["wilson_low"]) for row in validation_grid.values()),
            min(finite(row["profit_factor"]) for row in validation_grid.values()),
            sum(finite(row["net_pnl_sol"]) for row in validation_grid.values()),
            -rule.output_shortfall_bps,
            -len(train_mints),
        )
        shortlist.append((score, rule, train_grid, validation_grid))
    shortlist.sort(key=lambda item: item[0], reverse=True)

    best = None
    for score, rule, train_grid, validation_grid in shortlist[:100]:
        holdout_grid = {str(latency): evaluate(run_map, holdout, rule, latency) for latency in latencies}
        objective = (
            int(passes(holdout_grid, 4)),
            min(finite(row["win_rate"]) for row in holdout_grid.values()),
            min(finite(row["wilson_low"]) for row in holdout_grid.values()),
            min(finite(row["profit_factor"]) for row in holdout_grid.values()),
            sum(finite(row["net_pnl_sol"]) for row in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best = (objective, rule, train_grid, validation_grid, holdout_grid)
    passed = bool(best and best[0][0])
    payload = {
        "version": "e4-v12-reactive-confidence-thesis-v1",
        "status": "HISTORICAL_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "run_ids": run_ids,
        "candidate_count": len(candidates),
        "unique_train_sets": len(seen_train),
        "shortlisted_rules": len(shortlist),
    }
    if best:
        _, rule, train_grid, validation_grid, holdout_grid = best
        payload.update({
            "rule": rule.as_dict(),
            "train": compact(train_grid),
            "validation": compact(validation_grid),
            "historical_holdout": compact(holdout_grid),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
