#!/usr/bin/env python3
"""Record the retired 300 ms Extra Trees ranking experiment."""

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
from sklearn.ensemble import ExtraTreesClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-extra-trees-failure.json")
SCHEMA_VERSION = "e4-v12-latency300-extra-trees-failure-v1"
THESIS_FAMILY = "latency300-extremely-randomized-ranking-v12"
MODEL_PARAMETERS = {
    "n_estimators": 256,
    "criterion": "log_loss",
    "max_depth": 16,
    "min_samples_leaf": 100,
    "max_features": "sqrt",
    "class_weight": "balanced",
    # Serial construction avoids sub-ULP score variation from parallel
    # reductions, making threshold and ledger fingerprints reproducible.
    "n_jobs": 1,
    "random_seed_base": 82_120,
}
SOURCE_FILES = (
    "scripts/e4_v12_latency300_extra_trees.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
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
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
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
        model = ExtraTreesClassifier(
            **{
                key: value
                for key, value in MODEL_PARAMETERS.items()
                if key != "random_seed_base"
            },
            random_state=(
                MODEL_PARAMETERS["random_seed_base"]
                + fold_number * 100
                + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(features[calibration])[:, 1]
        test_scores = model.predict_proba(features[test])[:, 1]
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
        "model_family": "extra_trees_latency_profit_classifier",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "fit 300 ms net-profit labels with one preregistered Extra Trees "
            "model; score at 250 ms; accept above the causal rolling 99.73rd "
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
            "A single preregistered extremely randomized tree model tested "
            "whether nonlinear feature interactions recover the exact 300 ms "
            "execution edge without parameter search."
        ),
        "label_audit": metadata,
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": False,
        "retired": True,
        "failure_classification": "VALIDATION_COLLAPSE",
        "failure_reason": (
            "The preregistered model reached 59.12% aggregate WR and 53.44% "
            "Wilson lower bound; the final chronological fold fell to 47.50% "
            "WR, below the frozen aggregate and per-fold requirements."
        ),
        "material_change_required_before_rerun": (
            "A genuinely new causal feature family or model family developed "
            "without these fold outcomes; parameter-only reruns are prohibited."
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
