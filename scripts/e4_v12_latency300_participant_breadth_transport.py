#!/usr/bin/env python3
"""Audit causal sub-250 ms independent-participant breadth features."""

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

OUTPUT = Path("artifacts/e4-v12-latency300-participant-breadth-transport.json")
CACHE = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-participant-breadth.npy"
)
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-participant-breadth.json"
)
SCHEMA_VERSION = "e4-v12-latency300-participant-breadth-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-participant-breadth-transport-v12"
HORIZON_MS = 250
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
PARSE_WORKERS = 2
PARTICIPANT_FEATURE_NAMES = (
    "participant_unique_traders_250ms",
    "participant_unique_traders_100ms",
    "participant_unique_buyers_250ms",
    "participant_unique_sellers_250ms",
    "participant_breadth_ratio_250ms",
    "participant_repeat_trade_fraction_250ms",
    "participant_largest_flow_share_250ms",
    "participant_flow_hhi_250ms",
    "participant_normalized_entropy_250ms",
    "participant_effective_count_250ms",
    "participant_new_last50_share",
    "participant_unique_velocity_50_250ms",
    "participant_median_flow_share_250ms",
    "participant_log_flow_per_trader_250ms",
    "participant_side_breadth_balance_250ms",
    "participant_two_sided_trader_share_250ms",
)
SOURCE_FILES = (
    "scripts/e4_v12_latency300_participant_breadth_transport.py",
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


def participant_features(trace: base.Trace) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    observed = [point for point in trace.points if point.timestamp_ns <= decision_ns]
    valid = [point for point in observed if point.price_sol > 0]
    reserve = base.latest_point(observed, decision_ns)
    if not valid or reserve is None or reserve.complete:
        return None
    trades = micro.public_trades(trace)
    cutoff_100 = decision_ns - 100 * 1_000_000
    cutoff_50 = decision_ns - 50 * 1_000_000
    recent_100 = [point for point in trades if point.timestamp_ns >= cutoff_100]
    recent_50 = [point for point in trades if point.timestamp_ns >= cutoff_50]
    earlier = [point for point in trades if point.timestamp_ns < cutoff_50]

    trader_flow: defaultdict[str, float] = defaultdict(float)
    trader_count: Counter[str] = Counter()
    buyers: set[str] = set()
    sellers: set[str] = set()
    for point in trades:
        trader_flow[point.trader] += abs(point.sol_amount)
        trader_count[point.trader] += 1
        if point.kind in base.choice_sets.BUY_KINDS:
            buyers.add(point.trader)
        if point.kind in base.choice_sets.SELL_KINDS:
            sellers.add(point.trader)

    unique = len(trader_flow)
    trade_count = len(trades)
    total_flow = sum(trader_flow.values())
    shares = (
        [flow / total_flow for flow in trader_flow.values()]
        if total_flow > 0
        else []
    )
    entropy = -sum(share * math.log(share) for share in shares if share > 0)
    normalized_entropy = entropy / math.log(unique) if unique > 1 else 0.0
    effective_count = math.exp(entropy) if shares else 0.0
    last_50_traders = {point.trader for point in recent_50}
    earlier_traders = {point.trader for point in earlier}
    new_last_50 = last_50_traders - earlier_traders
    side_total = len(buyers) + len(sellers)

    values = (
        float(unique),
        float(len({point.trader for point in recent_100})),
        float(len(buyers)),
        float(len(sellers)),
        unique / max(trade_count, 1),
        sum(count > 1 for count in trader_count.values()) / max(unique, 1),
        max(shares, default=0.0),
        sum(share * share for share in shares),
        normalized_entropy,
        effective_count,
        len(new_last_50) / max(len(last_50_traders), 1),
        len(last_50_traders) / max(unique, 1),
        median(shares) if shares else 0.0,
        math.log1p(total_flow / max(unique, 1)),
        1.0 - abs(len(buyers) - len(sellers)) / max(side_total, 1),
        len(buyers & sellers) / max(unique, 1),
    )
    if len(values) != len(PARTICIPANT_FEATURE_NAMES) or not all(
        math.isfinite(value) for value in values
    ):
        raise ValueError(f"invalid participant feature vector for {trace.mint}")
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
        feature_values = participant_features(trace)
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
        (len(rows), len(PARTICIPANT_FEATURE_NAMES)), np.nan, dtype=np.float64
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
                    raise ValueError(f"duplicate participant row index {index}")
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
        feature_values = participant_features(trace)
        if feature_values is None:
            continue
        if visited[index]:
            raise ValueError(f"duplicate cached participant row index {index}")
        matrix[index] = feature_values
        visited[index] = True
        cached_counts[run_id] += 1
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint)) for index in missing[:10]
        ]
        raise ValueError(f"{len(missing)} rows lack participant features: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("participant matrix contains non-finite values")
    atomic_npy(CACHE, matrix)
    cache_audit = {
        "version": "e4-v12-latency300-participant-breadth-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(PARTICIPANT_FEATURE_NAMES),
        "feature_names": list(PARTICIPANT_FEATURE_NAMES),
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
            and matrix.shape
            == (len(payload["rows"]), len(PARTICIPANT_FEATURE_NAMES))
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
    for column, name in enumerate(PARTICIPANT_FEATURE_NAMES):
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
    identity = {
        "diagnostic_family_identifier": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(PARTICIPANT_FEATURE_NAMES),
        "participant_matrix_sha256": cache_audit["matrix_sha256"],
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
        "participant_cache_audit": cache_audit,
        "features": records,
        "feature_count": len(records),
        "direction_consistent_features": sum(
            row["direction_consistent_all_epochs"] for row in records
        ),
        "all_epoch_auc_52_features": stable,
        "all_epoch_auc_52_feature_count": len(stable),
        "finding": (
            f"{len(stable)} of {len(records)} new causal sub-250ms independent-"
            "participant features preserved epoch-0 direction with oriented AUC "
            ">=0.52 in all four epochs. This is a transport diagnostic, not a "
            "fitted candidate or approval."
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
