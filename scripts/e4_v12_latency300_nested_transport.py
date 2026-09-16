#!/usr/bin/env python3
"""Evaluate fit-only chronological transport filtering at exact 300 ms."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as common
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-nested-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-nested-transport-v1"
THESIS_FAMILY = "latency300-nested-transport-filter-v12"
FIT_SUBEPOCHS = 4
AUC_FLOOR = 0.52
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "class_weight": "balanced",
}
RANDOM_SEED_BASE = 142_120
SOURCE_FILES = (
    "scripts/e4_v12_latency300_nested_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
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


def nested_transport_selection(
    features: np.ndarray,
    profitable: np.ndarray,
    row_windows: np.ndarray,
    fit_end: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Select directions stable across fit-only chronological subepochs."""
    window_chunks = [
        np.asarray(chunk, dtype=np.int16)
        for chunk in np.array_split(np.arange(fit_end), FIT_SUBEPOCHS)
    ]
    if any(len(chunk) == 0 for chunk in window_chunks):
        raise ValueError("fit history cannot form four nonempty subepochs")
    masks = [np.isin(row_windows, chunk) for chunk in window_chunks]
    records = []
    selected = []
    for column, name in enumerate(actors.FEATURE_NAMES):
        raw_aucs = [
            transport.auc_or_chance(profitable[mask], features[mask, column])
            for mask in masks
        ]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        keep = all(auc >= AUC_FLOOR for auc in oriented)
        if keep:
            selected.append(column)
        records.append(
            {
                "feature": name,
                "direction": (
                    "higher_is_positive" if direction == 1 else "lower_is_positive"
                ),
                "raw_auc_by_fit_subepoch": raw_aucs,
                "oriented_auc_by_fit_subepoch": oriented,
                "minimum_oriented_auc": min(oriented),
                "selected": keep,
            }
        )
    if not selected:
        raise ValueError("fit-only transport filter selected no features")
    return np.asarray(selected, dtype=np.int16), {
        "fit_window_chunks": [chunk.tolist() for chunk in window_chunks],
        "selected_feature_count": len(selected),
        "selected_features": [actors.FEATURE_NAMES[index] for index in selected],
        "features": records,
    }


def requirements_for(
    summary: dict[str, Any], folds: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, bool]:
    return {
        "minimum_closed_trades": summary["trades"] >= 300,
        "minimum_win_rate": summary["win_rate"] >= 0.65,
        "minimum_wilson_bound": summary["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": summary["net_pnl_sol"] > 0,
        "minimum_profit_factor": summary["profit_factor"] >= 1.25,
        "every_fold_minimum_trades": all(row["trades"] >= 50 for row in folds),
        "every_fold_minimum_win_rate": all(row["win_rate"] >= 0.55 for row in folds),
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
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    features = np.asarray([row.features for row in rows], dtype=np.float32)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("feature matrix shape changed")
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    profitable = pnl > 0
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
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]
        selected, selection_audit = nested_transport_selection(
            features, profitable, row_windows, fit_end
        )
        model = HistGradientBoostingClassifier(
            **MODEL_PARAMETERS,
            early_stopping=False,
            random_state=(
                RANDOM_SEED_BASE + fold_number * 100 + conformal.POLICY_INDEX
            ),
        )
        model.fit(features[fit][:, selected], profitable[fit])
        calibration_scores = model.predict_proba(
            features[calibration][:, selected]
        )[:, 1]
        test_scores = model.predict_proba(features[test][:, selected])[:, 1]
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
                "selection_audit": selection_audit,
            }
        )
        folds.append(result)

    summary = common.aggregate(folds)
    requirements = requirements_for(summary, folds, metadata)
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
        "model_family": "nested_chronological_transport_filtered_histogram_boosting",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "fit_subepochs": FIT_SUBEPOCHS,
            "fit_only_oriented_auc_floor": AUC_FLOOR,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "within each fold, orient features on the first fit-only subepoch, "
            "retain only features with oriented AUC >=0.52 in all four fit-only "
            "subepochs, then accept scores above the unchanged causal rolling "
            "99.73rd percentile"
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
            "A transport diagnostic found a stable causal feature core despite "
            "base-rate decay. This experiment nests feature transport selection "
            "inside every fit split so calibration and test labels remain unseen."
        ),
        "label_audit": metadata,
        "aggregate": summary,
        "folds": [
            common.compact_fold(fold)
            | {"selection_audit": fold["selection_audit"]}
            for fold in folds
        ],
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "VALIDATION_COLLAPSE",
        "failure_reason": (
            None
            if passed
            else "Nested fit-only transport filtering failed at least one frozen "
            f"gate: {summary['wins']}/{summary['trades']} wins "
            f"({summary['win_rate']:.2%}), Wilson "
            f"{summary['wilson_95_lower_bound']:.2%}, final-fold WR "
            f"{folds[-1]['win_rate']:.2%}."
        ),
        "material_change_required_before_rerun": (
            "Freeze this exact identity and collect a new strictly later "
            "untouched holdout; no development retuning is permitted."
            if passed
            else "A genuinely new causal feature or model family; AUC-floor, "
            "subepoch-count, threshold, and model-parameter variants are prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "feature_direction_uses_fit_only": True,
            "feature_selection_uses_fit_only": True,
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
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
