#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay as economics

FEATURES = (
    "source_sol",
    "entry_fdv_usd",
    "source_price_impact_bps",
    "source_curve_share",
    "source_tokens",
    "prior_creator_attempts",
    "prior_creator_win_rate",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    rows = sorted(float(value) for value in values)
    position = min(len(rows) - 1, max(0, round((len(rows) - 1) * fraction)))
    return rows[position]


@dataclass(frozen=True)
class Clause:
    feature: str
    minimum: float
    maximum: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Rule:
    clauses: tuple[Clause, ...]
    max_output_shortfall_bps: int

    def accepts(self, row: Mapping[str, Any]) -> bool:
        return all(
            clause.minimum <= finite(row.get(clause.feature)) <= clause.maximum
            for clause in self.clauses
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "clauses": [clause.as_dict() for clause in self.clauses],
            "max_output_shortfall_bps": self.max_output_shortfall_bps,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Rule":
        return cls(
            clauses=tuple(Clause(**dict(row)) for row in value.get("clauses") or []),
            max_output_shortfall_bps=integer(value.get("max_output_shortfall_bps"), 800),
        )


def source_row(
    run: golden.RunData,
    launch: golden.Launch,
    creator_attempts: Counter[str],
    creator_wins: Counter[str],
    creator_losses: Counter[str],
) -> dict[str, Any] | None:
    buy = launch.e4_buy
    if buy is None:
        return None
    source_sol = max(0.0, finite(buy.get("sol_amount")))
    source_tokens = max(0.0, finite(buy.get("token_amount")))
    states = economics.reserve_states(run.grouped.get(launch.mint, ()))
    buy_ns = integer(buy.get("received_ns"))
    post = economics.state_at_or_before(states, buy_ns) or economics.state_at_or_after(states, buy_ns)
    if post is None or source_sol <= 0 or source_tokens <= 0:
        return None

    average_price = source_sol / source_tokens
    post_marginal_price = post.virtual_sol / max(post.virtual_tokens, 1e-12)
    impact_bps = (post_marginal_price / max(average_price, 1e-18) - 1.0) * 10_000.0
    pre_virtual_sol = max(1e-12, post.virtual_sol - source_sol)
    curve_share = source_sol / pre_virtual_sol
    settled = creator_wins[launch.creator] + creator_losses[launch.creator]
    return {
        "mint": launch.mint,
        "run_id": run.run_id,
        "run_index": run.run_index,
        "decision_ns": buy_ns,
        "requested_fraction": 0.0185,
        "score": 0.99,
        "mode": "v12_reactive_tight_output",
        "source_sol": source_sol,
        "source_tokens": source_tokens,
        "entry_fdv_usd": max(0.0, finite(buy.get("fdv_usd"), post.fdv_usd)),
        "source_price_impact_bps": impact_bps,
        "source_curve_share": curve_share,
        "prior_creator_attempts": creator_attempts[launch.creator],
        "prior_creator_win_rate": creator_wins[launch.creator] / settled if settled else 0.0,
        "e4_won": bool(launch.e4_position and finite(launch.e4_position.get("pnl_sol")) > 0),
        "e4_pnl_sol": finite(launch.e4_position.get("pnl_sol")) if launch.e4_position else None,
    }


def build_rows(runs: Sequence[golden.RunData]) -> list[dict[str, Any]]:
    creator_attempts: Counter[str] = Counter()
    creator_wins: Counter[str] = Counter()
    creator_losses: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for run in runs:
        current: list[dict[str, Any]] = []
        for launch in run.launches.values():
            row = source_row(run, launch, creator_attempts, creator_wins, creator_losses)
            if row is not None:
                current.append(row)
        current.sort(key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint"))))
        rows.extend(current)
        for row in current:
            launch = run.launches[str(row["mint"])]
            if launch.creator:
                creator_attempts[launch.creator] += 1
                if row["e4_won"]:
                    creator_wins[launch.creator] += 1
                else:
                    creator_losses[launch.creator] += 1
        print(json.dumps({"run_id": run.run_id, "reactive_rows": len(current)}), flush=True)
    return rows


def quantile_bands(rows: Sequence[Mapping[str, Any]], feature: str) -> list[float]:
    values = [finite(row.get(feature)) for row in rows]
    fractions = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.0)
    return sorted(set(percentile(values, fraction) for fraction in fractions))


def clauses(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Clause]]:
    result: dict[str, list[Clause]] = {}
    for feature in FEATURES:
        values = quantile_bands(rows, feature)
        options: list[Clause] = []
        for left in range(len(values)):
            for right in range(left, len(values)):
                if left == 0 and right == len(values) - 1:
                    continue
                options.append(Clause(feature, values[left], values[right]))
        result[feature] = options
    return result


def candidate_rules(rows: Sequence[Mapping[str, Any]]) -> Iterable[Rule]:
    options = clauses(rows)
    guards = (200, 400, 600, 800, 1_000)
    for guard in guards:
        yield Rule((), guard)
        for feature in FEATURES:
            for clause in options[feature]:
                yield Rule((clause,), guard)
        compact = {
            feature: sorted(
                options[feature],
                key=lambda clause: clause.maximum - clause.minimum,
            )[:18]
            for feature in FEATURES
        }
        for left_index, left_feature in enumerate(FEATURES):
            for right_feature in FEATURES[left_index + 1 :]:
                for left_clause in compact[left_feature]:
                    for right_clause in compact[right_feature]:
                        yield Rule((left_clause, right_clause), guard)


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if rule.accepts(row)]


