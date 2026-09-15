#!/usr/bin/env python3
"""Record the retired 300 ms causal resilience rank experiment."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as common
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-causal-resilience-rank-failure.json")
SCHEMA_VERSION = "e4-v12-latency300-causal-resilience-rank-failure-v1"
THESIS_FAMILY = "latency300-causal-resilience-rank-v12"
HISTORY_FEATURE_NAME = "buyer_history_count_mean"
VOLATILITY_FEATURE_NAME = "log_return_volatility"
HISTORY_FEATURE = actors.FEATURE_NAMES.index(HISTORY_FEATURE_NAME)
VOLATILITY_FEATURE = actors.FEATURE_NAMES.index(VOLATILITY_FEATURE_NAME)
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "class_weight": "balanced",
}
RANDOM_SEED_BASE = 102_120
SOURCE_FILES = (
    "scripts/e4_v12_latency300_causal_resilience_rank.py",
    "scripts/e4_v12_latency_flip_autopsy.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_adaptive_exit_search.py",
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


def percentiles(prior: list[float], values: np.ndarray) -> np.ndarray:
    ordered = np.sort(np.asarray(prior, dtype=np.float64))
    return (
        np.searchsorted(ordered, values, side="right").astype(np.float64) + 0.5
    ) / (len(ordered) + 1.0)


def resilience_scores(
    probabilities: np.ndarray,
    history_values: np.ndarray,
    volatility_values: np.ndarray,
    prior_history: list[float],
    prior_volatility: list[float],
) -> np.ndarray:
    scores = np.zeros(len(probabilities), dtype=np.float64)
    for start in range(0, len(scores), conformal.REFRESH_ROWS):
        stop = min(start + conformal.REFRESH_ROWS, len(scores))
        history_rank = percentiles(
            prior_history[-conformal.HISTORY_ROWS :],
            history_values[start:stop],
        )
        volatility_rank = percentiles(
            prior_volatility[-conformal.HISTORY_ROWS :],
            volatility_values[start:stop],
        )
        scores[start:stop] = probabilities[start:stop] * np.sqrt(
            history_rank * (1.0 - volatility_rank)
        )
        prior_history.extend(float(value) for value in history_values[start:stop])
        prior_volatility.extend(
            float(value) for value in volatility_values[start:stop]
        )
    return scores


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("300 ms source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("300 ms labels consumed untouched live evidence")

    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
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
            **MODEL_PARAMETERS,
            early_stopping=False,
            random_state=(
                RANDOM_SEED_BASE + fold_number * 100 + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit], pnl[fit] > 0)
        calibration_probabilities = model.predict_proba(features[calibration])[:, 1]
        test_probabilities = model.predict_proba(features[test])[:, 1]
        prior_history = [float(value) for value in features[fit, HISTORY_FEATURE]]
        prior_volatility = [
            float(value) for value in features[fit, VOLATILITY_FEATURE]
        ]
        calibration_scores = resilience_scores(
            calibration_probabilities,
            features[calibration, HISTORY_FEATURE],
            features[calibration, VOLATILITY_FEATURE],
            prior_history,
            prior_volatility,
        )
        test_scores = resilience_scores(
            test_probabilities,
            features[test, HISTORY_FEATURE],
            features[test, VOLATILITY_FEATURE],
            prior_history,
            prior_volatility,
        )
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

    summary = common.aggregate(folds)
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
        "full_quote_coverage": metadata["no_quote_rows"] == 0,
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
        "model_family": "causal_resilience_ranked_histogram_boosting",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "resilience_features": [
                HISTORY_FEATURE_NAME,
                VOLATILITY_FEATURE_NAME,
            ],
            "rank_combination": (
                "P(profit) * sqrt(history_rank * (1-volatility_rank))"
            ),
            "rank_history_rows": conformal.HISTORY_ROWS,
            "rank_refresh_rows": conformal.REFRESH_ROWS,
            "score_fraction": conformal.SCORE_FRACTION,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "rank 300ms profit probability by prior-only buyer-history and "
            "volatility desirability; accept above rolling 99.73rd percentile"
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
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "refresh_rows": conformal.REFRESH_ROWS,
            "score_fraction": conformal.SCORE_FRACTION,
            "threshold_uses_prior_scores_only": True,
        },
        "latency_assumptions_ms": [labels.LATENCY_MS],
        "execution_policy": (
            "paper replay at exact 300 ms entry delay; compound 1.85% of "
            "current bankroll; maximum two concurrent positions"
        ),
        "exit_policy": conformal.adaptive.POLICIES[conformal.POLICY_INDEX].key,
    }
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "scientific_basis": (
            "A latency-flip autopsy identified sparse proven buyer history and "
            "high early volatility in all four folds, motivating one fixed "
            "prior-only geometric resilience rank."
        ),
        "label_audit": metadata,
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": False,
        "retired": True,
        "failure_classification": "NEGATIVE_EXPECTANCY",
        "failure_reason": (
            "The arithmetic resilience rank distorted calibration and flooded "
            "the output: 172 of 504 trades won (34.13%), net PnL was -1.0721 "
            "SOL, PF was 0.766, and maximum fold drawdown reached 26.16%."
        ),
        "material_change_required_before_rerun": (
            "A genuinely new jointly calibrated model or causal feature family; "
            "rank exponent, weight, window, and threshold retuning are prohibited."
        ),
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "feature_ranks_use_prior_values_only": True,
            "threshold_uses_prior_scores_only": True,
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
