#!/usr/bin/env python3
"""Diagnose causal cross-launch market-flow features at exact 300 ms latency."""

from __future__ import annotations

import hashlib
import json
import pickle
from collections import deque
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp
from sklearn.metrics import roc_auc_score

OUTPUT = Path("artifacts/e4-v12-latency300-market-flow-transport.json")
SCHEMA_VERSION = "e4-v12-latency300-market-flow-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-cross-launch-market-flow-v12"
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
LOOKBACK_MS = (1_000, 5_000, 30_000, 120_000)
SOURCE_FIELDS = (
    "public_buy_sol",
    "price_multiple_from_create",
    "creator_seed_sol",
    "buy_count",
    "mayhem_mode",
)
FLOW_FEATURE_NAMES = (
    "market_prior_launches_1s",
    "market_prior_launches_5s",
    "market_prior_launches_30s",
    "market_prior_launches_120s",
    "market_interarrival_ms",
    "market_prior_public_buy_mean_5s",
    "market_prior_public_buy_mean_30s",
    "market_prior_price_multiple_mean_5s",
    "market_prior_price_multiple_mean_30s",
    "market_prior_price_multiple_max_30s",
    "market_prior_creator_seed_mean_30s",
    "market_prior_buy_count_mean_5s",
    "market_prior_buy_count_mean_30s",
    "market_prior_mayhem_fraction_30s",
    "market_current_public_buy_share_30s",
    "market_current_price_vs_prior_mean_30s",
    "market_current_seed_vs_prior_mean_30s",
    "market_launch_intensity_ratio_5s_120s",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_market_flow_transport.py",
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


def auc_or_chance(y: np.ndarray, values: np.ndarray) -> float:
    if len(np.unique(y)) < 2 or len(np.unique(values)) < 2:
        return 0.5
    return float(roc_auc_score(y, values))


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def causal_market_flow_matrix(
    decisions_ns: np.ndarray, base_features: np.ndarray
) -> np.ndarray:
    """Build features from strictly earlier decision snapshots only.

    Rows sharing a decision timestamp are evaluated as one batch, so their values
    cannot leak later sort-key order into one another.
    """
    if len(decisions_ns) != len(base_features):
        raise ValueError("decision and feature counts differ")
    indexes = {name: actors.FEATURE_NAMES.index(name) for name in SOURCE_FIELDS}
    result = np.zeros((len(decisions_ns), len(FLOW_FEATURE_NAMES)), dtype=np.float64)
    order = np.argsort(decisions_ns, kind="stable")
    history: deque[tuple[int, int]] = deque()
    prior_decision_ns: int | None = None
    cursor = 0
    while cursor < len(order):
        decision_ns = int(decisions_ns[order[cursor]])
        end = cursor + 1
        while end < len(order) and int(decisions_ns[order[end]]) == decision_ns:
            end += 1
        cutoff_ns = decision_ns - LOOKBACK_MS[-1] * 1_000_000
        while history and history[0][0] < cutoff_ns:
            history.popleft()
        prior = list(history)
        by_window: dict[int, list[int]] = {}
        for duration_ms in LOOKBACK_MS:
            threshold = decision_ns - duration_ms * 1_000_000
            by_window[duration_ms] = [index for timestamp, index in prior if timestamp >= threshold]
        five = by_window[5_000]
        thirty = by_window[30_000]

        def mean(indices: list[int], field: str) -> float:
            if not indices:
                return 0.0
            return float(np.mean(base_features[indices, indexes[field]]))

        prior_public_5 = mean(five, "public_buy_sol")
        prior_public_30 = mean(thirty, "public_buy_sol")
        prior_price_5 = mean(five, "price_multiple_from_create")
        prior_price_30 = mean(thirty, "price_multiple_from_create")
        prior_seed_30 = mean(thirty, "creator_seed_sol")
        prior_buys_5 = mean(five, "buy_count")
        prior_buys_30 = mean(thirty, "buy_count")
        prior_mayhem_30 = mean(thirty, "mayhem_mode")
        prior_price_max_30 = (
            float(np.max(base_features[thirty, indexes["price_multiple_from_create"]]))
            if thirty
            else 0.0
        )
        prior_public_sum_30 = (
            float(np.sum(base_features[thirty, indexes["public_buy_sol"]]))
            if thirty
            else 0.0
        )
        interarrival_ms = (
            min((decision_ns - prior_decision_ns) / 1_000_000, LOOKBACK_MS[-1])
            if prior_decision_ns is not None
            else float(LOOKBACK_MS[-1])
        )
        intensity_ratio = _safe_ratio(
            len(five) / 5.0,
            len(by_window[120_000]) / 120.0,
        )
        for position in range(cursor, end):
            row_index = int(order[position])
            current_public = float(base_features[row_index, indexes["public_buy_sol"]])
            current_price = float(
                base_features[row_index, indexes["price_multiple_from_create"]]
            )
            current_seed = float(base_features[row_index, indexes["creator_seed_sol"]])
            result[row_index] = (
                len(by_window[1_000]),
                len(five),
                len(thirty),
                len(by_window[120_000]),
                interarrival_ms,
                prior_public_5,
                prior_public_30,
                prior_price_5,
                prior_price_30,
                prior_price_max_30,
                prior_seed_30,
                prior_buys_5,
                prior_buys_30,
                prior_mayhem_30,
                _safe_ratio(current_public, prior_public_sum_30 + current_public),
                _safe_ratio(current_price, prior_price_30),
                _safe_ratio(current_seed, prior_seed_30),
                intensity_ratio,
            )
        for position in range(cursor, end):
            row_index = int(order[position])
            history.append((decision_ns, row_index))
        prior_decision_ns = decision_ns
        cursor = end
    return result


def main() -> dict[str, Any]:
    metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if metadata["all_source_hashes_verified"] is not True:
        raise ValueError("source hashes were not verified")
    if metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    base_features = np.asarray([row.features for row in rows], dtype=np.float64)
    decisions_ns = np.asarray([row.decision_ns for row in rows], dtype=np.int64)
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    if base_features.shape != (len(rows), len(actors.FEATURE_NAMES)):
        raise ValueError("base feature matrix shape changed")
    if len(pnl) != len(rows) or not bool(np.all(np.isfinite(pnl))):
        raise ValueError("300 ms labels are incomplete")
    flow = causal_market_flow_matrix(decisions_ns, base_features)
    if not bool(np.all(np.isfinite(flow))):
        raise ValueError("market-flow matrix contains non-finite values")
    windows = sorted({row.run_id for row in rows}, key=int)
    if len(windows) != 4 * WINDOWS_PER_EPOCH:
        raise ValueError("diagnostic requires exactly four 16-window epochs")
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray([window_index[row.run_id] for row in rows], dtype=np.int16)
    positive = pnl > 0
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    epochs = [
        {
            "epoch": epoch,
            "windows": windows[epoch * WINDOWS_PER_EPOCH : (epoch + 1) * WINDOWS_PER_EPOCH],
            "rows": int(mask.sum()),
            "profitable_rows": int(positive[mask].sum()),
            "positive_rate": float(np.mean(positive[mask])),
        }
        for epoch, mask in enumerate(epoch_masks)
    ]
    records: list[dict[str, Any]] = []
    for column, name in enumerate(FLOW_FEATURE_NAMES):
        raw_aucs = [auc_or_chance(positive[mask], flow[mask, column]) for mask in epoch_masks]
        direction = 1 if raw_aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in raw_aucs]
        statistic, pvalue = ks_2samp(flow[epoch_masks[0], column], flow[epoch_masks[-1], column])
        records.append(
            {
                "feature": name,
                "earliest_epoch_direction": "higher_is_positive" if direction == 1 else "lower_is_positive",
                "raw_auc_by_epoch": raw_aucs,
                "oriented_auc_by_epoch": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_epochs": all(auc >= 0.5 for auc in oriented),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
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
    stable = [row["feature"] for row in records if row["all_epochs_above_descriptive_floor"]]
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(FLOW_FEATURE_NAMES),
        "label_matrix_sha256": metadata["pnl_sha256"],
        "latency_ms": labels.LATENCY_MS,
        "lookback_ms": LOOKBACK_MS,
        "same_timestamp_policy": "batch before insertion",
        "orientation_policy": "orient on epoch 0 once and freeze for epochs 1-3",
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
    }
    warranted = len(stable) > 0
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "scientific_basis": (
            "Cross-launch congestion and competing public flow are observable at decision time "
            "and may explain why otherwise similar launches lose post-entry opportunity at 300 ms."
        ),
        "label_audit": metadata,
        "epochs": epochs,
        "flow_feature_audit": {
            "rows": len(rows),
            "features": len(FLOW_FEATURE_NAMES),
            "feature_names": list(FLOW_FEATURE_NAMES),
            "matrix_sha256": hashlib.sha256(flow.astype("<f8", copy=False).tobytes()).hexdigest(),
            "strictly_earlier_decision_snapshots_only": True,
            "same_timestamp_rows_excluded_from_one_another": True,
            "outcomes_used_as_features": False,
            "active_untouched_live_data_used": False,
            "production_paths_changed": 0,
        },
        "features": records,
        "stable_feature_count": len(stable),
        "stable_features": stable,
        "finding": (
            f"{len(stable)} of {len(FLOW_FEATURE_NAMES)} causal cross-launch market-flow "
            "features preserved epoch-0 direction with oriented AUC >= 0.52 in all four epochs."
        ),
        "candidate_warranted": warranted,
        "retired_family": not warranted,
        "failure_classification": None if warranted else "VALIDATION_COLLAPSE",
        "material_change_required_before_rerun": (
            "A preregistered candidate using only the transported market-flow features."
            if warranted
            else "A new causal risk-set feature family; market-flow lookback or threshold retuning is prohibited."
        ),
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
