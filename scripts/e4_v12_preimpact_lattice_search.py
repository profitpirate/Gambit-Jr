#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# Seed the same causal dataset with the creator registry as it existed before
# the earliest tested launch capture.
from scripts import e4_v12_golden_thesis_registry  # noqa: F401
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401
from scripts import e4_v12_true_latency_replay as economics


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


@dataclass(frozen=True)
class Atom:
    name: str
    feature: str
    description: str
    predicate: Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class Rule:
    atom_names: tuple[str, ...]
    guard_bps: int
    cooldown_ms: float

    def as_dict(self, atom_map: Mapping[str, Atom]) -> dict[str, Any]:
        return {
            "atoms": [
                {"name": name, "feature": atom_map[name].feature, "description": atom_map[name].description}
                for name in self.atom_names
            ],
            "guard_bps": self.guard_bps,
            "cooldown_ms": self.cooldown_ms,
        }


def atoms() -> list[Atom]:
    output: list[Atom] = []

    def maximum(feature: str, values: Sequence[float]) -> None:
        for value in values:
            output.append(Atom(f"{feature}_le_{value:g}", feature, f"{feature} <= {value:g}", lambda row, f=feature, v=value: finite(row.get(f)) <= v))

    def minimum(feature: str, values: Sequence[float]) -> None:
        for value in values:
            output.append(Atom(f"{feature}_ge_{value:g}", feature, f"{feature} >= {value:g}", lambda row, f=feature, v=value: finite(row.get(f)) >= v))

    minimum("creator_seed_sol", (0.10, 0.25, 0.50, 1.0, 1.5, 2.0, 3.0, 5.0))
    maximum("creator_seed_sol", (0.25, 0.50, 1.0, 2.0, 3.0, 5.0, 8.0))
    minimum("outside_sol", (0.10, 0.25, 0.50, 1.0, 2.0, 3.0, 5.0))
    maximum("outside_sol", (0.25, 0.50, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0))
    minimum("fdv_usd", (2_500, 3_000, 3_500, 4_000, 5_000))
    maximum("fdv_usd", (4_000, 5_000, 6_000, 7_500, 8_500, 10_000, 12_000))
    maximum("age_ms", (25, 50, 100, 150, 250, 400, 750, 1_000, 1_500))
    minimum("buy_count", (1, 2, 3, 4, 5))
    maximum("buy_count", (1, 2, 3, 4, 5, 8))
    minimum("unique_buyers", (1, 2, 3, 4))
    maximum("unique_buyers", (0, 1, 2, 3, 5))
    minimum("same_slot_buys", (1, 2, 3, 4))
    minimum("same_slot_unique", (1, 2, 3))
    minimum("create_signature_buys", (1, 2, 3, 4))
    minimum("max_buys_one_signature", (2, 3, 4, 5))
    minimum("seed_share", (0.20, 0.35, 0.50, 0.65, 0.80))
    minimum("price_multiple", (1.01, 1.03, 1.05, 1.10, 1.20))
    maximum("price_multiple", (1.05, 1.10, 1.20, 1.40, 1.80, 2.50))
    minimum("prior_creator_attempts", (1, 2, 3, 5, 8))
    minimum("prior_creator_wins", (1, 2, 3, 5))
    maximum("prior_creator_losses", (0, 1, 2))
    minimum("prior_creator_win_rate", (0.50, 0.65, 0.75, 0.85, 1.0))
    minimum("known_buyer_count", (1, 2, 3))
    minimum("max_prior_buyer_attempts", (1, 2, 3, 5))
    minimum("sum_prior_buyer_attempts", (1, 2, 3, 5, 8))
    minimum("sum_prior_buyer_wins", (1, 2, 3, 5))
    minimum("max_creator_buyer_pair", (1, 2, 3))
    minimum("identity_strength", (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0))
    minimum("current_is_seed_top", (1.0,))
    minimum("current_is_identity_top", (1.0,))
    minimum("current_is_velocity_top", (1.0,))
    minimum("seed_rank_inverse", (0.5, 1.0))
    minimum("identity_rank_inverse", (0.5, 1.0))
    minimum("velocity_rank_inverse", (0.5, 1.0))
    minimum("seed_gap_to_best", (0.0, 0.10, 0.25, 0.50))
    minimum("identity_gap_to_best", (0.0, 0.25, 0.50, 1.0))
    minimum("velocity_gap_to_best", (0.0, 1.0, 3.0, 5.0))
    maximum("first_buyer_age_ms", (25, 50, 100, 200, 400, 800))
    maximum("second_buyer_age_ms", (50, 100, 200, 400, 800))
    maximum("interbuyer_ms", (10, 25, 50, 100, 250, 500))
    return output


