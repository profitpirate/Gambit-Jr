#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from scripts import e4_v12_independent_exit_replay_v2  # noqa: F401 - scaled E4 output guard
from scripts import e4_v12_independent_exit_replay as exit_replay
from scripts import e4_v12_reactive_profit_registry  # noqa: F401 - causal whitelist seed
from scripts import e4_v12_reactive_profit_model as source_model
from scripts import e4_v12_golden_thesis_search as golden
from scripts import e4_v12_true_latency_replay as economics

FEATURES = source_model.FEATURES


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


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray(
        [[source_model.feature_values(row)[feature] for feature in FEATURES] for row in rows],
        dtype=float,
    )


def policy_key(policy: exit_replay.ExitPolicy) -> tuple[float, ...]:
    return tuple(float(value) for value in asdict(policy).values())


def policy_candidates(limit: int = 420) -> list[exit_replay.ExitPolicy]:
    handpicked = [
        exit_replay.ExitPolicy(0.10, 0.20, 0.30, 0.60, 0.15, 0.00, 5_000, 0),
        exit_replay.ExitPolicy(0.12, 0.20, 0.30, 1.00, 0.20, 0.00, 10_000, 0),
        exit_replay.ExitPolicy(0.15, 0.30, 0.30, 1.00, 0.20, 0.00, 30_000, 0),
        exit_replay.ExitPolicy(0.08, 0.15, 0.50, 0.50, 0.10, 0.00, 2_500, 0),
        exit_replay.ExitPolicy(0.10, 0.25, 0.50, 0.75, 0.15, 0.02, 5_000, 50),
        exit_replay.ExitPolicy(0.15, 0.40, 0.30, 1.50, 0.25, 0.00, 60_000, 100),
        exit_replay.ExitPolicy(0.20, 0.50, 0.30, 2.00, 0.30, -0.02, 120_000, 100),
        exit_replay.ExitPolicy(0.08, 0.10, 0.70, 0.30, 0.08, 0.00, 1_500, 0),
    ]
    rng = random.Random(712)
    stops = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25)
    tp1s = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75)
    partials = (0.20, 0.30, 0.50, 0.70)
    finals = (0.30, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00)
    trails = (0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30)
    floors = (-0.05, -0.02, 0.00, 0.02, 0.05, 0.10)
    holds = (750.0, 1_000.0, 1_500.0, 2_500.0, 5_000.0, 10_000.0, 30_000.0, 60_000.0, 120_000.0)
    minimums = (0.0, 25.0, 50.0, 100.0, 250.0)
    candidates = list(handpicked)
    while len(candidates) < limit:
        tp1 = rng.choice(tp1s)
        final = rng.choice([value for value in finals if value >= tp1 + 0.10])
        candidates.append(
            exit_replay.ExitPolicy(
                stop_loss_fraction=rng.choice(stops),
                first_take_profit_fraction=tp1,
                first_partial_fraction=rng.choice(partials),
                final_take_profit_fraction=final,
                trailing_drawdown_fraction=rng.choice(trails),
                post_partial_floor_fraction=rng.choice(floors),
                maximum_hold_ms=rng.choice(holds),
                minimum_hold_ms=rng.choice(minimums),
            )
        )
    unique = {policy_key(policy): policy for policy in candidates}
    return list(unique.values())


def to_prediction(row: Mapping[str, Any]) -> economics.Prediction:
    return economics.Prediction(
        mint=str(row.get("mint") or ""),
        decision_ns=integer(row.get("decision_ns")),
        requested_fraction=finite(row.get("requested_fraction"), 0.0185),
        score=finite(row.get("score"), 0.99),
        mode=str(row.get("mode") or "v12_reactive_independent"),
        metadata=dict(row),
    )


def simulate_one(
    run: golden.RunData,
    row: Mapping[str, Any],
    policy: exit_replay.ExitPolicy,
    guard_bps: int,
    latency: float,
    starting_balance_sol: float,
) -> Mapping[str, Any] | None:
    prediction = to_prediction(row)
    position, _ = exit_replay.simulate_independent(
        prediction,
        run.grouped.get(prediction.mint, ()),
        liquid_sol=starting_balance_sol,
        latency_ms=latency,
        reserve_sol=0.03,
        fee_bps=125,
        max_output_shortfall_bps=guard_bps,
        policy=policy,
    )
    return position.as_dict() if position is not None else None


