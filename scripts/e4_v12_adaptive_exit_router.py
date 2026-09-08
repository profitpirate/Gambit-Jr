#!/usr/bin/env python3
"""Profit-first entry selector with a causal per-launch exit-policy router.

A single global exit can hide an otherwise valid entry edge.  This experiment
fits one expected-utility model per predeclared independent exit policy using
training windows only.  At launch time it may abstain or choose the policy with
the highest predicted worst-latency utility.  Realised economics are then read
from that frozen policy at each independently simulated 0/1/2/5/10ms latency.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor

from scripts import e4_v12_allout_profit_hazard as base
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-adaptive-exit-router-v1"


def safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return safe(value.tolist())
    if isinstance(value, np.generic):
        return safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fit_router(
    corpus: base.Corpus,
    train_mask: np.ndarray,
    model_family: str,
    identity: bool,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, Any]]:
    x = corpus.x_identity if identity else corpus.x_general
    policies = list(corpus.policies)
    predictions = np.zeros((len(corpus.rows), len(policies)), dtype=np.float64)
    uncertainties = np.zeros_like(predictions)
    model_summaries = []

    for policy_index, policy in enumerate(policies):
        target = corpus.pnl[policy].min(axis=1)
        if model_family == "extra":
            model = ExtraTreesRegressor(
                n_estimators=480,
                max_depth=14,
                min_samples_leaf=5,
                max_features="sqrt",
                n_jobs=-1,
                random_state=seed + policy_index * 17,
            )
            model.fit(x[train_mask], target[train_mask])
            tree_predictions = np.asarray(
                [tree.predict(x) for tree in model.estimators_], dtype=np.float32
            )
            predictions[:, policy_index] = tree_predictions.mean(axis=0)
            uncertainties[:, policy_index] = tree_predictions.std(axis=0)
        elif model_family == "hgb":
            model = HistGradientBoostingRegressor(
                learning_rate=0.035,
                max_iter=360,
                max_leaf_nodes=15,
                min_samples_leaf=30,
                l2_regularization=4.0,
                random_state=seed + policy_index * 17,
            )
            model.fit(x[train_mask], target[train_mask])
            predictions[:, policy_index] = model.predict(x)
            # HGB does not expose an ensemble dispersion.  Use absolute distance
            # from the training median as a deterministic conservative proxy.
            median = float(np.median(target[train_mask]))
            uncertainties[:, policy_index] = np.abs(predictions[:, policy_index] - median)
        else:
            raise ValueError(model_family)
        model_summaries.append(
            {
                "policy": policy,
                "train_target_mean": float(target[train_mask].mean()),
                "train_target_positive_rate": float((target[train_mask] > 0).mean()),
            }
        )
    return predictions, uncertainties, policies, {
        "model_family": model_family,
        "identity_features": identity,
        "models": model_summaries,
    }


def adaptive_economics(
    indices: Sequence[int],
    scores: np.ndarray,
    policy_indexes: np.ndarray,
    corpus: base.Corpus,
    latency_column: int,
) -> dict[str, Any]:
    ordered = sorted(
        {int(index) for index in indices},
        key=lambda index: (int(corpus.create_ns[index]), str(corpus.mints[index])),
    )
    cash = base.STARTING_BANKROLL_SOL
    peak_equity = cash
    maximum_drawdown = 0.0
    open_positions: list[tuple[int, int, float, float, dict[str, Any]]] = []
    sequence = 0
    closed: list[dict[str, Any]] = []
    skipped_concurrency = 0

    def equity() -> float:
        return cash + sum(item[2] for item in open_positions)

    def update_drawdown() -> None:
        nonlocal peak_equity, maximum_drawdown
        value = equity()
        peak_equity = max(peak_equity, value)
        if peak_equity > 0:
            maximum_drawdown = max(maximum_drawdown, (peak_equity - value) / peak_equity)

    def settle(timestamp_ns: int) -> None:
        nonlocal cash
        while open_positions and open_positions[0][0] <= timestamp_ns:
            _, _, stake, pnl, record = heapq.heappop(open_positions)
            cash += stake + pnl
            record["pnl_sol"] = pnl
            record["ending_cash_after_exit_sol"] = cash
            closed.append(record)
            update_drawdown()

    for index in ordered:
        now = int(corpus.create_ns[index])
        settle(now)
        if len(open_positions) >= base.MAX_CONCURRENT:
            skipped_concurrency += 1
            continue
        current_equity = equity()
        stake = min(max(0.0, cash - base.RESERVE_SOL), current_equity * base.POSITION_FRACTION)
        if stake <= 0:
            continue
        policy_index = int(policy_indexes[index])
        policy = corpus.policies[policy_index]
        reference_pnl = base.finite(corpus.pnl[policy][index, latency_column])
        pnl = stake * reference_pnl / base.ENTRY_BUDGET_SOL
        exit_delay = max(0.0, base.finite(corpus.exit_delay_ms[policy][index, latency_column]))
        exit_ns = now + int(exit_delay * 1_000_000)
        cash -= stake
        record = {
            "index": index,
            "mint": str(corpus.mints[index]),
            "run_id": str(corpus.run_ids[index]),
            "create_ns": now,
            "exit_ns": exit_ns,
            "policy": policy,
            "score": float(scores[index]),
            "stake_sol": stake,
            "pnl_sol": None,
            "selected_by_e4": bool(corpus.selected[index]),
            "landed_by_e4": bool(corpus.landed[index]),
        }
        heapq.heappush(open_positions, (exit_ns, sequence, stake, pnl, record))
        sequence += 1
        update_drawdown()
    settle(2**63 - 1)

    pnls = [float(row["pnl_sol"]) for row in closed]
    wins = sum(value > 0 for value in pnls)
    gains = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    total_profit = sum(gains)
    return {
        "trades": len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": wins / len(closed) if closed else 0.0,
        "wilson_lower": base.wilson_lower(wins, len(closed)),
        "net_pnl_sol": cash - base.STARTING_BANKROLL_SOL,
        "ending_bankroll_sol": cash,
        "profit_factor": base.profit_factor(pnls),
        "maximum_drawdown_fraction": maximum_drawdown,
        "expectancy_sol": statistics.fmean(pnls) if pnls else 0.0,
        "average_win_sol": statistics.fmean(gains) if gains else 0.0,
        "average_loss_sol": statistics.fmean(losses) if losses else 0.0,
        "largest_winner_profit_share": (
            max(gains, default=0.0) / total_profit if total_profit > 0 else 0.0
        ),
        "capture_windows": len({row["run_id"] for row in closed}),
        "e4_selected_overlap": sum(row["selected_by_e4"] for row in closed),
        "skipped_for_concurrency": skipped_concurrency,
        "policy_counts": dict(
            sorted(
                __import__("collections").Counter(row["policy"] for row in closed).items()
            )
        ),
        "chronology": "stake locked at entry; adaptive policy frozen at entry; proceeds settle at exit",
        "closed": closed,
    }


def multi_latency(
    indices: Sequence[int],
    scores: np.ndarray,
    policy_indexes: np.ndarray,
    corpus: base.Corpus,
) -> dict[str, Any]:
    results = {
        str(latency): adaptive_economics(
            indices, scores, policy_indexes, corpus, column
        )
        for column, latency in enumerate(base.LATENCIES)
    }
    blocks = list(results.values())
    return {
        "latencies": results,
        "minimum_trades": min((row["trades"] for row in blocks), default=0),
        "minimum_win_rate": min((row["win_rate"] for row in blocks), default=0.0),
        "minimum_wilson_lower": min((row["wilson_lower"] for row in blocks), default=0.0),
        "minimum_profit_factor": min((row["profit_factor"] for row in blocks), default=0.0),
        "minimum_net_pnl_sol": min((row["net_pnl_sol"] for row in blocks), default=-999.0),
        "maximum_drawdown_fraction": max((row["maximum_drawdown_fraction"] for row in blocks), default=1.0),
        "minimum_expectancy_sol": min((row["expectancy_sol"] for row in blocks), default=-999.0),
        "maximum_largest_winner_profit_share": max((row["largest_winner_profit_share"] for row in blocks), default=1.0),
        "minimum_capture_windows": min((row["capture_windows"] for row in blocks), default=0),
    }


def candidate_grid(
    score: np.ndarray,
    uncertainty: np.ndarray,
    validation: np.ndarray,
) -> list[tuple[float, float]]:
    score_values = score[validation]
    uncertainty_values = uncertainty[validation]
    score_thresholds = {
        float(np.quantile(score_values, q))
        for q in (0.90, 0.94, 0.96, 0.975, 0.985, 0.99, 0.995, 0.997, 0.999)
    }
    uncertainty_thresholds = {
        float(np.quantile(uncertainty_values, q))
        for q in (0.25, 0.50, 0.70, 0.85, 0.95, 1.0)
    }
    return sorted(
        {(score_threshold, uncertainty_threshold) for score_threshold in score_thresholds for uncertainty_threshold in uncertainty_thresholds},
        reverse=True,
    )


def run_experiment(
    corpus: base.Corpus,
    family: str,
    identity: bool,
    seed: int,
) -> dict[str, Any]:
    train = corpus.splits == "train"
    validation = corpus.splits == "validation"
    holdout = corpus.splits == "holdout"
    predictions, uncertainties, policies, model = fit_router(
        corpus, train, family, identity, seed
    )
    policy_indexes = np.argmax(predictions, axis=1)
    row_indexes = np.arange(len(corpus.rows))
    best_prediction = predictions[row_indexes, policy_indexes]
    chosen_uncertainty = uncertainties[row_indexes, policy_indexes]

    candidates = []
    for score_threshold, uncertainty_threshold in candidate_grid(
        best_prediction, chosen_uncertainty, validation
    ):
        mask = validation & (best_prediction >= score_threshold) & (chosen_uncertainty <= uncertainty_threshold)
        indices = np.flatnonzero(mask)
        if len(indices) < 8:
            continue
        metrics = multi_latency(indices, best_prediction, policy_indexes, corpus)
        candidates.append(
            {
                "score_threshold": score_threshold,
                "uncertainty_threshold": uncertainty_threshold,
                "validation": metrics,
                "eligible": base.validation_eligible(metrics),
            }
        )
    if not candidates:
        return {
            "version": VERSION,
            "status": "NOT_CONCLUSIVE",
            "reason": "no validation candidate reached eight autonomous trades",
            "model": model,
        }
    winner = max(
        candidates,
        key=lambda row: (
            1 if row["eligible"] else 0,
            *base.objective(row["validation"]),
        ),
    )
    holdout_indices = np.flatnonzero(
        holdout
        & (best_prediction >= winner["score_threshold"])
        & (chosen_uncertainty <= winner["uncertainty_threshold"])
    )
    holdout_metrics = multi_latency(
        holdout_indices, best_prediction, policy_indexes, corpus
    )
    passed, failures = base.historical_pass(holdout_metrics)
    return {
        "version": VERSION,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "seed": seed,
        "model": model,
        "policies": policies,
        "winner": {
            "score_threshold": winner["score_threshold"],
            "uncertainty_threshold": winner["uncertainty_threshold"],
            "validation": winner["validation"],
            "holdout": holdout_metrics,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_candidates": sorted(
            candidates,
            key=lambda row: (1 if row["eligible"] else 0, *base.objective(row["validation"])),
            reverse=True,
        )[:100],
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--family", choices=("extra", "hgb"), required=True)
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpus = load_corpus_stream(args.corpus, 0)
    report = run_experiment(corpus, args.family, args.identity, args.seed)
    corpus_hash = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = corpus_hash
    report["experiment_id"] = "e4x-router-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "family": args.family,
                "identity": args.identity,
                "seed": args.seed,
                "corpus": corpus_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    write_json(args.output, report)
    print(json.dumps({"status":report.get("status"),"experiment_id":report["experiment_id"],"winner":{k:v for k,v in (report.get("winner") or {}).items() if k in {"score_threshold","uncertainty_threshold","historical_pass","failed_requirements"}}},indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
