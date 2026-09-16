#!/usr/bin/env python3
"""Audit causal anchor-and-crowd price-discovery interaction features."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_latency300_microstructure_transport as micro
import e4_v12_latency300_participant_breadth_transport as participants
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp

OUTPUT = Path("artifacts/e4-v12-latency300-anchor-crowd-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-anchor-crowd-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-anchor-crowd-transport-v12"
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
FEATURE_NAMES = (
    "anchor_crowd_range_x_effective_count",
    "anchor_crowd_range_x_flow_per_trader",
    "anchor_crowd_range_x_largest_share",
    "anchor_crowd_range_x_hhi",
    "anchor_crowd_range_x_entropy",
    "anchor_crowd_range_x_effective_count_x_largest_share",
    "anchor_crowd_range_x_effective_count_x_hhi",
    "anchor_crowd_range_x_flow_per_trader_x_largest_share",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_anchor_crowd_transport.py",
    "scripts/e4_v12_latency300_participant_breadth_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
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


def interaction_matrix(
    micro_matrix: np.ndarray, participant_matrix: np.ndarray
) -> np.ndarray:
    micro_columns = {name: index for index, name in enumerate(micro.MICRO_FEATURE_NAMES)}
    participant_columns = {
        name: index for index, name in enumerate(participants.PARTICIPANT_FEATURE_NAMES)
    }
    price_range = micro_matrix[:, micro_columns["micro_log_price_range_250ms"]]
    effective_count = participant_matrix[
        :, participant_columns["participant_effective_count_250ms"]
    ]
    flow_per_trader = participant_matrix[
        :, participant_columns["participant_log_flow_per_trader_250ms"]
    ]
    largest_share = participant_matrix[
        :, participant_columns["participant_largest_flow_share_250ms"]
    ]
    hhi = participant_matrix[:, participant_columns["participant_flow_hhi_250ms"]]
    entropy = participant_matrix[
        :, participant_columns["participant_normalized_entropy_250ms"]
    ]
    log_effective_count = np.log1p(effective_count)
    matrix = np.column_stack(
        (
            price_range * log_effective_count,
            price_range * flow_per_trader,
            price_range * largest_share,
            price_range * hhi,
            price_range * entropy,
            price_range * log_effective_count * largest_share,
            price_range * log_effective_count * hhi,
            price_range * flow_per_trader * largest_share,
        )
    )
    if matrix.shape[1] != len(FEATURE_NAMES) or not bool(np.all(np.isfinite(matrix))):
        raise ValueError("anchor-and-crowd interaction matrix is invalid")
    return matrix


def main() -> dict[str, Any]:
    label_metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    micro_matrix, micro_audit = micro.load_or_build_matrix(Path.cwd(), payload)
    participant_matrix, participant_audit = participants.load_or_build_matrix(
        Path.cwd(), payload
    )
    if label_metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if label_metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if label_metadata["all_source_hashes_verified"] is not True:
        raise ValueError("label source hashes were not verified")
    if label_metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    if micro_audit["active_untouched_live_data_used"] is not False:
        raise ValueError("microstructure matrix consumed active live evidence")
    if participant_audit["active_untouched_live_data_used"] is not False:
        raise ValueError("participant matrix consumed active live evidence")
    rows = payload["rows"]
    matrix = interaction_matrix(micro_matrix, participant_matrix)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    profitable = pnl > 0
    windows = sorted({row.run_id for row in rows}, key=int)
    if len(windows) != 4 * WINDOWS_PER_EPOCH:
        raise ValueError("diagnostic requires four 16-window epochs")
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[row.run_id] for row in rows], dtype=np.int16
    )
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    price_range_column = micro.MICRO_FEATURE_NAMES.index(
        "micro_log_price_range_250ms"
    )
    benchmark_auc_by_epoch = [
        transport.auc_or_chance(profitable[mask], micro_matrix[mask, price_range_column])
        for mask in epoch_masks
    ]
    benchmark_minimum_later_auc = min(benchmark_auc_by_epoch[1:])
    benchmark_mean_later_auc = float(np.mean(benchmark_auc_by_epoch[1:]))
    records = []
    for column, name in enumerate(FEATURE_NAMES):
        raw_aucs = [
            transport.auc_or_chance(profitable[mask], matrix[mask, column])
            for mask in epoch_masks
        ]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        statistic, pvalue = ks_2samp(
            matrix[epoch_masks[0], column], matrix[epoch_masks[-1], column]
        )
        records.append(
            {
                "feature": name,
                "earliest_epoch_direction": (
                    "higher_is_positive" if direction == 1 else "lower_is_positive"
                ),
                "raw_auc_by_epoch": raw_aucs,
                "oriented_auc_by_epoch": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "minimum_later_auc_delta_vs_price_range": (
                    min(oriented[1:]) - benchmark_minimum_later_auc
                ),
                "mean_later_auc_delta_vs_price_range": (
                    float(np.mean(oriented[1:])) - benchmark_mean_later_auc
                ),
                "direction_consistent_all_epochs": all(auc >= 0.5 for auc in oriented),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
                "first_to_last_oriented_auc_change": oriented[-1] - oriented[0],
                "first_to_last_ks_statistic": float(statistic),
                "first_to_last_ks_pvalue": float(pvalue),
            }
        )
    records.sort(
        key=lambda row: (
            row["all_epochs_above_descriptive_floor"],
            row["minimum_later_oriented_auc"],
            row["mean_later_oriented_auc"],
        ),
        reverse=True,
    )
    stable = [
        row["feature"] for row in records if row["all_epochs_above_descriptive_floor"]
    ]
    improved = [
        row["feature"]
        for row in records
        if row["minimum_later_auc_delta_vs_price_range"] > 0
    ]
    matrix_sha256 = __import__("hashlib").sha256(matrix.tobytes(order="C")).hexdigest()
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(FEATURE_NAMES),
        "interaction_matrix_sha256": matrix_sha256,
        "microstructure_matrix_sha256": micro_audit["matrix_sha256"],
        "participant_matrix_sha256": participant_audit["matrix_sha256"],
        "label_matrix_sha256": label_metadata["pnl_sha256"],
        "causal_horizon_ms": participants.HORIZON_MS,
        "latency_ms": labels.LATENCY_MS,
        "orientation_policy": (
            "orient each fixed interaction once from epoch 0, then freeze "
            "direction for epochs 1-3"
        ),
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "features": records,
        "feature_count": len(records),
        "direction_consistent_features": sum(
            row["direction_consistent_all_epochs"] for row in records
        ),
        "all_epoch_auc_52_features": stable,
        "all_epoch_auc_52_feature_count": len(stable),
        "benchmark": {
            "feature": "micro_log_price_range_250ms",
            "auc_by_epoch": benchmark_auc_by_epoch,
            "minimum_later_auc": benchmark_minimum_later_auc,
            "mean_later_auc": benchmark_mean_later_auc,
        },
        "interactions_improving_minimum_later_auc": improved,
        "interactions_improving_minimum_later_auc_count": len(improved),
        "source_audit": {
            "rows": len(rows),
            "windows": len(windows),
            "raw_capture_bytes": participant_audit["raw_capture_bytes"],
            "raw_source_hashes_verified": True,
            "microstructure_matrix_sha256": micro_audit["matrix_sha256"],
            "participant_matrix_sha256": participant_audit["matrix_sha256"],
            "interaction_matrix_sha256": matrix_sha256,
            "future_values_in_features": False,
            "active_untouched_live_data_used": False,
        },
        "finding": (
            f"{len(stable)} of {len(records)} predeclared causal anchor-and-crowd "
            "interactions preserved epoch-0 direction with oriented AUC >=0.52 "
            f"in all four epochs, but only {len(improved)} improved the minimum "
            "later-era AUC of the strongest constituent price-range signal. This "
            "is a transport diagnostic, not a fitted candidate or approval."
        ),
        "candidate_warranted": bool(improved),
        "candidate_fitted": False,
        "development_gate_passed": False,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "active_untouched_live_data_used": False,
        "production_paths_changed": 0,
    }
    base.write_json(OUTPUT, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return output


if __name__ == "__main__":
    main()
