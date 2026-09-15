#!/usr/bin/env python3
"""Ablate the causal online-conformal development candidate."""

from __future__ import annotations

import json
import math
import pickle
from pathlib import Path
from typing import Any

import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

CACHE = Path(".tmp-relative-price-capped-actor-holdout/combined-rows.pkl")
PNL_MATRIX = Path(".tmp-relative-price-capped-actor-holdout/adaptive-pnl-matrix.npy")
DEVELOPMENT = Path("artifacts/e4-v12-online-conformal-development.json")
OUTPUT = Path("artifacts/e4-v12-online-conformal-ablation.json")
FOLDS = ((24, 5, 10), (34, 5, 10), (44, 5, 10), (54, 5, 10))
POLICY_INDEX = 12
SCORE_FRACTION = 0.0027
HISTORY_ROWS = 10_000
REFRESH_ROWS = 100
FEATURE_VARIANTS = {
    "full_58": tuple(range(58)),
    "without_actor_memory_42": tuple(range(42)),
    "without_market_context_46": (*range(30), *range(42, 58)),
    "base_snapshot_only_30": tuple(range(30)),
    "actor_memory_only_16": tuple(range(42, 58)),
}


def wilson(wins: int, trades: int) -> float:
    if trades <= 0:
        return 0.0
    z = 1.96
    p = wins / trades
    denominator = 1.0 + z * z / trades
    centre = p + z * z / (2.0 * trades)
    spread = z * math.sqrt(p * (1.0 - p) / trades + z * z / (4.0 * trades * trades))
    return (centre - spread) / denominator


def metrics(values: np.ndarray) -> dict[str, Any]:
    trades = len(values)
    wins = int((values > 0).sum())
    profit = float(values[values > 0].sum())
    loss = float(values[values < 0].sum())
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": wilson(wins, trades),
        "net_pnl_sol": profit + loss,
        "profit_factor": profit / max(abs(loss), 1e-12),
        "gross_profit_sol": profit,
        "gross_loss_sol": loss,
    }


def aggregate(folds: list[dict[str, Any]]) -> dict[str, Any]:
    trades = sum(row["trades"] for row in folds)
    wins = sum(row["wins"] for row in folds)
    profit = sum(row["gross_profit_sol"] for row in folds)
    loss = sum(row["gross_loss_sol"] for row in folds)
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": wilson(wins, trades),
        "net_pnl_sol": profit + loss,
        "profit_factor": profit / max(abs(loss), 1e-12),
        "minimum_fold_win_rate": min(row["win_rate"] for row in folds),
        "minimum_fold_profit_factor": min(row["profit_factor"] for row in folds),
        "positive_folds": sum(row["net_pnl_sol"] > 0 for row in folds),
    }


def rolling_mask(calibration: np.ndarray, test: np.ndarray) -> np.ndarray:
    mask = np.zeros(len(test), dtype=bool)
    prior = list(calibration[-HISTORY_ROWS:])
    for start in range(0, len(test), REFRESH_ROWS):
        stop = min(start + REFRESH_ROWS, len(test))
        threshold = float(np.quantile(np.asarray(prior[-HISTORY_ROWS:]), 1.0 - SCORE_FRACTION))
        mask[start:stop] = test[start:stop] >= threshold
        prior.extend(test[start:stop])
        if len(prior) > HISTORY_ROWS + REFRESH_ROWS:
            prior = prior[-HISTORY_ROWS:]
    return mask


