#!/usr/bin/env python3
"""Evaluate the frozen cross-launch market-flow candidate at exact 300 ms."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_conformal as common
import e4_v12_latency300_labels as labels
import e4_v12_latency300_market_flow_transport as transport
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-latency300-market-flow-candidate.json")
DISCOVERY_PATH = Path("artifacts/e4-v12-latency300-market-flow-transport.json")
BASELINE_PATH = Path("artifacts/e4-v12-latency300-conformal-failure.json")
SCHEMA_VERSION = "e4-v12-latency300-market-flow-candidate-v1"
THESIS_FAMILY = "latency300-cross-launch-market-flow-v12"
STABLE_FLOW_FEATURE_NAMES = (
    "market_current_seed_vs_prior_mean_30s",
    "market_current_public_buy_share_30s",
    "market_current_price_vs_prior_mean_30s",
    "market_interarrival_ms",
    "market_prior_launches_1s",
    "market_launch_intensity_ratio_5s_120s",
    "market_prior_launches_5s",
)
MODEL_PARAMETERS = {
    "learning_rate": 0.05,
    "max_iter": 100,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 100,
    "l2_regularization": 2.0,
    "class_weight": "balanced",
}
RANDOM_SEED_BASE = 152_120
SOURCE_FILES = (
    "scripts/e4_v12_latency300_market_flow_candidate.py",
    "scripts/e4_v12_latency300_market_flow_transport.py",
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
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
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
    if tuple(discovery["stable_features"]) != STABLE_FLOW_FEATURE_NAMES:
        raise ValueError("frozen transported feature set changed")
    if discovery["candidate_warranted"] is not True:
        raise ValueError("market-flow transport did not warrant a candidate")
    rows = payload["rows"]
    base_features = np.asarray([row.features for row in rows], dtype=np.float64)
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    full_flow = transport.causal_market_flow_matrix(decisions, base_features)
    flow_columns = [
        transport.FLOW_FEATURE_NAMES.index(name) for name in STABLE_FLOW_FEATURE_NAMES
    ]
    stable_flow = full_flow[:, flow_columns]
    features = np.concatenate((base_features, stable_flow), axis=1).astype(np.float32)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
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
        model = HistGradientBoostingClassifier(
            **MODEL_PARAMETERS,
            early_stopping=False,
            random_state=RANDOM_SEED_BASE + fold_number * 100 + conformal.POLICY_INDEX,
        )
        model.fit(features[fit], pnl[fit] > 0)
        calibration_scores = model.predict_proba(features[calibration])[:, 1]
        test_scores = model.predict_proba(features[test])[:, 1]
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
        "discovery_diagnostic_id": discovery["diagnostic_id"],
        "feature_set_fingerprint": base.stable_hash(
            (*actors.FEATURE_NAMES, *STABLE_FLOW_FEATURE_NAMES)
        ),
        "model_family": "histogram_boosting_with_causal_cross_launch_market_flow",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "flow_lookback_ms": transport.LOOKBACK_MS,
            "stable_flow_features": STABLE_FLOW_FEATURE_NAMES,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": 250,
        "candidate_risk_set_policy": (
            "augment the established causal decision snapshot with the seven frozen "
            "cross-launch features that transported across all four epochs; accept "
            "above the unchanged rolling 99.73rd percentile of prior scores"
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
    failure_reason = None
    if not passed:
        failed = [name for name, value in requirements.items() if not value]
        failure_reason = (
            f"Cross-launch market flow failed {', '.join(failed)}: "
            f"{summary['wins']}/{summary['trades']} wins ({summary['win_rate']:.2%}), "
            f"Wilson {summary['wilson_95_lower_bound']:.2%}, net "
            f"{summary['net_pnl_sol']:+.6f} SOL, PF {summary['profit_factor']:.3f}."
        )
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "scientific_basis": discovery["scientific_basis"],
        "label_audit": metadata,
        "feature_audit": {
            "base_features": len(actors.FEATURE_NAMES),
            "added_features": list(STABLE_FLOW_FEATURE_NAMES),
            "added_feature_count": len(STABLE_FLOW_FEATURE_NAMES),
            "flow_matrix_sha256": discovery["flow_feature_audit"]["matrix_sha256"],
            "strictly_earlier_decision_snapshots_only": True,
            "same_timestamp_rows_excluded_from_one_another": True,
            "active_untouched_live_data_used": False,
        },
        "baseline_comparison": {
            "baseline_experiment_id": baseline["experiment_id"],
            "candidate_minus_baseline_trades": summary["trades"] - baseline["aggregate"]["trades"],
            "candidate_minus_baseline_win_rate": summary["win_rate"] - baseline["aggregate"]["win_rate"],
            "candidate_minus_baseline_net_pnl_sol": summary["net_pnl_sol"] - baseline["aggregate"]["net_pnl_sol"],
            "candidate_minus_baseline_profit_factor": summary["profit_factor"] - baseline["aggregate"]["profit_factor"],
        },
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "VALIDATION_COLLAPSE",
        "failure_reason": failure_reason,
        "material_change_required_before_rerun": (
            "Freeze this identity and collect a new strictly later untouched holdout; no development retuning."
            if passed
            else "A genuinely new causal feature or model family; market-flow lookback, subset, threshold, and model-parameter variants are prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "market_flow_uses_strictly_earlier_decision_snapshots": True,
            "equal_timestamp_launches_are_batched": True,
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