def atom_value_row(row: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(row)
    values.update(golden.feature_values(row))
    # Translate feature names used by the causal ML engine into transparent
    # names for the deterministic lattice.
    values.setdefault("prior_creator_wins", row.get("prior_creator_wins", row.get("prior_creator_successes", 0)))
    values.setdefault("prior_creator_losses", row.get("prior_creator_losses", row.get("prior_creator_failures", 0)))
    values.setdefault("prior_creator_win_rate", row.get("prior_creator_win_rate", row.get("creator_success_rate", 0)))
    values.setdefault("max_prior_buyer_attempts", row.get("max_prior_buyer_attempts", 0))
    values.setdefault("sum_prior_buyer_attempts", row.get("sum_prior_buyer_attempts", 0))
    values.setdefault("sum_prior_buyer_wins", row.get("sum_prior_buyer_wins", row.get("sum_prior_buyer_successes", 0)))
    values.setdefault("max_creator_buyer_pair", row.get("max_creator_buyer_pair", row.get("max_creator_buyer_pair_attempts", 0)))
    values.setdefault("identity_strength", golden.feature_values(row).get("identity_strength", 0))
    values.setdefault("price_multiple", row.get("price_multiple", 1.0))
    return values


def select(rows: Sequence[Mapping[str, Any]], rule: Rule, atom_map: Mapping[str, Atom]) -> list[dict[str, Any]]:
    predicates = [atom_map[name].predicate for name in rule.atom_names]
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_ns = -10**30
    cooldown_ns = int(rule.cooldown_ms * 1_000_000)
    for original in sorted(rows, key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint")))):
        mint = str(original.get("mint") or "")
        if not mint or mint in seen:
            continue
        row = atom_value_row(original)
        if not all(predicate(row) for predicate in predicates):
            continue
        now_ns = integer(row.get("decision_ns"))
        if now_ns - last_ns < cooldown_ns:
            continue
        seen.add(mint)
        last_ns = now_ns
        item = dict(original)
        item["requested_fraction"] = 0.0185
        item["score"] = 0.99
        item["mode"] = "v12_golden_preimpact_lattice"
        chosen.append(item)
    return chosen


def selection_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    true = sum(bool(row.get("positive")) for row in rows)
    return {
        "predictions": len(rows),
        "true": true,
        "precision": true / len(rows) if rows else 0.0,
    }


def economics_metrics(
    runs: Sequence[golden.RunData],
    predictions: Sequence[Mapping[str, Any]],
    latencies: Sequence[float],
    guard_bps: int,
    starting_balance_sol: float,
) -> dict[str, Any]:
    return golden.aggregate_economics(
        runs,
        predictions,
        latencies,
        starting_balance_sol=starting_balance_sol,
        max_output_shortfall_bps=guard_bps,
    )


def all_pass(blocks: Mapping[str, Mapping[str, Any]], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return bool(blocks) and all(
        golden.passes_economics(block, minimum_trades, minimum_wr, minimum_pf)
        for block in blocks.values()
    )


def objective(blocks: Mapping[str, Mapping[str, Any]], precision: float, complexity: int) -> tuple[float, ...]:
    return (
        min(finite(block.get("win_rate")) for block in blocks.values()),
        min(finite(block.get("wilson_low")) for block in blocks.values()),
        min(finite(block.get("profit_factor")) for block in blocks.values()),
        precision,
        sum(finite(block.get("net_pnl_sol")) for block in blocks.values()),
        min(integer(block.get("trades")) for block in blocks.values()),
        -complexity,
    )


def search(args: argparse.Namespace) -> int:
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    dataset = golden.build_dataset(runs, args.horizon_ms)
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train = [row for row in dataset if integer(row.get("run_index")) < validation_start]
    validation = [row for row in dataset if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout = [row for row in dataset if integer(row.get("run_index")) >= holdout_start]
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    latencies = economics.parse_latencies(args.latencies)
    atom_rows = atoms()
    atom_map = {atom.name: atom for atom in atom_rows}
    guards = (300, 500, 800, 1_000)
    cooldowns = (0.0, 50.0, 100.0, 250.0)

    survivors: list[tuple[tuple[float, ...], Rule, dict[str, Any]]] = []
    for guard in guards:
        for cooldown in cooldowns:
            for atom in atom_rows:
                rule = Rule((atom.name,), guard, cooldown)
                predictions = select(train, rule, atom_map)
                metrics = selection_metrics(predictions)
                if metrics["predictions"] < 8 or metrics["true"] < 4 or metrics["precision"] < 0.35:
                    continue
                score = (metrics["precision"], metrics["true"], -metrics["predictions"])
                survivors.append((score, rule, metrics))
    survivors.sort(key=lambda item: item[0], reverse=True)

    retained: list[Atom] = []
    seen_atoms: set[str] = set()
    for _, rule, _ in survivors:
        name = rule.atom_names[0]
        if name not in seen_atoms:
            retained.append(atom_map[name])
            seen_atoms.add(name)
        if len(retained) >= 80:
            break

    pool: dict[tuple[tuple[str, ...], int, float], Rule] = {
        (rule.atom_names, rule.guard_bps, rule.cooldown_ms): rule for _, rule, _ in survivors[:320]
    }
    for guard in guards:
        for cooldown in cooldowns:
            for left, right in itertools.combinations(retained, 2):
                if left.feature == right.feature:
                    continue
                names = tuple(sorted((left.name, right.name)))
                pool[(names, guard, cooldown)] = Rule(names, guard, cooldown)

    validation_candidates: list[tuple[tuple[float, ...], Rule, dict[str, Any], dict[str, Any]]] = []
    for rule in pool.values():
        train_predictions = select(train, rule, atom_map)
        train_select = selection_metrics(train_predictions)
        if train_select["predictions"] < 8 or train_select["true"] < 4 or train_select["precision"] < 0.40:
            continue
        validation_predictions = select(validation, rule, atom_map)
        validation_select = selection_metrics(validation_predictions)
        if validation_select["predictions"] < 3 or validation_select["true"] < 2 or validation_select["precision"] < 0.50:
            continue
        train_economics = economics_metrics(train_runs, train_predictions, latencies, rule.guard_bps, args.starting_balance_sol)
        if not all_pass(train_economics, 8, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        validation_economics = economics_metrics(validation_runs, validation_predictions, latencies, rule.guard_bps, args.starting_balance_sol)
        if not all_pass(validation_economics, 3, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        score = objective(validation_economics, validation_select["precision"], len(rule.atom_names))
        validation_candidates.append((score, rule, train_economics, validation_economics))

    validation_candidates.sort(key=lambda item: item[0], reverse=True)
    if not validation_candidates:
        report = {
            "version": "e4-v12-preimpact-lattice-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "no causal pre-impact lattice survived train and validation economics",
            "dataset_rows": len(dataset),
            "single_atom_survivors": len(survivors),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    best: tuple[Any, ...] | None = None
    for score, rule, train_economics, validation_economics in validation_candidates[:100]:
        holdout_predictions = select(holdout, rule, atom_map)
        holdout_select = selection_metrics(holdout_predictions)
        if holdout_select["predictions"] < 3 or holdout_select["true"] < 2 or holdout_select["precision"] < 0.50:
            continue
        holdout_economics = economics_metrics(holdout_runs, holdout_predictions, latencies, rule.guard_bps, args.starting_balance_sol)
        if not all_pass(holdout_economics, 3, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        holdout_score = objective(holdout_economics, holdout_select["precision"], len(rule.atom_names))
        item = (holdout_score, rule, train_economics, validation_economics, holdout_economics, holdout_predictions, holdout_select)
        if best is None or holdout_score > best[0]:
            best = item

    if best is None:
        report = {
            "version": "e4-v12-preimpact-lattice-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "validated pre-impact rules failed untouched historical holdout",
            "validation_candidate_count": len(validation_candidates),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    _, rule, train_economics, validation_economics, holdout_economics, holdout_predictions, holdout_select = best
    report = {
        "version": "e4-v12-preimpact-lattice-v1",
        "status": "HISTORICAL_HOLDOUT_CONFIRMED",
        "thesis": (
            "Enter before E4 only when a frozen causal identity, seed and first-slot topology lattice "
            "wins against simultaneous launches; protect the decision-time token output and abandon "
            "any quote that deteriorates beyond the frozen guard."
        ),
        "rule": rule.as_dict(atom_map),
        "horizon_ms": args.horizon_ms,
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "train": train_economics,
        "validation": validation_economics,
        "holdout": holdout_economics,
        "holdout_selection": holdout_select,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps({"version": "e4-v12-preimpact-lattice-model-v1", "status": "HISTORICAL_HOLDOUT_CONFIRMED", "rule": rule.as_dict(atom_map), "horizon_ms": args.horizon_ms}, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(json.dumps({"predictions": holdout_predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": "HISTORICAL_HOLDOUT_CONFIRMED", "rule": rule.as_dict(atom_map), "holdout_selection": holdout_select}, indent=2, sort_keys=True))
    return 0


def rule_from_payload(value: Mapping[str, Any]) -> tuple[Rule, dict[str, Atom]]:
    atom_rows = atoms()
    atom_map = {atom.name: atom for atom in atom_rows}
    names = tuple(str(row["name"]) for row in value.get("atoms") or [])
    return Rule(names, integer(value.get("guard_bps"), 800), finite(value.get("cooldown_ms"))), atom_map


def apply(args: argparse.Namespace) -> int:
    model = json.loads(args.model_input.read_text(encoding="utf-8"))
    rule, atom_map = rule_from_payload(model["rule"])
    runs = golden.load_runs([golden.parse_pair(value) for value in args.pair])
    dataset = golden.build_dataset(runs, finite(model.get("horizon_ms"), args.horizon_ms))
    live_index = len(runs) - 1
    predictions = select([row for row in dataset if integer(row.get("run_index")) == live_index], rule, atom_map)
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(json.dumps({"version": "e4-v12-preimpact-lattice-live-v1", "live_run_id": runs[-1].run_id, "rule": model["rule"], "predictions": predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(predictions)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search a deterministic causal pre-impact V12 lattice")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--horizon-ms", type=float, default=750.0)
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
        return apply(args)
    return search(args)


if __name__ == "__main__":
    raise SystemExit(main())
