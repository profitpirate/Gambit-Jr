#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts import e4_v12_true_latency_replay_v2  # noqa: F401
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_reactive_profit_model as source_model
from scripts import e4_v12_true_latency_replay as economics


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


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

    def as_dict(self, atom_map: Mapping[str, Atom]) -> dict[str, Any]:
        return {
            "atoms": [
                {
                    "name": name,
                    "feature": atom_map[name].feature,
                    "description": atom_map[name].description,
                }
                for name in self.atom_names
            ],
            "guard_bps": self.guard_bps,
        }


def threshold_atoms() -> list[Atom]:
    atoms: list[Atom] = []

    def maximum(feature: str, values: Sequence[float]) -> None:
        for value in values:
            atoms.append(
                Atom(
                    name=f"{feature}_le_{value:g}",
                    feature=feature,
                    description=f"{feature} <= {value:g}",
                    predicate=lambda row, f=feature, v=value: finite(row.get(f)) <= v,
                )
            )

    def minimum(feature: str, values: Sequence[float]) -> None:
        for value in values:
            atoms.append(
                Atom(
                    name=f"{feature}_ge_{value:g}",
                    feature=feature,
                    description=f"{feature} >= {value:g}",
                    predicate=lambda row, f=feature, v=value: finite(row.get(f)) >= v,
                )
            )

    maximum("source_sol", (1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0))
    maximum("source_price_impact_bps", (300, 500, 700, 900, 1_100, 1_300, 1_500, 2_000, 3_000, 5_000))
    maximum("entry_age_ms", (25, 50, 100, 150, 250, 400, 750, 1_500))
    maximum("entry_fdv_usd", (4_000, 5_000, 6_000, 7_500, 8_500, 10_000, 12_000))
    minimum("entry_fdv_usd", (2_500, 3_000, 3_500, 4_000))
    maximum("pre_buy_count", (0, 1, 2, 3, 4, 5, 8))
    minimum("pre_unique_buyers", (1, 2, 3, 4))
    maximum("pre_unique_buyers", (0, 1, 2, 3, 5))
    minimum("pre_same_slot_buys", (1, 2, 3, 4))
    minimum("creator_seed_sol", (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0))
    maximum("creator_seed_sol", (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0))
    minimum("outside_sol", (0.25, 0.5, 1.0, 2.0, 3.0))
    maximum("outside_sol", (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0))
    minimum("pre_seed_share", (0.20, 0.35, 0.50, 0.65, 0.80))
    minimum("prior_creator_attempts", (1, 2, 3, 5))
    minimum("prior_creator_wins", (1, 2, 3))
    maximum("prior_creator_losses", (0, 1, 2))
    minimum("prior_creator_win_rate", (0.50, 0.65, 0.75, 0.85, 1.0))
    minimum("known_buyer_count", (1, 2, 3))
    minimum("max_prior_buyer_attempts", (1, 2, 3, 5))
    minimum("sum_prior_buyer_attempts", (1, 2, 3, 5, 8))
    minimum("sum_prior_buyer_wins", (1, 2, 3))
    minimum("max_creator_buyer_pair", (1, 2, 3))
    minimum("identity_strength", (0.5, 1.0, 2.0, 3.0, 4.0, 6.0))
    minimum("pre_create_signature_buys", (1, 2, 3))
    minimum("pre_max_buys_one_signature", (2, 3, 4))
    maximum("first_buyer_age_ms", (50, 100, 200, 400, 800))
    maximum("interbuyer_ms", (25, 50, 100, 250, 500))
    return atoms


def selected(rows: Sequence[Mapping[str, Any]], rule: Rule, atom_map: Mapping[str, Atom]) -> list[dict[str, Any]]:
    predicates = [atom_map[name].predicate for name in rule.atom_names]
    return [dict(row) for row in rows if all(predicate(row) for predicate in predicates)]