def screening_metrics(
    runs_by_index: Mapping[int, golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    policy: exit_replay.ExitPolicy,
    guard_bps: int,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for latency in latencies:
        positions = [
            simulate_one(
                runs_by_index[integer(row.get("run_index"))],
                row,
                policy,
                guard_bps,
                latency,
                starting_balance_sol,
            )
            for row in rows
        ]
        positions = [position for position in positions if position is not None]
        output[str(int(latency) if float(latency).is_integer() else latency)] = golden.positions_metrics(positions)
    return output


def aggregate_exact(
    runs: Sequence[golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    policy: exit_replay.ExitPolicy,
    guard_bps: int,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> dict[str, Any]:
    by_run: defaultdict[int, list[economics.Prediction]] = defaultdict(list)
    for row in rows:
        by_run[integer(row.get("run_index"))].append(to_prediction(row))
    output: dict[str, Any] = {}
    for latency in latencies:
        positions: list[dict[str, Any]] = []
        ending_balances: list[float] = []
        rejected: Counter[str] = Counter()
        for run in runs:
            result = exit_replay.replay(
                by_run.get(run.run_index, ()),
                run.grouped,
                starting_balance_sol=starting_balance_sol,
                latency_ms=latency,
                reserve_sol=0.03,
                fee_bps=125,
                max_output_shortfall_bps=guard_bps,
                policy=policy,
                max_concurrent=2,
            )
            positions.extend(result.get("positions") or [])
            ending_balances.append(finite(result.get("ending_balance_sol"), starting_balance_sol))
            rejected.update(result.get("rejected") or {})
        block = golden.positions_metrics(positions)
        block["positions"] = positions
        block["mean_ending_balance_sol"] = statistics.fmean(ending_balances) if ending_balances else starting_balance_sol
        block["rejected"] = dict(sorted(rejected.items()))
        output[str(int(latency) if float(latency).is_integer() else latency)] = block
    return output


def all_pass(
    blocks: Mapping[str, Mapping[str, Any]],
    minimum_trades: int,
    minimum_wr: float,
    minimum_pf: float,
) -> bool:
    return bool(blocks) and all(
        golden.passes_economics(block, minimum_trades, minimum_wr, minimum_pf)
        for block in blocks.values()
    )


def robust_labels(
    runs_by_index: Mapping[int, golden.RunData],
    rows: Sequence[Mapping[str, Any]],
    policy: exit_replay.ExitPolicy,
    guard_bps: int,
    latencies: Sequence[float],
    starting_balance_sol: float,
) -> np.ndarray:
    labels = []
    for row in rows:
        run = runs_by_index[integer(row.get("run_index"))]
        outcomes = [
            simulate_one(run, row, policy, guard_bps, latency, starting_balance_sol)
            for latency in latencies
        ]
        labels.append(
            int(
                all(
                    outcome is not None and finite(outcome.get("pnl_sol")) > 0
                    for outcome in outcomes
                )
            )
        )
    return np.asarray(labels, dtype=int)


def model_specs() -> list[tuple[str, Any]]:
    return [
        (
            "logit",
            Pipeline(
                [
                    ("scale", StandardScaler()),
                    ("model", LogisticRegression(C=0.20, class_weight="balanced", max_iter=5_000, solver="liblinear", random_state=712)),
                ]
            ),
        ),
        (
            "extra4",
            ExtraTreesClassifier(n_estimators=260, max_depth=4, min_samples_leaf=3, max_features="sqrt", class_weight="balanced_subsample", random_state=712, n_jobs=-1),
        ),
        (
            "extra6",
            ExtraTreesClassifier(n_estimators=320, max_depth=6, min_samples_leaf=4, max_features="sqrt", class_weight="balanced_subsample", random_state=712, n_jobs=-1),
        ),
        (
            "forest5",
            RandomForestClassifier(n_estimators=300, max_depth=5, min_samples_leaf=3, max_features="sqrt", class_weight="balanced_subsample", random_state=712, n_jobs=-1),
        ),
    ]


def fit_model(template: Any, rows: Sequence[Mapping[str, Any]], labels: np.ndarray) -> Any:
    import copy

    model = copy.deepcopy(template)
    model.fit(matrix(rows), labels)
    return model


def probabilities(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return model.predict_proba(matrix(rows))[:, 1] if rows else np.asarray([], dtype=float)


def select(rows: Sequence[Mapping[str, Any]], model: Any, threshold: float, max_impact_bps: float) -> list[dict[str, Any]]:
    values = probabilities(model, rows)
    selected = []
    for row, probability in zip(rows, values):
        if float(probability) < threshold:
            continue
        if finite(row.get("source_price_impact_bps")) > max_impact_bps:
            continue
        item = dict(row)
        item["score"] = float(probability)
        item["requested_fraction"] = 0.0185
        item["mode"] = "v12_reactive_independent"
        selected.append(item)
    selected.sort(key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint"))))
    return selected


def objective(blocks: Mapping[str, Mapping[str, Any]], selected_count: int) -> tuple[float, ...]:
    return (
        min(finite(block.get("win_rate")) for block in blocks.values()),
        min(finite(block.get("wilson_low")) for block in blocks.values()),
        min(finite(block.get("profit_factor")) for block in blocks.values()),
        sum(finite(block.get("net_pnl_sol")) for block in blocks.values()),
        min(integer(block.get("trades")) for block in blocks.values()),
        -selected_count,
    )


def search(args: argparse.Namespace) -> int:
    runs = golden.load_runs([golden.parse_pair(value) for value in args.pair])
    rows = source_model.build_rows(runs)
    if len(runs) < 8:
        raise SystemExit("at least eight chronological live windows are required")
    holdout_start = len(runs) - 2
    validation_start = max(4, holdout_start - 2)
    train = [row for row in rows if integer(row.get("run_index")) < validation_start]
    validation = [row for row in rows if validation_start <= integer(row.get("run_index")) < holdout_start]
    holdout = [row for row in rows if integer(row.get("run_index")) >= holdout_start]
    train_runs = runs[:validation_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    runs_by_index = {run.run_index: run for run in runs}
    latencies = economics.parse_latencies(args.latencies)
    guards = (300, 500, 800, 1_000, 1_500, 2_000, 2_500)

    policy_survivors: list[tuple[tuple[float, ...], exit_replay.ExitPolicy, int, dict[str, Any]]] = []
    for policy in policy_candidates(args.policy_candidates):
        for guard in guards:
            blocks = screening_metrics(runs_by_index, train, policy, guard, latencies, args.starting_balance_sol)
            minimum = max(10, min(20, len(train) // 4))
            if not all_pass(blocks, minimum, 0.50, 1.05):
                continue
            score = objective(blocks, len(train))
            policy_survivors.append((score, policy, guard, blocks))
    policy_survivors.sort(key=lambda item: item[0], reverse=True)
    policy_survivors = policy_survivors[: args.retained_policies]

    best: tuple[Any, ...] | None = None
    diagnostics = []
    for policy_score, policy, guard, train_policy_blocks in policy_survivors:
        labels = robust_labels(runs_by_index, train, policy, guard, latencies, args.starting_balance_sol)
        positives = int(labels.sum())
        if positives < 6 or positives >= len(labels):
            continue
        for model_name, template in model_specs():
            model = fit_model(template, train, labels)
            validation_probabilities = probabilities(model, validation)
            thresholds = sorted(set(float(np.quantile(validation_probabilities, q)) for q in (0.50,0.60,0.70,0.78,0.84,0.88,0.92,0.95,0.97,0.98,0.99))) if len(validation_probabilities) else [1.0]
            for threshold in thresholds:
                for max_impact in (300.0, 500.0, 800.0, 1_200.0, 2_000.0, 100_000.0):
                    validation_selected = select(validation, model, threshold, max_impact)
                    if len(validation_selected) < 4:
                        continue
                    validation_blocks = aggregate_exact(validation_runs, validation_selected, policy, guard, latencies, args.starting_balance_sol)
                    if not all_pass(validation_blocks, 4, args.minimum_win_rate, args.minimum_profit_factor):
                        continue
                    score = objective(validation_blocks, len(validation_selected))
                    candidate = (score, policy, guard, model_name, model, threshold, max_impact, train_policy_blocks, validation_blocks)
                    if best is None or score > best[0]:
                        best = candidate
            diagnostics.append({"policy": policy.as_dict(), "guard_bps": guard, "model": model_name, "robust_train_wins": positives})

    if best is None:
        report = {
            "version": "e4-v12-reactive-independent-golden-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "no independent-exit policy and entry model passed all 0-10ms validation gates",
            "coverage": {"rows": len(rows), "train": len(train), "validation": len(validation), "holdout": len(holdout)},
            "policy_survivors": len(policy_survivors),
            "diagnostic_models": len(diagnostics),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    score, policy, guard, model_name, model, threshold, max_impact, train_policy_blocks, validation_blocks = best
    holdout_selected = select(holdout, model, threshold, max_impact)
    holdout_blocks = aggregate_exact(holdout_runs, holdout_selected, policy, guard, latencies, args.starting_balance_sol)
    passed = len(holdout_selected) >= 4 and all_pass(holdout_blocks, 4, args.minimum_win_rate, args.minimum_profit_factor)
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-reactive-independent-golden-v1",
        "status": status,
        "thesis": (
            "Use E4's authenticated source entry as the selection trigger, admit only the frozen "
            "profitable source/identity regime under a strict token-output guard, and manage the "
            "position with a frozen independent partial-stop-trailing exit rather than copying a "
            "source exit from a different entry price."
        ),
        "entry_model": {"name": model_name, "threshold": threshold, "maximum_source_impact_bps": max_impact},
        "exit_policy": policy.as_dict(),
        "guard_bps": guard,
        "features": FEATURES,
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "train_runs": [run.run_id for run in train_runs],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "validation": validation_blocks,
        "holdout": holdout_blocks,
        "holdout_predictions": len(holdout_selected),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(json.dumps({"predictions": holdout_selected}, indent=2, sort_keys=True), encoding="utf-8")
    joblib.dump(
        {
            "version": "e4-v12-reactive-independent-model-v1",
            "status": status,
            "model": model,
            "model_name": model_name,
            "threshold": threshold,
            "maximum_source_impact_bps": max_impact,
            "exit_policy": policy.as_dict(),
            "guard_bps": guard,
            "features": FEATURES,
            "history_run_ids": [run.run_id for run in runs],
        },
        args.model_output,
    )
    print(json.dumps({"status": status, "holdout_predictions": len(holdout_selected), "entry_model": report["entry_model"], "exit_policy": report["exit_policy"], "guard_bps": guard}, indent=2, sort_keys=True))
    return 0 if passed else 3


def apply(args: argparse.Namespace) -> int:
    bundle = joblib.load(args.model_input)
    runs = golden.load_runs([golden.parse_pair(value) for value in args.pair])
    rows = source_model.build_rows(runs)
    live_index = len(runs) - 1
    live = [row for row in rows if integer(row.get("run_index")) == live_index]
    selected = select(live, bundle["model"], finite(bundle["threshold"]), finite(bundle["maximum_source_impact_bps"], 100_000.0))
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(json.dumps({"version": "e4-v12-reactive-independent-live-v1", "live_run_id": runs[-1].run_id, "predictions": selected}, indent=2, sort_keys=True), encoding="utf-8")
    args.policy_output.write_text(json.dumps(bundle["exit_policy"], indent=2, sort_keys=True), encoding="utf-8")
    args.guard_output.write_text(str(integer(bundle["guard_bps"], 800)) + "\n", encoding="utf-8")
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(selected)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Find a sub-10ms reactive entry with independent exit management")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--minimum-win-rate", type=float, default=0.65)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.25)
    parser.add_argument("--policy-candidates", type=int, default=420)
    parser.add_argument("--retained-policies", type=int, default=32)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--predictions-output", type=Path, required=True)
    parser.add_argument("--policy-output", type=Path, default=Path("artifacts/independent-exit-policy.json"))
    parser.add_argument("--guard-output", type=Path, default=Path("artifacts/independent-guard-bps.txt"))
    args = parser.parse_args()
    if args.mode == "apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply(args)
    return search(args)


if __name__ == "__main__":
    raise SystemExit(main())
