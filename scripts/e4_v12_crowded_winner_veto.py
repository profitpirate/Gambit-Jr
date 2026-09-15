#!/usr/bin/env python3
"""Record the retired causal crowded-winner veto experiment.

The secondary gate uses only prior high-score candidates' observable buyer
history. This module is research-only and cannot promote or modify V12.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-crowded-winner-veto-failure.json")
BASELINE_DEVELOPMENT = Path("artifacts/e4-v12-online-conformal-development.json")
SCHEMA_VERSION = "e4-v12-crowded-winner-veto-failure-v1"
THESIS_FAMILY = "causal-crowded-winner-veto-v12"
SECONDARY_FEATURE = "buyer_average_pnl_max"
SECONDARY_FEATURE_INDEX = actors.FEATURE_NAMES.index(SECONDARY_FEATURE)
SECONDARY_UPPER_QUANTILE = 0.95
SECONDARY_HISTORY_CANDIDATES = 500
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "class_weight": "balanced",
    "random_seed_base": 12_120,
}
SOURCE_FILES = (
    "scripts/e4_v12_crowded_winner_veto.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def rolling_two_gate_selection(
    calibration_scores: np.ndarray,
    calibration_crowding: np.ndarray,
    test_scores: np.ndarray,
    test_crowding: np.ndarray,
) -> tuple[np.ndarray, list[float], list[float], int]:
    """Select high scores while vetoing the prior crowding upper tail."""
    selected = np.zeros(len(test_scores), dtype=bool)
    score_thresholds: list[float] = []
    crowding_thresholds: list[float] = []
    prior_scores = list(calibration_scores[-conformal.HISTORY_ROWS :])
    initial_score_threshold = float(
        np.quantile(np.asarray(prior_scores), 1.0 - conformal.SCORE_FRACTION)
    )
    initial_candidates = calibration_crowding[
        calibration_scores >= initial_score_threshold
    ]
    if not len(initial_candidates):
        initial_candidates = calibration_crowding
    prior_candidate_crowding = list(initial_candidates)
    vetoed = 0
    for start in range(0, len(test_scores), conformal.REFRESH_ROWS):
        stop = min(start + conformal.REFRESH_ROWS, len(test_scores))
        score_threshold = float(
            np.quantile(
                np.asarray(prior_scores[-conformal.HISTORY_ROWS :]),
                1.0 - conformal.SCORE_FRACTION,
            )
        )
        crowding_threshold = float(
            np.quantile(
                np.asarray(
                    prior_candidate_crowding[-SECONDARY_HISTORY_CANDIDATES:]
                ),
                SECONDARY_UPPER_QUANTILE,
            )
        )
        primary = test_scores[start:stop] >= score_threshold
        secondary = test_crowding[start:stop] <= crowding_threshold
        selected[start:stop] = primary & secondary
        vetoed += int(np.sum(primary & ~secondary))
        score_thresholds.append(score_threshold)
        crowding_thresholds.append(crowding_threshold)

        # Updates contain only features and scores observable at decision time.
        prior_scores.extend(test_scores[start:stop])
        prior_candidate_crowding.extend(test_crowding[start:stop][primary])
        if len(prior_scores) > conformal.HISTORY_ROWS + conformal.REFRESH_ROWS:
            prior_scores = prior_scores[-conformal.HISTORY_ROWS :]
        if len(prior_candidate_crowding) > SECONDARY_HISTORY_CANDIDATES + 100:
            prior_candidate_crowding = prior_candidate_crowding[
                -SECONDARY_HISTORY_CANDIDATES:
            ]
    return selected, score_thresholds, crowding_thresholds, vetoed


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
        "attempted_trades": sum(row["attempted_trades"] for row in folds),
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
        "crowded_winner_vetoes": sum(
            row["crowded_winner_vetoes"] for row in folds
        ),
        "ledger_hash": base.stable_hash(ledger),
    }


def compact_fold(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"ledger", "by_capture_window"}
    }


def main() -> dict[str, Any]:
    baseline_report = json.loads(BASELINE_DEVELOPMENT.read_text(encoding="utf-8"))
    baseline = baseline_report["aggregate"]
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
        calibration = calibration[
            np.argsort(decisions[calibration], kind="stable")
        ]
        test = test[np.argsort(decisions[test], kind="stable")]
        model = HistGradientBoostingClassifier(
            **{
                key: value
                for key, value in MODEL_PARAMETERS.items()
                if key != "random_seed_base"
            },
            early_stopping=False,
            random_state=(
                MODEL_PARAMETERS["random_seed_base"]
                + fold_number * 100
                + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(features[calibration])[:, 1]
        test_scores = model.predict_proba(features[test])[:, 1]
        mask, score_thresholds, crowding_thresholds, vetoed = (
            rolling_two_gate_selection(
                calibration_scores,
                features[calibration, SECONDARY_FEATURE_INDEX],
                test_scores,
                features[test, SECONDARY_FEATURE_INDEX],
            )
        )
        result = conformal.replay(rows, test[mask], pnl, test_scores[mask])
        result.update(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "score_threshold_minimum": min(score_thresholds),
                "score_threshold_maximum": max(score_thresholds),
                "score_threshold_hash": base.stable_hash(score_thresholds),
                "crowding_threshold_minimum": min(crowding_thresholds),
                "crowding_threshold_maximum": max(crowding_thresholds),
                "crowding_threshold_hash": base.stable_hash(
                    crowding_thresholds
                ),
                "crowded_winner_vetoes": vetoed,
            }
        )
        folds.append(result)

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
        "every_fold_six_profitable_windows": all(
            row["profitable_capture_windows"] >= 6 for row in folds
        ),
        "maximum_drawdown": summary["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": summary["largest_winner_contribution"] <= 0.15,
    }
    incremental_requirements = {
        "minimum_win_rate_uplift_50bps": (
            summary["win_rate"] >= baseline["win_rate"] + 0.005
        ),
        "wilson_bound_not_lower": (
            summary["wilson_95_lower_bound"]
            >= baseline["wilson_95_lower_bound"]
        ),
        "net_pnl_not_lower": summary["net_pnl_sol"] >= baseline["net_pnl_sol"],
        "profit_factor_not_lower": (
            summary["profit_factor"] >= baseline["profit_factor"]
        ),
        "minimum_fold_profit_factor_not_lower": (
            summary["minimum_fold_profit_factor"]
            >= baseline["minimum_fold_profit_factor"]
        ),
    }
    chronology = [
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
        "model_family": "histogram_gradient_boosted_profit_classifier",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "primary_score_fraction": conformal.SCORE_FRACTION,
            "rolling_score_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "secondary_feature": SECONDARY_FEATURE,
            "secondary_upper_quantile": SECONDARY_UPPER_QUANTILE,
            "secondary_history_candidates": SECONDARY_HISTORY_CANDIDATES,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "score at 250ms; require the causal rolling 99.73rd-percentile "
            "profit score and veto the prior high-score candidates' rolling "
            "95th-percentile maximum buyer-history PnL upper tail"
        ),
        "chronological_split": chronology,
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
            "rolling_score_history_rows": conformal.HISTORY_ROWS,
            "secondary_history_candidates": SECONDARY_HISTORY_CANDIDATES,
            "refresh_rows": conformal.REFRESH_ROWS,
            "score_fraction": conformal.SCORE_FRACTION,
            "crowding_upper_quantile": SECONDARY_UPPER_QUANTILE,
            "thresholds_use_prior_observations_only": True,
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
        "scientific_basis": (
            "buyer_average_pnl_max had inverse winner AUC in every consumed "
            "development fold, consistent with crowded historical winners"
        ),
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "baseline_experiment_id": baseline_report["experiment_id"],
        "baseline": baseline,
        "aggregate": summary,
        "folds": [compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "incremental_requirements": incremental_requirements,
        "development_gate_passed": False,
        "retired": True,
        "failure_classification": "VALIDATION_COLLAPSE",
        "failure_reason": (
            "Aggregate WR, Wilson bound, PnL, PF, and drawdown improved, but "
            "only 296 trades survived and the final fold represented only "
            "five profitable windows; the preregistered breadth gates failed."
        ),
        "material_change_required_before_rerun": (
            "A new evidence epoch large enough to reassess breadth without "
            "reusing these fold outcomes; parameter-only reruns are prohibited."
        ),
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "both_thresholds_use_prior_features_and_scores_only": True,
            "outcomes_never_update_model_or_thresholds": True,
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