def main() -> dict[str, Any]:
    development = json.loads(DEVELOPMENT.read_text(encoding="utf-8"))
    with CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    adaptive_pnl = np.load(PNL_MATRIX, allow_pickle=False)[:, POLICY_INDEX]
    baseline_pnl = np.asarray(
        [
            base.net_pnl(
                row.outcomes[0],
                0.001,
                base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION,
            )
            for row in rows
        ],
        dtype=np.float32,
    )
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    results: dict[str, dict[str, Any]] = {}
    full_masks: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []

    for variant_number, (name, columns) in enumerate(FEATURE_VARIANTS.items()):
        fold_results = []
        for fold_number, (test_start, calibration_size, test_size) in enumerate(FOLDS):
            fit_end = test_start - calibration_size
            fit = np.flatnonzero(row_windows < fit_end)
            calibration = np.flatnonzero((row_windows >= fit_end) & (row_windows < test_start))
            test = np.flatnonzero((row_windows >= test_start) & (row_windows < test_start + test_size))
            calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
            test = test[np.argsort(decisions[test], kind="stable")]
            model = HistGradientBoostingClassifier(
                learning_rate=0.05,
                max_iter=100,
                max_leaf_nodes=15,
                min_samples_leaf=100,
                l2_regularization=2.0,
                class_weight="balanced",
                early_stopping=False,
                random_state=12_120 + fold_number * 100 + POLICY_INDEX,
            )
            model.fit(features[np.ix_(fit, columns)], adaptive_pnl[fit] > 0)
            calibration_score = model.predict_proba(features[np.ix_(calibration, columns)])[:, 1]
            test_score = model.predict_proba(features[np.ix_(test, columns)])[:, 1]
            mask = rolling_mask(calibration_score, test_score)
            fold_results.append(metrics(adaptive_pnl[test][mask]))
            if name == "full_58":
                full_masks.append((calibration_score, test_score, test, test[mask]))
        results[name] = {
            "features": len(columns),
            "folds": fold_results,
            "aggregate": aggregate(fold_results),
        }

    static_folds = []
    fixed_exit_folds = []
    for calibration_score, test_score, test, rolling_selected in full_masks:
        threshold = float(np.quantile(calibration_score, 1.0 - SCORE_FRACTION))
        static_folds.append(metrics(adaptive_pnl[test][test_score >= threshold]))
        fixed_exit_folds.append(metrics(baseline_pnl[rolling_selected]))
    results["fixed_calibration_threshold"] = {
        "folds": static_folds,
        "aggregate": aggregate(static_folds),
    }
    results["fixed_baseline_exit"] = {
        "folds": fixed_exit_folds,
        "aggregate": aggregate(fixed_exit_folds),
    }

    shuffled_folds = []
    rng = np.random.default_rng(12_120)
    for fold_number, (test_start, calibration_size, test_size) in enumerate(FOLDS):
        fit_end = test_start - calibration_size
        fit = np.flatnonzero(row_windows < fit_end)
        calibration = np.flatnonzero((row_windows >= fit_end) & (row_windows < test_start))
        test = np.flatnonzero((row_windows >= test_start) & (row_windows < test_start + test_size))
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        labels = adaptive_pnl[fit] > 0
        labels = labels[rng.permutation(len(labels))]
        model = HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=100,
            max_leaf_nodes=15,
            min_samples_leaf=100,
            l2_regularization=2.0,
            class_weight="balanced",
            early_stopping=False,
            random_state=13_120 + fold_number,
        )
        model.fit(features[fit], labels)
        calibration_score = model.predict_proba(features[calibration])[:, 1]
        test_score = model.predict_proba(features[test])[:, 1]
        shuffled_folds.append(metrics(adaptive_pnl[test][rolling_mask(calibration_score, test_score)]))
    results["shuffled_labels"] = {
        "folds": shuffled_folds,
        "aggregate": aggregate(shuffled_folds),
    }

    full = results["full_58"]["aggregate"]
    requirements = {
        "actor_memory_adds_precision": full["win_rate"] > results["without_actor_memory_42"]["aggregate"]["win_rate"],
        "market_context_adds_precision": full["win_rate"] > results["without_market_context_46"]["aggregate"]["win_rate"],
        "rolling_guard_adds_precision": full["win_rate"] > results["fixed_calibration_threshold"]["aggregate"]["win_rate"],
        "adaptive_exit_adds_precision": full["win_rate"] > results["fixed_baseline_exit"]["aggregate"]["win_rate"],
        "real_labels_beat_shuffle": full["win_rate"] > results["shuffled_labels"]["aggregate"]["win_rate"],
        "real_labels_beat_shuffle_pnl": full["net_pnl_sol"] > results["shuffled_labels"]["aggregate"]["net_pnl_sol"],
    }
    output = {
        "version": "e4-v12-online-conformal-ablation-v1",
        "thesis_family": "window-conformal-stable-precision-v12",
        "experiment_id": development["experiment_id"],
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "results": results,
        "requirements": requirements,
        "ablation_gate_passed": all(requirements.values()),
        "production_paths_changed": 0,
        "untouched_holdout_passed": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()
