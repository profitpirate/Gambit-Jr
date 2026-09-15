#!/usr/bin/env python3
"""Record the retired lower-tail PnL conformal experiment deterministically."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

OUTPUT = Path("artifacts/e4-v12-lower-tail-profit-conformal-failure.json")
SCHEMA_VERSION = "e4-v12-lower-tail-profit-conformal-failure-v1"
THESIS_FAMILY = "lower-tail-profit-conformal-v12"
TARGET_QUANTILE = 0.25
MODEL_PARAMETERS = {
    "loss": "quantile",
    "quantile": TARGET_QUANTILE,
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "random_seed_base": 32_120,
}
SOURCE_FILES = (
    "scripts/e4_v12_lower_tail_profit_conformal.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def aggregate(folds: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = [trade for fold in folds for trade in fold["ledger"]]
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    trades = len(ledger)
    wins = len(profits)
    windows = {
        run_id: row
        for fold in folds
        for run_id, row in fold["by_capture_window"].items()
    }
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": sum(row["net_pnl_sol"] for row in folds),
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_fold_drawdown_fraction": max(
            row["maximum_drawdown_fraction"] for row in folds
        ),
        "minimum_fold_win_rate": min(row["win_rate"] for row in folds),
        "minimum_fold_profit_factor": min(row["profit_factor"] for row in folds),
        "minimum_fold_profitable_windows": min(
            row["profitable_capture_windows"] for row in folds
        ),
        "positive_folds": sum(row["net_pnl_sol"] > 0 for row in folds),
        "capture_windows": len(windows),
        "profitable_capture_windows": sum(
            row["pnl_sol"] > 0 for row in windows.values()
        ),
        "largest_winner_contribution": max(profits, default=0.0)
        / max(sum(profits), 1e-12),
        "rejected_concurrency": sum(row["rejected_concurrency"] for row in folds),
        "ledger_hash": base.stable_hash(ledger),
    }


def compact_fold(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"ledger", "by_capture_window"}
    }


def main() -> dict[str, Any]:
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    pnl = np.load(conformal.PNL_MATRIX, allow_pickle=False)[:, conformal.POLICY_INDEX]
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    folds: list[dict[str, Any]] = []
    score_diagnostics = []
    for fold_number, (test_start, calibration_size, test_size) in enumerate(
        conformal.FOLDS
    ):
        fit_end = test_start - calibration_size
        fit = np.flatnonzero(row_windows < fit_end)
        calibration = np.flatnonzero(
            (row_windows >= fit_end) & (row_windows < test_start)
        )
        test = np.flatnonzero(
            (row_windows >= test_start) & (row_windows < test_start + test_size)
        )
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        model = HistGradientBoostingRegressor(
            **{key: value for key, value in MODEL_PARAMETERS.items() if key != "random_seed_base"},
            early_stopping=False,
            random_state=(
                MODEL_PARAMETERS["random_seed_base"]
                + fold_number * 100
                + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit], pnl[fit])
        calibration_scores = model.predict(features[calibration])
        test_scores = model.predict(features[test])
        mask, thresholds = conformal.rolling_selection(
            calibration_scores, test_scores
        )
        result = conformal.replay(rows, test[mask], pnl, test_scores[mask])
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
        unique_test_scores, test_counts = np.unique(test_scores, return_counts=True)
        score_diagnostics.append(
            {
                "fold": fold_number,
                "calibration_rows": len(calibration),
                "calibration_unique_scores": int(
                    np.unique(calibration_scores).size
                ),
                "test_rows": len(test),
                "test_unique_scores": int(unique_test_scores.size),
                "largest_test_score_tie": int(test_counts.max()),
                "attempted_trades": result["attempted_trades"],
                "threshold_minimum": min(thresholds),
                "threshold_maximum": max(thresholds),
            }
        )

    summary = aggregate(folds)
    requirements = {
        "minimum_closed_trades": summary["trades"] >= 300,
        "minimum_win_rate": summary["win_rate"] >= 0.65,
        "minimum_wilson_bound": summary["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": summary["net_pnl_sol"] > 0,
        "minimum_profit_factor": summary["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(row["trades"] >= 50 for row in folds),
        "every_fold_minimum_win_rate": all(
            row["win_rate"] >= 0.55 for row in folds
        ),
        "every_fold_positive": all(row["net_pnl_sol"] > 0 for row in folds),
        "every_fold_minimum_profit_factor": all(
            row["profit_factor"] >= 1.10 for row in folds
        ),
        "maximum_drawdown": summary["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": summary["largest_winner_contribution"] <= 0.15,
    }
    chronological_split = [
        {
            "fit": fold["fit_windows"],
            "calibration": fold["calibration_windows"],
            "test": fold["test_windows"],
        }
        for fold in folds
    ]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "histogram_gradient_boosted_quantile_regressor",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "score at 250ms by predicted 25th-percentile policy PnL; accept "
            "above the causal rolling 99.73rd percentile of prior scores"
        ),
        "chronological_split": chronological_split,
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
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "refresh_rows": conformal.REFRESH_ROWS,
            "score_fraction": conformal.SCORE_FRACTION,
            "threshold_uses_prior_scores_only": True,
            "ties_are_inclusive": True,
        },
        "latency_assumptions_ms": [0, 1, 2, 5, 10],
        "execution_policy": (
            "paper replay; compound 1.85% of current bankroll; maximum two "
            "concurrent positions; conservative 60-second occupancy"
        ),
        "exit_policy": conformal.adaptive.POLICIES[conformal.POLICY_INDEX].key,
    }
    experiment_id = f"e4x-{base.stable_hash(identity)}"
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "aggregate": summary,
        "folds": [compact_fold(fold) for fold in folds],
        "score_degeneracy": score_diagnostics,
        "requirements": requirements,
        "development_gate_passed": False,
        "retired": True,
        "failure_classification": "NEGATIVE_EXPECTANCY",
        "failure_reason": (
            "The 25th-percentile PnL target collapsed into large tied score "
            "blocks; inclusive conformal thresholds admitted concentrated "
            "losers and failed every economic gate."
        ),
        "material_change_required_before_rerun": (
            "A non-degenerate causal target transformation or a different "
            "model family; parameter-only reruns are prohibited."
        ),
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "threshold_uses_prior_scores_only": True,
            "outcomes_never_update_model_or_threshold": True,
            "active_untouched_live_data_used": False,
        },
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()
