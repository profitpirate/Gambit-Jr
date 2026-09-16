#!/usr/bin/env python3
"""Evaluate a fit-only empirical-rank price-path ensemble at 300 ms."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_latency300_conformal as common
import e4_v12_latency300_distributed_discovery_candidate as distributed
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_latency300_microstructure_transport as micro
import e4_v12_latency300_nested_transport as nested
import e4_v12_latency300_price_path_transport as path
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np

OUTPUT = Path("artifacts/e4-v12-latency300-path-rank-candidate.json")
SCHEMA_VERSION = "e4-v12-latency300-path-rank-candidate-v1"
THESIS_FAMILY = "latency300-causal-path-empirical-rank-v12"
FEATURE_NAMES = path.PRICE_PATH_FEATURE_NAMES
SOURCE_FILES = (
    "scripts/e4_v12_latency300_path_rank_candidate.py",
    "scripts/e4_v12_latency300_distributed_discovery_candidate.py",
    "scripts/e4_v12_latency300_price_path_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
    "scripts/e4_v12_latency300_nested_transport.py",
    "scripts/e4_v12_latency300_conformal.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
    "scripts/e4_v12_adaptive_exit_search.py",
    "scripts/e4_v12_profit_survival_search.py",
)


def sha256_lf(path_value: Path) -> str:
    return __import__("hashlib").sha256(
        path_value.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def source_code_fingerprint(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else root
    return base.stable_hash(
        {relative: sha256_lf(root / relative) for relative in SOURCE_FILES}
    )


def empirical_rank_scores(
    reference: np.ndarray,
    candidates: np.ndarray,
    directions: np.ndarray,
) -> np.ndarray:
    if reference.ndim != 2 or candidates.ndim != 2:
        raise ValueError("rank matrices must be two-dimensional")
    if reference.shape[1] != candidates.shape[1] or len(directions) != reference.shape[1]:
        raise ValueError("rank feature dimensions differ")
    if len(reference) == 0:
        raise ValueError("empirical rank requires a nonempty reference")
    oriented = np.empty_like(candidates, dtype=np.float64)
    for column, direction in enumerate(directions):
        sorted_reference = np.sort(reference[:, column], kind="stable")
        ranks = np.searchsorted(
            sorted_reference, candidates[:, column], side="right"
        ).astype(np.float64) / len(sorted_reference)
        oriented[:, column] = ranks if direction == 1 else 1.0 - ranks
    return np.mean(oriented, axis=1)


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    path_metadata = json.loads(path.CACHE_METADATA.read_text(encoding="utf-8"))
    micro_metadata = json.loads(micro.CACHE_METADATA.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    for name, cache_metadata, cache_path in (
        ("price-path", path_metadata, path.CACHE),
        ("microstructure", micro_metadata, micro.CACHE),
    ):
        if cache_metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
            raise ValueError(f"{name} cache targets another evidence manifest")
        if cache_metadata["matrix_sha256"] != base.sha256_path(cache_path):
            raise ValueError(f"{name} matrix fingerprint changed")
        if cache_metadata["raw_source_hashes_verified"] is not True:
            raise ValueError(f"{name} source hashes were not verified")
        if cache_metadata["active_untouched_live_data_used"] is not False:
            raise ValueError(f"{name} features consumed active live evidence")
    rows = payload["rows"]
    features = np.load(path.CACHE, allow_pickle=False)
    microstructure = np.load(micro.CACHE, allow_pickle=False)
    price_range = microstructure[
        :, micro.MICRO_FEATURE_NAMES.index("micro_log_price_range_250ms")
    ]
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if features.shape != (len(rows), len(FEATURE_NAMES)):
        raise ValueError("price-path feature matrix shape changed")
    if not bool(np.all(np.isfinite(features))) or not bool(
        np.all(np.isfinite(price_range)) and np.all(np.isfinite(pnl))
    ):
        raise ValueError("candidate features or labels are incomplete")
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
        fit = fit[np.argsort(decisions[fit], kind="stable")]
        calibration = calibration[np.argsort(decisions[calibration], kind="stable")]
        test = test[np.argsort(decisions[test], kind="stable")]

        fit_gate_threshold = float(
            np.quantile(
                price_range[fit], 1.0 - distributed.RANGE_GATE_FRACTION
            )
        )
        fit_gated = fit[price_range[fit] >= fit_gate_threshold]
        calibration_gate_mask, calibration_gate_thresholds = (
            distributed.rolling_selection(
                price_range[fit],
                price_range[calibration],
                distributed.RANGE_GATE_FRACTION,
            )
        )
        test_gate_mask, test_gate_thresholds = distributed.rolling_selection(
            price_range[calibration],
            price_range[test],
            distributed.RANGE_GATE_FRACTION,
        )
        calibration_gated = calibration[calibration_gate_mask]
        test_gated = test[test_gate_mask]
        if len(fit_gated) == 0 or len(calibration_gated) == 0 or len(test_gated) == 0:
            raise ValueError("price-range gate produced an empty chronological split")

        raw_aucs = np.asarray(
            [
                transport.auc_or_chance(
                    profitable[fit_gated], features[fit_gated, column]
                )
                for column in range(features.shape[1])
            ],
            dtype=np.float64,
        )
        directions = np.where(raw_aucs >= 0.5, 1, -1).astype(np.int8)
        calibration_scores = empirical_rank_scores(
            features[fit_gated], features[calibration_gated], directions
        )
        test_scores = empirical_rank_scores(
            features[fit_gated], features[test_gated], directions
        )
        conditional_mask, conditional_thresholds = distributed.rolling_selection(
            calibration_scores,
            test_scores,
            distributed.CONDITIONAL_SCORE_FRACTION,
        )
        selected = test_gated[conditional_mask]
        result = conformal.replay(rows, selected, pnl, test_scores[conditional_mask])
        result.update(
            {
                "fold": fold_number,
                "fit_windows": windows[:fit_end],
                "calibration_windows": windows[fit_end:test_start],
                "test_windows": windows[test_start : test_start + test_size],
                "fit_gate_threshold": fit_gate_threshold,
                "fit_gate_rows": len(fit_gated),
                "calibration_gate_rows": len(calibration_gated),
                "test_gate_rows": len(test_gated),
                "fit_only_feature_directions": {
                    name: "higher_is_positive" if direction == 1 else "lower_is_positive"
                    for name, direction in zip(FEATURE_NAMES, directions, strict=True)
                },
                "fit_only_raw_auc": {
                    name: float(auc)
                    for name, auc in zip(FEATURE_NAMES, raw_aucs, strict=True)
                },
                "calibration_gate_threshold_hash": base.stable_hash(
                    calibration_gate_thresholds
                ),
                "test_gate_threshold_hash": base.stable_hash(test_gate_thresholds),
                "conditional_threshold_minimum": min(conditional_thresholds),
                "conditional_threshold_maximum": max(conditional_thresholds),
                "conditional_threshold_hash": base.stable_hash(
                    conditional_thresholds
                ),
            }
        )
        folds.append(result)

    summary = common.aggregate(folds)
    requirements = nested.requirements_for(summary, folds, metadata)
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
        "feature_set_fingerprint": base.stable_hash(FEATURE_NAMES),
        "price_path_matrix_sha256": path_metadata["matrix_sha256"],
        "microstructure_matrix_sha256": micro_metadata["matrix_sha256"],
        "model_family": "fit_only_equal_weight_empirical_copula_rank_ensemble",
        "full_parameters": {
            "feature_weighting": "equal_weight_all_sixteen_features",
            "feature_direction": "fit_gate_labels_only",
            "rank_reference": "fit_gate_features_only",
            "range_gate_fraction": distributed.RANGE_GATE_FRACTION,
            "conditional_score_fraction": distributed.CONDITIONAL_SCORE_FRACTION,
            "implied_total_score_fraction": conformal.SCORE_FRACTION,
            "rolling_history_rows": conformal.HISTORY_ROWS,
            "threshold_refresh_rows": conformal.REFRESH_ROWS,
            "adaptive_exit_policy_index": conformal.POLICY_INDEX,
            "label_matrix_sha256": metadata["pnl_sha256"],
        },
        "causal_horizon_ms": path.HORIZON_MS,
        "candidate_risk_set_policy": (
            "causal rolling top-five-percent price-range gate followed by an "
            "equal-weight empirical percentile average over all sixteen path "
            "features; directions and reference distributions use fit history "
            "only; causal rolling conditional guard preserves 0.27 percent output"
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
            "range_gate_fraction": distributed.RANGE_GATE_FRACTION,
            "conditional_score_fraction": distributed.CONDITIONAL_SCORE_FRACTION,
            "thresholds_use_prior_scores_only": True,
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
            "The flexible two-stage booster collapsed chronologically despite "
            "stable conditional path signal. This prespecified empirical-rank "
            "family removes interaction fitting and parameter optimization."
        ),
        "label_audit": metadata,
        "price_path_cache_audit": path_metadata,
        "microstructure_cache_audit": micro_metadata,
        "aggregate": summary,
        "folds": [common.compact_fold(fold) for fold in folds],
        "requirements": requirements,
        "development_gate_passed": passed,
        "retired": not passed,
        "failure_classification": None if passed else "VALIDATION_COLLAPSE",
        "failure_reason": (
            None
            if passed
            else "Empirical path ranking failed at least one frozen gate: "
            f"{summary['wins']}/{summary['trades']} wins "
            f"({summary['win_rate']:.2%}), Wilson "
            f"{summary['wilson_95_lower_bound']:.2%}, final-fold WR "
            f"{folds[-1]['win_rate']:.2%}."
        ),
        "material_change_required_before_rerun": (
            "Freeze this exact identity and collect a new strictly later "
            "untouched holdout; no development retuning is permitted."
            if passed
            else "A genuinely new causal feature or model family; rank weighting, "
            "direction, feature-subset, range-gate, threshold, and fraction "
            "variants are prohibited."
        ),
        "ready_for_new_strictly_later_holdout": passed,
        "anti_lookahead": {
            "price_path_events_at_or_before_250ms_only": True,
            "fit_gate_uses_fit_only": True,
            "feature_directions_use_fit_labels_only": True,
            "rank_reference_uses_fit_features_only": True,
            "calibration_precedes_test": True,
            "conditional_threshold_uses_prior_scores_only": True,
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
