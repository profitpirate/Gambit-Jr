#!/usr/bin/env python3
"""Evaluate one frozen full-feature logistic candidate at exact 300 ms."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as common
import e4_v12_latency300_labels as labels
import e4_v12_latency300_market_flow_candidate as gates
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

OUTPUT = Path("artifacts/e4-v12-latency300-full-logit.json")
BASELINE_PATH = Path("artifacts/e4-v12-latency300-conformal-failure.json")
SCHEMA_VERSION = "e4-v12-latency300-full-logit-v1"
THESIS_FAMILY = "latency300-full-feature-logistic-v12"
MODEL_PARAMETERS = {
    "C": 0.1,
    "class_weight": "balanced",
    "max_iter": 1_000,
    "solver": "liblinear",
}
RANDOM_SEED_BASE = 162_120
SOURCE_FILES = (
    "scripts/e4_v12_latency300_full_logit.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_causal_actor_memory.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def model(seed: int):
    return make_pipeline(
        StandardScaler(),
        LogisticRegression(**MODEL_PARAMETERS, random_state=seed),
    )


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("300 ms source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float64)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("full feature matrix shape changed")
    if not bool(np.all(np.isfinite(features))):
        raise ValueError("full feature matrix contains non-finite values")
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
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
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        estimator = model(
            RANDOM_SEED_BASE + fold_number * 100 + conformal.POLICY_INDEX
        )
        estimator.fit(features[fit], pnl[fit] > 0)
        calibration_scores = estimator.predict_proba(features[calibration])[:, 1]
        test_scores = estimator.predict_proba(features[test])[:, 1]
        mask, thresholds = conformal.rolling_selection(calibration_scores, test_scores)
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
    requirements = gates.requirements_for(summary, folds, metadata)
    passed = all(requirements.values())
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
        "model_family": "standardized_l2_logistic_profit_classifier",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "scaler": "fit-only StandardScaler",
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "fit one standardized L2 logistic model to all 58 causal exact-300ms "
            "profit features; accept above the unchanged rolling 99.73rd "
            "percentile of prior scores"
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
            "paper replay at exact 300 ms entry delay; compound 1.85% of current "
            "bankroll; maximum two concurrent positions"
        ),
        "exit_policy": conformal.adaptive.POLICIES[conformal.POLICY_INDEX].key,
    }
    failed = [name for name, value in requirements.items() if not value]
    saturated_folds = [
        fold["fold"]
        for fold in folds
        if fold["threshold_minimum"] >= 1.0
        and fold["threshold_maximum"] >= 1.0
    ]
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "scientific_basis": (
            "A full-feature linear probability rank is less flexible than the "
            "retired tree ensemble and may transport under the measured regime drift."
        ),
        "label_audit": metadata,
        "baseline_comparison": {
            "baseline_experiment_id": baseline["experiment_id"],
            "candidate_minus_baseline_trades": summary["trades"] - baseline["aggregate"]["trades"],
            "candidate_minus_baseline_win_rate": summary["win_rate"] - baseline["aggregate"]["win_rate"],
            "candidate_minus_baseline_net_pnl_sol": summary["net_pnl_sol"] - baseline["aggregate"]["net_pnl_sol"],
            "candidate_minus_baseline_profit_factor": summary["profit_factor"] - baseline["aggregate"]["profit_factor"],
        },
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "output_guard_audit": {
            "saturated_probability_threshold_folds": saturated_folds,
            "maximum_attempted_trades_in_a_fold": max(
                fold["attempted_trades"] for fold in folds
            ),
            "strict_greater_than_substitution_tested": False,
            "post_result_guard_retuning_permitted": False,
        },
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "OUTPUT_DETERIORATION",
        "failure_reason": (
            None
            if passed
            else f"Full-feature logistic failed {', '.join(failed)}: "
            f"{summary['wins']}/{summary['trades']} wins ({summary['win_rate']:.2%}), "
            f"Wilson {summary['wilson_95_lower_bound']:.2%}, net "
            f"{summary['net_pnl_sol']:+.6f} SOL, PF {summary['profit_factor']:.3f}."
        ),
        "material_change_required_before_rerun": (
            "Freeze this identity and collect a new strictly later untouched holdout; no development retuning."
            if passed
            else "A genuinely new causal feature or model family; logistic regularization, feature subset, class-weight, and threshold variants are prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "scaler_fitted_on_fit_only": True,
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