def precompute(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    latencies: Sequence[float],
    guards: Sequence[int],
    starting_balance_sol: float,
) -> dict[tuple[str, int, float], Mapping[str, Any] | None]:
    run_by_index = {run.run_index: run for run in runs}
    output: dict[tuple[str, int, float], Mapping[str, Any] | None] = {}
    for row in rows:
        mint = str(row.get("mint") or "")
        run = run_by_index[integer(row.get("run_index"))]
        prediction = economics.Prediction(
            mint=mint,
            decision_ns=integer(row.get("decision_ns")),
            requested_fraction=finite(row.get("requested_fraction"), 0.0185),
            score=finite(row.get("score"), 0.99),
            mode=str(row.get("mode") or "v12_reactive_profit_guard"),
            metadata=dict(row),
        )
        for guard in guards:
            for latency in latencies:
                position, _ = economics.simulate_position(
                    prediction,
                    run.grouped.get(mint, ()),
                    economics.same_window_e4_positions(run.batch).get(mint),
                    liquid_sol=starting_balance_sol,
                    latency_ms=latency,
                    entry_fraction_default=0.0185,
                    reserve_sol=0.03,
                    fee_bps=125,
                    max_output_shortfall_bps=guard,
                    confirmation_ms=1500.0,
                )
                output[(mint, guard, latency)] = position.as_dict() if position is not None else None
    return output


def quick_metrics(
    rows: Sequence[Mapping[str, Any]],
    rule: Rule,
    atom_map: Mapping[str, Atom],
    cache: Mapping[tuple[str, int, float], Mapping[str, Any] | None],
    latencies: Sequence[float],
) -> dict[str, Any]:
    accepted_rows = selected(rows, rule, atom_map)
    output: dict[str, Any] = {}
    for latency in latencies:
        positions = [
            cache.get((str(row.get("mint") or ""), rule.guard_bps, latency))
            for row in accepted_rows
        ]
        positions = [row for row in positions if row is not None]
        output[str(int(latency) if float(latency).is_integer() else latency)] = golden.positions_metrics(positions)
    return output


