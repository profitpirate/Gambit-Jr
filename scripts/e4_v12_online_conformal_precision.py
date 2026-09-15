#!/usr/bin/env python3
"""Develop a causal online-conformal precision thesis on consumed E4 evidence.

This module is research-only.  It cannot promote or modify production V12 and
requires a new strictly later evidence epoch before any live conclusion.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

CACHE = Path(".tmp-relative-price-capped-actor-holdout/combined-rows.pkl")
PNL_MATRIX = Path(".tmp-relative-price-capped-actor-holdout/adaptive-pnl-matrix.npy")
DEVELOPMENT_OUTPUT = Path("artifacts/e4-v12-online-conformal-development.json")
SCHEMA_VERSION = "e4-v12-online-conformal-precision-v1"
THESIS_FAMILY = "window-conformal-stable-precision-v12"
FOLDS = ((24, 5, 10), (34, 5, 10), (44, 5, 10), (54, 5, 10))
POLICY_INDEX = 12
SCORE_FRACTION = 0.0027
HISTORY_ROWS = 10_000
REFRESH_ROWS = 100
MAXIMUM_HOLD_MS = adaptive.POLICIES[POLICY_INDEX].hold_ms
NOMINAL_POSITION = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
VARIABLE_FEE_RATE = base.PROTOCOL_AND_CREATOR_FEE_BPS / 10_000
FIXED_FEE = 0.001 + base.TIP_SOL + base.BASE_TRANSACTION_FEE_SOL


def gross_multiple_from_nominal_pnl(pnl_sol: float) -> float:
    return (
        pnl_sol + NOMINAL_POSITION * (1.0 + VARIABLE_FEE_RATE) + FIXED_FEE
    ) / (NOMINAL_POSITION * (1.0 - VARIABLE_FEE_RATE))


def rolling_selection(
    calibration_scores: np.ndarray,
    test_scores: np.ndarray,
) -> tuple[np.ndarray, list[float]]:
    selected = np.zeros(len(test_scores), dtype=bool)
    thresholds: list[float] = []
    prior = list(calibration_scores[-HISTORY_ROWS:])
    for start in range(0, len(test_scores), REFRESH_ROWS):
        stop = min(start + REFRESH_ROWS, len(test_scores))
        threshold = float(np.quantile(np.asarray(prior[-HISTORY_ROWS:]), 1.0 - SCORE_FRACTION))
        selected[start:stop] = test_scores[start:stop] >= threshold
        thresholds.append(threshold)
        prior.extend(test_scores[start:stop])
        if len(prior) > HISTORY_ROWS + REFRESH_ROWS:
            prior = prior[-HISTORY_ROWS:]
    return selected, thresholds


def replay(
    rows: list[base.ResearchRow],
    selected_indices: np.ndarray,
    nominal_pnl: np.ndarray,
    scores: np.ndarray,
) -> dict[str, Any]:
    candidates = sorted(
        zip(selected_indices.tolist(), scores.tolist(), strict=True),
        key=lambda item: (int(rows[item[0]].run_id), rows[item[0]].decision_ns, rows[item[0]].mint),
    )
    bankroll = base.STARTING_BANKROLL_SOL
    active: list[int] = []
    ledger: list[dict[str, Any]] = []
    rejected_concurrency = 0
    for index, score in candidates:
        row = rows[index]
        active = [exit_ns for exit_ns in active if exit_ns > row.decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        position = bankroll * base.POSITION_FRACTION
        outcome = base.Outcome(
            gross_multiple=gross_multiple_from_nominal_pnl(float(nominal_pnl[index])),
            exit_offset_ms=float(MAXIMUM_HOLD_MS),
            exit_reason="CONSERVATIVE_MAXIMUM_HOLD",
        )
        pnl_sol = base.net_pnl(outcome, 0.001, position)
        bankroll += pnl_sol
        active.append(row.decision_ns + MAXIMUM_HOLD_MS * 1_000_000)
        ledger.append(
            {
                "run_id": row.run_id,
                "mint": row.mint,
                "decision_ns": row.decision_ns,
                "score": score,
                "position_sol": position,
                "gross_multiple": outcome.gross_multiple,
                "pnl_sol": pnl_sol,
                "win": pnl_sol > 0,
            }
        )
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    equity = base.STARTING_BANKROLL_SOL
    peak = equity
    maximum_drawdown = 0.0
    by_window: dict[str, dict[str, Any]] = {}
    for row in ledger:
        equity += row["pnl_sol"]
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
        item = by_window.setdefault(row["run_id"], {"trades": 0, "wins": 0, "pnl_sol": 0.0})
        item["trades"] += 1
        item["wins"] += int(row["win"])
        item["pnl_sol"] += row["pnl_sol"]
    return {
        "attempted_trades": len(candidates),
        "trades": len(ledger),
        "wins": len(profits),
        "win_rate": len(profits) / max(len(ledger), 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(len(profits), len(ledger)),
        "net_pnl_sol": bankroll - base.STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": maximum_drawdown / base.STARTING_BANKROLL_SOL,
        "capture_windows": len(by_window),
        "profitable_capture_windows": sum(row["pnl_sol"] > 0 for row in by_window.values()),
        "largest_winner_contribution": max(profits, default=0.0) / max(sum(profits), 1e-12),
        "rejected_concurrency": rejected_concurrency,
        "conservative_exit_offset_ms": MAXIMUM_HOLD_MS,
        "by_capture_window": by_window,
        "ledger_hash": base.stable_hash(ledger),
        "ledger": ledger,
    }


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main() -> dict[str, Any]:
    with CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    pnl = np.load(PNL_MATRIX, allow_pickle=False)[:, POLICY_INDEX]
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    folds = []
    all_profits: list[float] = []
    all_losses: list[float] = []
    combined_ledger = []
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
        model.fit(features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(features[calibration])[:, 1]
        test_scores = model.predict_proba(features[test])[:, 1]
        mask, thresholds = rolling_selection(calibration_scores, test_scores)
        selected = test[mask]
        result = replay(rows, selected, pnl, test_scores[mask])
        result.update(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "threshold_minimum": min(thresholds),
                "threshold_maximum": max(thresholds),
                "threshold_hash": base.stable_hash(thresholds),
            }
        )
        folds.append(result)
        for row in result["ledger"]:
            combined_ledger.append(row)
            (all_profits if row["pnl_sol"] > 0 else all_losses).append(row["pnl_sol"])
    trades = sum(row["trades"] for row in folds)
    wins = sum(row["wins"] for row in folds)
    combined_windows = {
        key: value
        for fold in folds
        for key, value in fold["by_capture_window"].items()
    }
    aggregate = {
        "attempted_trades": sum(row["attempted_trades"] for row in folds),
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": sum(row["net_pnl_sol"] for row in folds),
        "profit_factor": sum(all_profits) / max(abs(sum(all_losses)), 1e-12),
        "maximum_fold_drawdown_fraction": max(row["maximum_drawdown_fraction"] for row in folds),
        "minimum_fold_win_rate": min(row["win_rate"] for row in folds),
        "minimum_fold_profit_factor": min(row["profit_factor"] for row in folds),
        "positive_folds": sum(row["net_pnl_sol"] > 0 for row in folds),
        "capture_windows": len(combined_windows),
        "profitable_capture_windows": sum(row["pnl_sol"] > 0 for row in combined_windows.values()),
        "largest_winner_contribution": max(all_profits, default=0.0) / max(sum(all_profits), 1e-12),
        "rejected_concurrency": sum(row["rejected_concurrency"] for row in folds),
        "ledger_hash": base.stable_hash(combined_ledger),
    }
    requirements = {
        "minimum_closed_trades": trades >= 300,
        "minimum_win_rate": aggregate["win_rate"] >= 0.65,
        "minimum_wilson_bound": aggregate["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": aggregate["net_pnl_sol"] > 0,
        "minimum_profit_factor": aggregate["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(row["trades"] >= 50 for row in folds),
        "every_fold_minimum_win_rate": all(row["win_rate"] >= 0.55 for row in folds),
        "every_fold_positive": all(row["net_pnl_sol"] > 0 for row in folds),
        "every_fold_minimum_profit_factor": all(row["profit_factor"] >= 1.10 for row in folds),
        "maximum_drawdown": aggregate["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": aggregate["largest_winner_contribution"] <= 0.15,
    }
    chronological_split = [
        {
            "fit": fold["fit_windows"],
            "calibration": fold["calibration_windows"],
            "test": fold["test_windows"],
        }
        for fold in folds
    ]
    model_parameters = {
        "learning_rate": 0.05,
        "max_iter": 100,
        "max_leaf_nodes": 15,
        "min_samples_leaf": 100,
        "l2_regularization": 2.0,
        "class_weight": "balanced",
        "random_seed_base": 12_120,
    }
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": sha256_lf(Path(__file__)),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "histogram_gradient_boosted_profit_classifier",
        "full_parameters": {
            "model": model_parameters,
            "score_fraction": SCORE_FRACTION,
            "rolling_history_rows": HISTORY_ROWS,
            "threshold_refresh_rows": REFRESH_ROWS,
            "adaptive_exit_policy_index": POLICY_INDEX,
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "score at 250ms; accept only above the causal rolling 99.73rd "
            "percentile of prior model scores"
        ),
        "chronological_split": {
            "development_folds": chronological_split,
            "frozen_live_training": {
                "fit": windows[:-5],
                "calibration": windows[-5:],
                "untouched_live": "exactly ten strictly future capture windows",
            },
        },
        "bankroll_sol": base.STARTING_BANKROLL_SOL,
        "position_sizing": {
            "fraction_of_current_bankroll": base.POSITION_FRACTION,
            "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": {
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
        },
        "output_guard": {
            "rolling_history_rows": HISTORY_ROWS,
            "refresh_rows": REFRESH_ROWS,
            "score_fraction": SCORE_FRACTION,
            "threshold_uses_prior_scores_only": True,
        },
        "latency_assumptions_ms": [0, 1, 2, 5, 10],
        "execution_policy": (
            "paper replay; compound 1.85% of current bankroll; maximum two "
            "concurrent positions; conservative 60-second occupancy"
        ),
        "exit_policy": adaptive.POLICIES[POLICY_INDEX].key,
    }
    output = {
        "version": SCHEMA_VERSION,
        "thesis_family": THESIS_FAMILY,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "data_audit": payload["audit"],
        "policy_index": POLICY_INDEX,
        "policy": adaptive.POLICIES[POLICY_INDEX].key,
        "score_fraction": SCORE_FRACTION,
        "rolling_history_rows": HISTORY_ROWS,
        "threshold_refresh_rows": REFRESH_ROWS,
        "model": {
            "family": "histogram_gradient_boosted_profit_classifier",
            "features": len(features[0]),
            **model_parameters,
        },
        "replay": {
            "starting_bankroll_sol": base.STARTING_BANKROLL_SOL,
            "position_fraction": base.POSITION_FRACTION,
            "maximum_concurrent_positions": base.MAX_CONCURRENT_POSITIONS,
            "priority_fee_sol": 0.001,
            "conservative_all_exits_assumed_at_maximum_hold": True,
        },
        "folds": folds,
        "aggregate": aggregate,
        "requirements": requirements,
        "development_gate_passed": all(requirements.values()),
        "ready_for_strictly_later_evidence": all(requirements.values()),
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "threshold_uses_prior_scores_only": True,
            "outcomes_never_update_model_or_threshold": True,
        },
        "production_paths_changed": 0,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
    }
    base.write_json(DEVELOPMENT_OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()
