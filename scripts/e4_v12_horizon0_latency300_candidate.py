#!/usr/bin/env python3
"""Evaluate one frozen creation-time candidate with exact 300 ms fills."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_horizon0_latency300_transport as transport
import e4_v12_latency300_conformal as common
import e4_v12_latency300_market_flow_candidate as flow_candidate
import e4_v12_latency300_market_flow_transport as market
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

OUTPUT = Path("artifacts/e4-v12-horizon0-latency300-candidate.json")
DISCOVERY_PATH = Path("artifacts/e4-v12-horizon0-latency300-transport.json")
BASELINE_PATH = Path("artifacts/e4-v12-latency300-conformal-failure.json")
SCHEMA_VERSION = "e4-v12-horizon0-latency300-candidate-v1"
THESIS_FAMILY = "horizon0-causal-transport-v12"
STABLE_FEATURE_NAMES = (
    "creator_severe_loss_rate",
    "mayhem_mode",
    "creator_resolved_launches",
    "creator_prior_launch_count",
    "metadata_content_addressed",
    "creator_profitable_history",
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
RANDOM_SEED_BASE = 172_120
SOURCE_FILES = (
    "scripts/e4_v12_horizon0_latency300_candidate.py",
    "scripts/e4_v12_horizon0_latency300_transport.py",
    "scripts/e4_v12_latency300_market_flow_transport.py",
    "scripts/e4_v12_latency300_market_flow_candidate.py",
    "scripts/e4_v12_latency300_conformal.py",
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


def full_feature_matrix(
    rows: list[base.ResearchRow],
) -> tuple[np.ndarray, tuple[str, ...]]:
    base_features = np.asarray([row.features for row in rows], dtype=np.float64)
    if base_features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("creation-time causal feature matrix shape changed")
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    flow = market.causal_market_flow_matrix(decisions, base_features)
    return (
        np.concatenate((base_features, flow), axis=1),
        (*actors.FEATURE_NAMES, *market.FLOW_FEATURE_NAMES),
    )


def main() -> dict[str, Any]:
    discovery = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    with transport.CACHE.open("rb") as handle:
        corpus = pickle.load(handle)
    if corpus["version"] != transport.CACHE_VERSION:
        raise ValueError("creation-time cache schema changed")
    if (
        corpus["builder_fingerprint"]
        != discovery["identity"]["source_code_fingerprint"]
    ):
        raise ValueError("creation-time cache was not built by the frozen diagnostic")
    if (
        corpus["manifest_sha256"]
        != discovery["identity"]["dataset_source_manifest_fingerprint"]
    ):
        raise ValueError("creation-time cache targets another evidence manifest")
    if tuple(discovery["stable_features"]) != STABLE_FEATURE_NAMES:
        raise ValueError("frozen transported feature set changed")
    if discovery["candidate_warranted"] is not True:
        raise ValueError("creation-time transport did not warrant a candidate")
    audit = corpus["audit"]
    if audit["raw_source_hashes_verified"] is not True:
        raise ValueError("creation-time source hashes were not verified")
    if audit["same_timestamp_creator_counts_batched"] is not True:
        raise ValueError("equal-timestamp creator counts were not batched")
    if audit["same_timestamp_market_context_batched"] is not True:
        raise ValueError("equal-timestamp market context was not batched")
    if audit["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")

    rows = corpus["rows"]
    pnl = np.asarray(corpus["pnl"], dtype=np.float64)
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("creation-time exact-300ms labels are incomplete")
    pnl_sha = hashlib.sha256(pnl.astype("<f8", copy=False).tobytes()).hexdigest()
    if pnl_sha != discovery["label_audit"]["pnl_sha256"]:
        raise ValueError("creation-time label fingerprint changed")
    full_features, feature_names = full_feature_matrix(rows)
    full_matrix_sha = hashlib.sha256(
        full_features.astype("<f8", copy=False).tobytes()
    ).hexdigest()
    if full_matrix_sha != discovery["feature_audit"]["matrix_sha256"]:
        raise ValueError("creation-time feature fingerprint changed")
    feature_columns = [feature_names.index(name) for name in STABLE_FEATURE_NAMES]
    features = full_features[:, feature_columns].astype(np.float32)
    decisions = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    windows = sorted({row.run_id for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )

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
                RANDOM_SEED_BASE
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
    requirements = flow_candidate.requirements_for(summary, folds, {"no_quote_rows": 0})
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
        "dataset_source_manifest_fingerprint": corpus["manifest_sha256"],
        "evidence_epoch": windows,
        "discovery_diagnostic_id": discovery["diagnostic_id"],
        "feature_set_fingerprint": base.stable_hash(STABLE_FEATURE_NAMES),
        "model_family": "histogram_boosting_on_creation_time_transport",
        "full_parameters": {
            "model": MODEL_PARAMETERS,
            "random_seed_base": RANDOM_SEED_BASE,
            "stable_features": STABLE_FEATURE_NAMES,
            "score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_sha256": pnl_sha,
        },
        "causal_horizon_ms": transport.HORIZON_MS,
        "candidate_risk_set_policy": (
            "use only the ten creation-time features frozen by four-epoch "
            "transport; accept above the unchanged causal rolling 99.73rd "
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
        "latency_assumptions_ms": [transport.LATENCY_MS],
        "execution_policy": (
            "paper replay at exact 300 ms entry delay; compound 1.85% of "
            "current bankroll; maximum two concurrent positions"
        ),
        "exit_policy": conformal.adaptive.POLICIES[conformal.POLICY_INDEX].key,
    }
    failed = [name for name, value in requirements.items() if not value]
    failure_reason = None
    if failed:
        failure_reason = (
            f"Creation-time transport failed {', '.join(failed)}: "
            f"{summary['wins']}/{summary['trades']} wins "
            f"({summary['win_rate']:.2%}), Wilson "
            f"{summary['wilson_95_lower_bound']:.2%}, net "
            f"{summary['net_pnl_sol']:+.6f} SOL, PF "
            f"{summary['profit_factor']:.3f}."
        )
    output = {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "thesis_family": THESIS_FAMILY,
        "identity": identity,
        "scientific_basis": discovery["scientific_basis"],
        "corpus_audit": audit,
        "label_audit": discovery["label_audit"],
        "feature_audit": {
            "selected_features": list(STABLE_FEATURE_NAMES),
            "selected_feature_count": len(STABLE_FEATURE_NAMES),
            "full_matrix_sha256": full_matrix_sha,
            "selected_matrix_sha256": hashlib.sha256(
                features.astype("<f4", copy=False).tobytes()
            ).hexdigest(),
            "strict_creation_event_only": True,
            "same_timestamp_creator_counts_batched": True,
            "same_timestamp_market_context_batched": True,
            "active_untouched_live_data_used": False,
        },
        "baseline_comparison": {
            "baseline_experiment_id": baseline["experiment_id"],
            "baseline_decision_horizon_ms": baseline["identity"][
                "causal_horizon_ms"
            ],
            "candidate_minus_baseline_trades": (
                summary["trades"] - baseline["aggregate"]["trades"]
            ),
            "candidate_minus_baseline_win_rate": (
                summary["win_rate"] - baseline["aggregate"]["win_rate"]
            ),
            "candidate_minus_baseline_net_pnl_sol": (
                summary["net_pnl_sol"]
                - baseline["aggregate"]["net_pnl_sol"]
            ),
            "candidate_minus_baseline_profit_factor": (
                summary["profit_factor"]
                - baseline["aggregate"]["profit_factor"]
            ),
        },
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "VALIDATION_COLLAPSE",
        "failure_reason": failure_reason,
        "material_change_required_before_rerun": (
            "Freeze this identity and collect a new strictly later untouched "
            "holdout; no development retuning."
            if passed
            else "A genuinely new causal feature or model family; creation-time "
            "feature subsets, threshold variants, and model-parameter variants "
            "are prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "fit_precedes_calibration": True,
            "calibration_precedes_test": True,
            "strict_creation_event_only": True,
            "creator_outcomes_delayed_ms": transport.RESOLUTION_DELAY_MS,
            "equal_timestamp_creator_counts_batched": True,
            "equal_timestamp_market_context_batched": True,
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
