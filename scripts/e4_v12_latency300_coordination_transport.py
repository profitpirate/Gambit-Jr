#!/usr/bin/env python3
"""Build and audit causal sub-250 ms coordination-topology features."""

from __future__ import annotations

import json
import math
import os
import pickle
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_latency300_microstructure_transport as micro
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp

OUTPUT = Path("artifacts/e4-v12-latency300-coordination-transport.json")
CACHE = Path(".tmp-relative-price-capped-actor-holdout/latency300-coordination.npy")
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-coordination.json"
)
SCHEMA_VERSION = "e4-v12-latency300-coordination-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-coordination-topology-v12"
HORIZON_MS = 250
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
PARSE_WORKERS = 2
COORDINATION_FEATURE_NAMES = (
    "coord_unique_slots_250ms",
    "coord_unique_signatures_250ms",
    "coord_slot_span_250ms",
    "coord_largest_slot_trade_share_250ms",
    "coord_slot_hhi_250ms",
    "coord_slot_entropy_250ms",
    "coord_same_slot_trade_fraction_250ms",
    "coord_multi_trader_slot_fraction_250ms",
    "coord_median_traders_per_slot_250ms",
    "coord_max_traders_per_slot_250ms",
    "coord_first_trade_latency_ms",
    "coord_time_to_third_unique_trader_ms",
    "coord_interarrival_cv_250ms",
    "coord_interarrival_burstiness_250ms",
    "coord_zero_gap_fraction_250ms",
    "coord_multi_slot_trader_fraction_250ms",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_coordination_transport.py",
    "scripts/e4_v12_latency300_microstructure_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
    "scripts/e4_v12_latency300_labels.py",
    "scripts/e4_v12_online_conformal_precision.py",
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


def atomic_npy(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            np.save(handle, values, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def coordination_features(trace: base.Trace) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    observed = [point for point in trace.points if point.timestamp_ns <= decision_ns]
    reserve = base.latest_point(observed, decision_ns)
    if reserve is None or reserve.complete:
        return None
    trades = sorted(
        micro.public_trades(trace),
        key=lambda point: (point.timestamp_ns, point.slot, point.signature),
    )
    slot_counts: Counter[int] = Counter(point.slot for point in trades)
    slot_traders: defaultdict[int, set[str]] = defaultdict(set)
    trader_slots: defaultdict[str, set[int]] = defaultdict(set)
    for point in trades:
        slot_traders[point.slot].add(point.trader)
        trader_slots[point.trader].add(point.slot)
    total = len(trades)
    shares = (
        [count / total for count in slot_counts.values()] if total else []
    )
    slot_entropy = -sum(share * math.log(share) for share in shares if share > 0)
    normalized_slot_entropy = (
        slot_entropy / math.log(len(slot_counts)) if len(slot_counts) > 1 else 0.0
    )
    timestamps = np.asarray([point.timestamp_ns for point in trades], dtype=np.int64)
    intervals_ms = np.diff(timestamps).astype(np.float64) / 1_000_000
    interval_mean = float(np.mean(intervals_ms)) if len(intervals_ms) else 0.0
    interval_std = float(np.std(intervals_ms)) if len(intervals_ms) else 0.0
    interval_cv = interval_std / interval_mean if interval_mean > 0 else 0.0
    burstiness = (
        (interval_std - interval_mean) / (interval_std + interval_mean)
        if interval_std + interval_mean > 0
        else 0.0
    )
    first_latency = (
        min(max((trades[0].timestamp_ns - trace.create_ns) / 1_000_000, 0.0), 250.0)
        if trades
        else float(HORIZON_MS)
    )
    third_unique_latency = float(HORIZON_MS)
    seen: set[str] = set()
    for point in trades:
        seen.add(point.trader)
        if len(seen) >= 3:
            third_unique_latency = min(
                max((point.timestamp_ns - trace.create_ns) / 1_000_000, 0.0),
                250.0,
            )
            break
    traders_per_slot = [len(traders) for traders in slot_traders.values()]
    repeated_slot_trades = sum(
        count for count in slot_counts.values() if count > 1
    )
    values = (
        float(len(slot_counts)),
        float(len({point.signature for point in trades})),
        float(max(slot_counts, default=0) - min(slot_counts, default=0)),
        max(shares, default=0.0),
        sum(share * share for share in shares),
        normalized_slot_entropy,
        repeated_slot_trades / max(total, 1),
        sum(len(traders) > 1 for traders in slot_traders.values())
        / max(len(slot_traders), 1),
        median(traders_per_slot) if traders_per_slot else 0.0,
        float(max(traders_per_slot, default=0)),
        first_latency,
        third_unique_latency,
        interval_cv,
        burstiness,
        float(np.mean(intervals_ms <= 0)) if len(intervals_ms) else 0.0,
        sum(len(slots) > 1 for slots in trader_slots.values())
        / max(len(trader_slots), 1),
    )
    if len(values) != len(COORDINATION_FEATURE_NAMES) or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError(f"invalid coordination feature vector for {trace.mint}")
    return values


def parse_source_spec(
    spec: base.CaptureSpec, row_lookup: dict[str, int]
) -> dict[str, Any]:
    actual_sha256 = base.sha256_path(spec.events_path)
    if actual_sha256 != spec.expected_sha256:
        raise ValueError(f"capture hash changed: {spec.run_id}")
    traces, parse_errors = base.load_capture(spec, Counter())
    if parse_errors:
        raise ValueError(f"capture {spec.run_id} has {parse_errors} parse errors")
    values = []
    missing_rows = 0
    invalid_rows = 0
    for trace in traces:
        index = row_lookup.get(trace.mint)
        if index is None:
            missing_rows += 1
            continue
        feature_values = coordination_features(trace)
        if feature_values is None:
            invalid_rows += 1
            continue
        values.append((index, feature_values))
    return {
        "run_id": spec.run_id,
        "sha256": actual_sha256,
        "launches": len(traces),
        "rows": len(values),
        "missing_rows": missing_rows,
        "invalid_rows": invalid_rows,
        "values": values,
    }


def build_matrix(root: Path, payload: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    rows = payload["rows"]
    row_lookup: dict[str, dict[str, int]] = {}
    for index, row in enumerate(rows):
        row_lookup.setdefault(str(row.run_id), {})[str(row.mint)] = index
    matrix = np.full(
        (len(rows), len(COORDINATION_FEATURE_NAMES)), np.nan, dtype=np.float64
    )
    visited = np.zeros(len(rows), dtype=bool)
    specs = adaptive.capture_specs(root, include_holdout=True)
    source_runs = {spec.run_id for spec in specs}
    audits = []
    with ProcessPoolExecutor(max_workers=PARSE_WORKERS) as executor:
        futures = {
            executor.submit(parse_source_spec, spec, row_lookup[spec.run_id]): spec.run_id
            for spec in specs
        }
        for number, future in enumerate(as_completed(futures), 1):
            audit = future.result()
            for index, values in audit.pop("values"):
                if visited[index]:
                    raise ValueError(f"duplicate coordination row index {index}")
                matrix[index] = values
                visited[index] = True
            audits.append(audit)
            print(
                f"parsed raw capture {number}/{len(specs)} run={audit['run_id']} "
                f"visited={int(visited.sum())}",
                flush=True,
            )
    cached_traces = payload.get("traces")
    if not isinstance(cached_traces, dict):
        raise TypeError("combined cache does not contain later traces")
    cached_counts: Counter[str] = Counter()
    for trace in cached_traces.values():
        run_id = str(trace.run_id)
        if run_id in source_runs:
            continue
        index = row_lookup.get(run_id, {}).get(str(trace.mint))
        if index is None:
            continue
        feature_values = coordination_features(trace)
        if feature_values is None:
            continue
        if visited[index]:
            raise ValueError(f"duplicate cached coordination row index {index}")
        matrix[index] = feature_values
        visited[index] = True
        cached_counts[run_id] += 1
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint)) for index in missing[:10]
        ]
        raise ValueError(f"{len(missing)} rows lack coordination features: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("coordination matrix contains non-finite values")
    atomic_npy(CACHE, matrix)
    cache_audit = {
        "version": "e4-v12-latency300-coordination-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(COORDINATION_FEATURE_NAMES),
        "feature_names": list(COORDINATION_FEATURE_NAMES),
        "matrix_sha256": base.sha256_path(CACHE),
        "raw_capture_runs": len(specs),
        "raw_capture_bytes": sum(spec.events_path.stat().st_size for spec in specs),
        "raw_source_hashes_verified": True,
        "raw_run_audits": sorted(audits, key=lambda row: int(row["run_id"])),
        "cached_later_runs": [
            {"run_id": run_id, "rows": cached_counts[run_id]}
            for run_id in sorted(cached_counts, key=int)
        ],
        "cached_later_rows": sum(cached_counts.values()),
        "active_untouched_live_data_used": False,
        "future_values_in_features": False,
        "production_paths_changed": 0,
    }
    base.write_json(CACHE_METADATA, cache_audit)
    return matrix, cache_audit


def load_or_build_matrix(
    root: Path, payload: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    if CACHE.is_file() and CACHE_METADATA.is_file():
        metadata = json.loads(CACHE_METADATA.read_text(encoding="utf-8"))
        matrix = np.load(CACHE, allow_pickle=False)
        if (
            metadata.get("dataset_manifest_sha256") == payload["manifest_sha256"]
            and metadata.get("matrix_sha256") == base.sha256_path(CACHE)
            and matrix.shape == (len(payload["rows"]), len(COORDINATION_FEATURE_NAMES))
            and bool(np.all(np.isfinite(matrix)))
            and metadata.get("active_untouched_live_data_used") is False
        ):
            return matrix, metadata
    return build_matrix(root, payload)


def main() -> dict[str, Any]:
    root = Path.cwd()
    label_metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if label_metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if label_metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if label_metadata["all_source_hashes_verified"] is not True:
        raise ValueError("label source hashes were not verified")
    if label_metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    matrix, cache_audit = load_or_build_matrix(root, payload)
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
    records = []
    for column, name in enumerate(COORDINATION_FEATURE_NAMES):
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
    strongest_minimum_later_auc = max(
        row["minimum_later_oriented_auc"] for row in records
    )
    candidate_warranted = len(stable) >= 2 and strongest_minimum_later_auc >= 0.55
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(COORDINATION_FEATURE_NAMES),
        "coordination_matrix_sha256": cache_audit["matrix_sha256"],
        "label_matrix_sha256": label_metadata["pnl_sha256"],
        "causal_horizon_ms": HORIZON_MS,
        "latency_ms": labels.LATENCY_MS,
        "orientation_policy": (
            "orient each new feature once from epoch 0, then freeze direction "
            "for epochs 1-3"
        ),
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "label_audit": label_metadata,
        "coordination_cache_audit": cache_audit,
        "features": records,
        "feature_count": len(records),
        "direction_consistent_features": sum(
            row["direction_consistent_all_epochs"] for row in records
        ),
        "all_epoch_auc_52_features": stable,
        "all_epoch_auc_52_feature_count": len(stable),
        "strongest_minimum_later_oriented_auc": strongest_minimum_later_auc,
        "candidate_warranted": candidate_warranted,
        "candidate_gate": {
            "minimum_stable_features": 2,
            "minimum_strongest_later_auc": 0.55,
        },
        "finding": (
            f"{len(stable)} of {len(records)} new causal sub-250ms coordination "
            "features preserved epoch-0 direction with oriented AUC >=0.52 in "
            "all four epochs. This is a transport diagnostic, not a fitted "
            f"candidate or approval. Candidate warranted: {candidate_warranted}."
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