def passes(block: Mapping[str, Any], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return golden.passes_economics(block, minimum_trades, minimum_wr, minimum_pf)


def evaluate(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    rule: Rule,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    predictions = select(rows, rule)
    metrics = golden.aggregate_economics(
        runs,
        predictions,
        latencies,
        starting_balance_sol=starting_balance_sol,
        max_output_shortfall_bps=rule.max_output_shortfall_bps,
    )
    return predictions, metrics


def search_mode(args: argparse.Namespace) -> int:
    pairs = [golden.parse_pair(value) for value in args.pair]
    if len(pairs) < 8:
        raise SystemExit("at least eight chronological run pairs are required")
    runs = golden.load_runs(pairs)
    rows = build_rows(runs)
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    train_rows = [row for row in rows if integer(row.get("run_index")) < validation_start]
    validation_rows = [row for row in rows if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout_rows = [row for row in rows if integer(row.get("run_index")) >= holdout_start]
    latencies = economics.parse_latencies(args.latencies)

    best: tuple[Any, ...] | None = None
    tested = 0
    for rule in candidate_rules(train_rows):
        tested += 1
        train_selected = select(train_rows, rule)
        validation_selected = select(validation_rows, rule)
        if len(train_selected) < 12 or len(validation_selected) < 5:
            continue
        train_win_rate = sum(bool(row.get("e4_won")) for row in train_selected) / len(train_selected)
        validation_win_rate = sum(bool(row.get("e4_won")) for row in validation_selected) / len(validation_selected)
        if train_win_rate < 0.55 or validation_win_rate < 0.55:
            continue
        _, train_economics = evaluate(
            train_runs,
            train_selected,
            Rule((), rule.max_output_shortfall_bps),
            latencies,
            args.starting_balance_sol,
        )
        if not all(
            passes(block, 12, args.minimum_win_rate, args.minimum_profit_factor)
            for block in train_economics.values()
        ):
            continue
        _, validation_economics = evaluate(
            validation_runs,
            validation_selected,
            Rule((), rule.max_output_shortfall_bps),
            latencies,
            args.starting_balance_sol,
        )
        if not all(
            passes(block, 5, args.minimum_win_rate, args.minimum_profit_factor)
            for block in validation_economics.values()
        ):
            continue
        worst_wr = min(finite(block.get("win_rate")) for block in validation_economics.values())
        worst_pf = min(finite(block.get("profit_factor")) for block in validation_economics.values())
        worst_low = min(finite(block.get("wilson_low")) for block in validation_economics.values())
        pnl = sum(finite(block.get("net_pnl_sol")) for block in validation_economics.values())
        objective = (worst_wr, worst_low, worst_pf, validation_win_rate, pnl, len(validation_selected), -len(rule.clauses))
        candidate = (objective, rule, train_economics, validation_economics)
        if best is None or objective > best[0]:
            best = candidate

    if best is None:
        report = {
            "version": "e4-v12-reactive-guard-search-v1",
            "status": "NOT_CONCLUSIVE",
            "tested_rules": tested,
            "reason": "no reactive E4-confirmed output guard passed chronological validation at all latencies",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    _, rule, train_economics, validation_economics = best
    holdout_predictions, holdout_economics = evaluate(
        holdout_runs,
        holdout_rows,
        rule,
        latencies,
        args.starting_balance_sol,
    )
    passed = bool(
        len(holdout_predictions) >= 4
        and all(
            passes(block, 4, args.minimum_win_rate, args.minimum_profit_factor)
            for block in holdout_economics.values()
        )
    )
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-reactive-guard-search-v1",
        "status": status,
        "thesis": (
            "Observe E4's authenticated entry transaction, then submit only when the source order's "
            "entry-time curve-impact band qualifies and a tightly protected current token quote remains executable."
        ),
        "rule": rule.as_dict(),
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "train": train_economics,
        "validation": validation_economics,
        "holdout": holdout_economics,
        "holdout_predictions": len(holdout_predictions),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(
        json.dumps({"predictions": holdout_predictions}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    args.model_output.write_text(
        json.dumps(
            {
                "version": "e4-v12-reactive-guard-model-v1",
                "status": status,
                "rule": rule.as_dict(),
                "history_run_ids": [run.run_id for run in runs],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"status": status, "rule": rule.as_dict()}, indent=2, sort_keys=True))
    return 0 if passed else 3


def apply_mode(args: argparse.Namespace) -> int:
    model = json.loads(args.model_input.read_text(encoding="utf-8"))
    rule = Rule.from_dict(model["rule"])
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    rows = build_rows(runs)
    live_index = len(runs) - 1
    predictions = select(
        [row for row in rows if integer(row.get("run_index")) == live_index],
        rule,
    )
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(
        json.dumps(
            {
                "version": "e4-v12-reactive-guard-live-predictions-v1",
                "live_run_id": runs[-1].run_id,
                "rule": rule.as_dict(),
                "predictions": predictions,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(predictions)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search a true-latency E4-confirmed tight-output thesis")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--minimum-win-rate", type=float, default=0.65)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--predictions-output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply_mode(args)
    return search_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