def block_passes(block: Mapping[str, Any], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return golden.passes_economics(block, minimum_trades, minimum_wr, minimum_pf)


def all_pass(blocks: Mapping[str, Mapping[str, Any]], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return bool(blocks) and all(block_passes(block, minimum_trades, minimum_wr, minimum_pf) for block in blocks.values())


def objective(blocks: Mapping[str, Mapping[str, Any]], complexity: int) -> tuple[float, ...]:
    return (
        min(finite(block.get("win_rate")) for block in blocks.values()),
        min(finite(block.get("wilson_low")) for block in blocks.values()),
        min(finite(block.get("profit_factor")) for block in blocks.values()),
        sum(finite(block.get("net_pnl_sol")) for block in blocks.values()),
        min(integer(block.get("trades")) for block in blocks.values()),
        -complexity,
    )


def exact_metrics(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    rule: Rule,
    atom_map: Mapping[str, Atom],
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> dict[str, Any]:
    predictions = selected(rows, rule, atom_map)
    return golden.aggregate_economics(
        runs,
        predictions,
        latencies,
        starting_balance_sol=starting_balance_sol,
        max_output_shortfall_bps=rule.guard_bps,
    )


def search(args: argparse.Namespace) -> int:
    pairs = [golden.parse_pair(value) for value in args.pair]
    runs = golden.load_runs(pairs)
    rows = source_model.build_rows(runs)
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    train = [row for row in rows if integer(row.get("run_index")) < validation_start]
    validation = [row for row in rows if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout = [row for row in rows if integer(row.get("run_index")) >= holdout_start]
    latencies = economics.parse_latencies(args.latencies)
    guards = (300, 500, 800, 1_000, 1_250, 1_500, 2_000, 2_500)
    cache = precompute(runs, rows, latencies, guards, args.starting_balance_sol)
    atoms = threshold_atoms()
    atom_map = {atom.name: atom for atom in atoms}

    train_survivors: list[tuple[tuple[float, ...], Rule, dict[str, Any]]] = []
    for guard in guards:
        for atom in atoms:
            rule = Rule((atom.name,), guard)
            blocks = quick_metrics(train, rule, atom_map, cache, latencies)
            if all_pass(blocks, 10, args.minimum_win_rate, args.minimum_profit_factor):
                train_survivors.append((objective(blocks, 1), rule, blocks))
    train_survivors.sort(key=lambda item: item[0], reverse=True)
    retained_atoms = []
    seen_names: set[str] = set()
    for _, rule, _ in train_survivors:
        name = rule.atom_names[0]
        if name not in seen_names:
            retained_atoms.append(atom_map[name])
            seen_names.add(name)
        if len(retained_atoms) >= 60:
            break

    candidates: list[tuple[tuple[float, ...], Rule, dict[str, Any], dict[str, Any]]] = []
    rule_pool: list[Rule] = [item[1] for item in train_survivors[:240]]
    for guard in guards:
        for left, right in itertools.combinations(retained_atoms, 2):
            if left.feature == right.feature:
                continue
            rule_pool.append(Rule(tuple(sorted((left.name, right.name))), guard))
    unique_pool: dict[tuple[tuple[str, ...], int], Rule] = {
        (rule.atom_names, rule.guard_bps): rule for rule in rule_pool
    }

    for rule in unique_pool.values():
        train_blocks = quick_metrics(train, rule, atom_map, cache, latencies)
        if not all_pass(train_blocks, 10, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        validation_blocks = quick_metrics(validation, rule, atom_map, cache, latencies)
        if not all_pass(validation_blocks, 4, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        score = objective(validation_blocks, len(rule.atom_names))
        candidates.append((score, rule, train_blocks, validation_blocks))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        report = {
            "version": "e4-v12-reactive-lattice-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "no stable one/two-clause causal rule passed true 0-10ms validation",
            "coverage": {"rows": len(rows), "train": len(train), "validation": len(validation), "holdout": len(holdout)},
            "train_survivor_count": len(train_survivors),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    best: tuple[Any, ...] | None = None
    for score, rule, train_blocks, validation_blocks in candidates[:120]:
        exact_train = exact_metrics(train_runs, train, rule, atom_map, latencies, args.starting_balance_sol)
        exact_validation = exact_metrics(validation_runs, validation, rule, atom_map, latencies, args.starting_balance_sol)
        if not all_pass(exact_train, 10, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        if not all_pass(exact_validation, 4, args.minimum_win_rate, args.minimum_profit_factor):
            continue
        exact_score = objective(exact_validation, len(rule.atom_names))
        item = (exact_score, rule, exact_train, exact_validation)
        if best is None or exact_score > best[0]:
            best = item

    if best is None:
        report = {
            "version": "e4-v12-reactive-lattice-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "quick candidates failed exact sequential-bankroll validation",
            "candidate_count": len(candidates),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    _, rule, exact_train, exact_validation = best
    exact_holdout = exact_metrics(holdout_runs, holdout, rule, atom_map, latencies, args.starting_balance_sol)
    predictions = selected(holdout, rule, atom_map)
    passed = len(predictions) >= 4 and all_pass(exact_holdout, 4, args.minimum_win_rate, args.minimum_profit_factor)
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-reactive-lattice-v1",
        "status": status,
        "thesis": (
            "After authenticated E4 source intent, enter only the deterministic causal source/launch "
            "regime selected by the frozen lattice and only when the scaled E4 token-output quote "
            "survives a tight BuyExactSolIn guard."
        ),
        "rule": rule.as_dict(atom_map),
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "train": exact_train,
        "validation": exact_validation,
        "holdout": exact_holdout,
        "holdout_predictions": len(predictions),
        "candidate_count": len(candidates),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(
        json.dumps(
            {
                "version": "e4-v12-reactive-lattice-model-v1",
                "status": status,
                "rule": rule.as_dict(atom_map),
                "history_run_ids": [run.run_id for run in runs],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    args.predictions_output.write_text(json.dumps({"predictions": predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": status, "rule": rule.as_dict(atom_map), "holdout_predictions": len(predictions)}, indent=2, sort_keys=True))
    return 0 if passed else 4


def rule_from_payload(value: Mapping[str, Any]) -> tuple[Rule, dict[str, Atom]]:
    atoms = threshold_atoms()
    atom_map = {atom.name: atom for atom in atoms}
    names = tuple(str(row["name"]) for row in value.get("atoms") or [])
    return Rule(names, integer(value.get("guard_bps"), 800)), atom_map


def apply(args: argparse.Namespace) -> int:
    payload = json.loads(args.model_input.read_text(encoding="utf-8"))
    rule, atom_map = rule_from_payload(payload["rule"])
    runs = golden.load_runs([golden.parse_pair(value) for value in args.pair])
    rows = source_model.build_rows(runs)
    live_index = len(runs) - 1
    predictions = selected([row for row in rows if integer(row.get("run_index")) == live_index], rule, atom_map)
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(json.dumps({"version": "e4-v12-reactive-lattice-live-v1", "live_run_id": runs[-1].run_id, "rule": payload["rule"], "predictions": predictions}, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(predictions)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search a stable deterministic sub-10ms reactive V12 rule")
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
        return apply(args)
    return search(args)


if __name__ == "__main__":
    raise SystemExit(main())
