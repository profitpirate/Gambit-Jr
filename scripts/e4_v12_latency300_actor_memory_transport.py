#!/usr/bin/env python3
"""Rebuild point-in-time actor memory from exact-300 ms outcomes.

The existing actor features were resolved from fast-entry outcomes.  This
diagnostic rebuilds the same buyer/creator memory schema against the exact
300 ms label contract, delays every update by 60 seconds, and compares
chronological transport without fitting or promoting a candidate.
"""

from __future__ import annotations

import heapq
import json
import os
import pickle
from collections import Counter
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_latency300_feature_transport as transport
import e4_v12_latency300_labels as labels
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base
import numpy as np
from scipy.stats import ks_2samp

OUTPUT = Path("artifacts/e4-v12-latency300-actor-memory-transport.json")
CACHE = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-actor-memory.npy"
)
CACHE_METADATA = Path(
    ".tmp-relative-price-capped-actor-holdout/latency300-actor-memory.json"
)
SCHEMA_VERSION = "e4-v12-latency300-actor-memory-transport-v1"
DIAGNOSTIC_FAMILY = "latency300-target-aligned-actor-memory-v12"
HORIZON_MS = 250
LATENCY_MS = 300
RESOLUTION_DELAY_MS = 60_000
WINDOWS_PER_EPOCH = 16
DESCRIPTIVE_AUC_FLOOR = 0.52
MATERIAL_IMPROVEMENT_FLOOR = 0.02
SOURCE_FILES = (
    "scripts/e4_v12_latency300_actor_memory_transport.py",
    "scripts/e4_v12_latency300_feature_transport.py",
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


def build_matrix(
    root: Path, payload: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    rows = payload["rows"]
    row_lookup = {
        (str(row.run_id), str(row.mint)): index for index, row in enumerate(rows)
    }
    if len(row_lookup) != len(rows):
        raise ValueError("combined rows do not have unique run/mint keys")
    windows = sorted({str(row.run_id) for row in rows}, key=int)
    specs = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(root, include_holdout=True)
    }
    cached_traces = payload.get("traces")
    if not isinstance(cached_traces, dict):
        raise TypeError("combined cache does not contain later traces")
    cached_by_run: dict[str, list[base.Trace]] = {}
    for trace in cached_traces.values():
        cached_by_run.setdefault(str(trace.run_id), []).append(trace)

    matrix = np.full(
        (len(rows), len(actors.ACTOR_FEATURE_NAMES)),
        np.nan,
        dtype=np.float64,
    )
    visited = np.zeros(len(rows), dtype=bool)
    buyer_states: dict[str, actors.ActorState] = {}
    creator_states: dict[str, actors.ActorState] = {}
    pending: list[tuple[int, int, str, tuple[str, ...], float]] = []
    sequence = 0
    creator_counts: Counter[str] = Counter()
    audits = []
    nominal_position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION

    def flush(timestamp_ns: int) -> None:
        while pending and pending[0][0] <= timestamp_ns:
            _, _, creator, buyers, pnl_sol = heapq.heappop(pending)
            creator_states.setdefault(creator, actors.ActorState()).update(pnl_sol)
            for buyer in buyers:
                buyer_states.setdefault(buyer, actors.ActorState()).update(pnl_sol)

    for window_number, run_id in enumerate(windows, 1):
        if run_id in specs:
            spec = specs[run_id]
            actual_sha256 = base.sha256_path(spec.events_path)
            if actual_sha256 != spec.expected_sha256:
                raise ValueError(f"capture hash changed: {run_id}")
            traces, parse_errors = base.load_capture(spec, creator_counts)
            source = "raw_hash_verified"
        else:
            traces = cached_by_run.get(run_id, [])
            parse_errors = 0
            actual_sha256 = "bound-by-combined-cache-manifest"
            source = "canonical_combined_cache"
        traces.sort(key=lambda trace: (trace.create_ns, trace.mint))
        counts: Counter[str] = Counter()
        for trace in traces:
            index = row_lookup.get((run_id, str(trace.mint)))
            if index is None:
                continue
            decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
            flush(decision_ns)
            buyers = actors.early_buyers(trace)
            values = actors.actor_features(
                buyers,
                trace.creator,
                buyer_states,
                creator_states,
            )
            outcome = adaptive.adaptive_outcome(
                trace,
                HORIZON_MS,
                adaptive.POLICIES[conformal.POLICY_INDEX],
                latency_ms=LATENCY_MS,
            )
            if outcome is None:
                raise ValueError(f"exact-300 ms outcome missing: {run_id}/{trace.mint}")
            pnl_sol = base.net_pnl(outcome, 0.001, nominal_position)
            matrix[index] = values
            visited[index] = True
            sequence += 1
            heapq.heappush(
                pending,
                (
                    decision_ns + RESOLUTION_DELAY_MS * 1_000_000,
                    sequence,
                    trace.creator,
                    buyers,
                    pnl_sol,
                ),
            )
            counts["rows"] += 1
            counts["rows_with_known_buyer"] += int(values[1] > 0)
            counts["rows_with_known_creator"] += int(values[11] > 0)
        audits.append(
            {
                "run_id": run_id,
                "source": source,
                "sha256": actual_sha256,
                "launches": len(traces),
                "parse_errors": parse_errors,
                **dict(counts),
            }
        )
        print(
            f"actor memory {window_number}/{len(windows)} run={run_id} "
            f"visited={int(visited.sum())}",
            flush=True,
        )
    if not bool(np.all(visited)):
        missing = np.flatnonzero(~visited)
        examples = [
            (str(rows[index].run_id), str(rows[index].mint))
            for index in missing[:10]
        ]
        raise ValueError(f"{len(missing)} rows lack exact-latency actor memory: {examples}")
    if not bool(np.all(np.isfinite(matrix))):
        raise ValueError("exact-latency actor memory contains non-finite values")
    atomic_npy(CACHE, matrix)
    metadata = {
        "version": "e4-v12-latency300-actor-memory-cache-v1",
        "dataset_manifest_sha256": payload["manifest_sha256"],
        "rows": len(rows),
        "features": len(actors.ACTOR_FEATURE_NAMES),
        "feature_names": list(actors.ACTOR_FEATURE_NAMES),
        "matrix_sha256": base.sha256_path(CACHE),
        "capture_windows": len(windows),
        "raw_capture_runs": len(specs),
        "raw_capture_bytes": sum(
            spec.events_path.stat().st_size for spec in specs.values()
        ),
        "cached_later_runs": len(windows) - len(specs),
        "raw_source_hashes_verified": True,
        "run_audits": audits,
        "latency_ms": LATENCY_MS,
        "resolution_delay_ms": RESOLUTION_DELAY_MS,
        "future_values_in_features": False,
        "active_untouched_live_data_used": False,
        "production_paths_changed": 0,
    }
    base.write_json(CACHE_METADATA, metadata)
    return matrix, metadata


def load_or_build_matrix(
    root: Path, payload: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any]]:
    if CACHE.is_file() and CACHE_METADATA.is_file():
        metadata = json.loads(CACHE_METADATA.read_text(encoding="utf-8"))
        if (
            metadata.get("dataset_manifest_sha256") == payload["manifest_sha256"]
            and metadata.get("latency_ms") == LATENCY_MS
            and metadata.get("resolution_delay_ms") == RESOLUTION_DELAY_MS
            and metadata.get("matrix_sha256") == base.sha256_path(CACHE)
        ):
            matrix = np.load(CACHE, allow_pickle=False)
            expected = (len(payload["rows"]), len(actors.ACTOR_FEATURE_NAMES))
            if matrix.shape == expected and bool(np.all(np.isfinite(matrix))):
                return matrix, metadata
    return build_matrix(root, payload)


def feature_records(
    matrix: np.ndarray,
    labels_positive: np.ndarray,
    epoch_masks: list[np.ndarray],
) -> list[dict[str, Any]]:
    records = []
    for column, name in enumerate(actors.ACTOR_FEATURE_NAMES):
        aucs = [
            transport.auc_or_chance(labels_positive[mask], matrix[mask, column])
            for mask in epoch_masks
        ]
        direction = 1 if aucs[0] >= 0.5 else -1
        oriented = [auc if direction == 1 else 1.0 - auc for auc in aucs]
        statistic, pvalue = ks_2samp(
            matrix[epoch_masks[0], column],
            matrix[epoch_masks[-1], column],
        )
        records.append(
            {
                "feature": name,
                "earliest_epoch_direction": (
                    "higher_is_positive" if direction == 1 else "lower_is_positive"
                ),
                "raw_auc_by_epoch": aucs,
                "oriented_auc_by_epoch": oriented,
                "minimum_later_oriented_auc": min(oriented[1:]),
                "mean_later_oriented_auc": float(np.mean(oriented[1:])),
                "direction_consistent_all_epochs": all(
                    auc >= 0.5 for auc in oriented
                ),
                "all_epochs_above_descriptive_floor": all(
                    auc >= DESCRIPTIVE_AUC_FLOOR for auc in oriented
                ),
                "first_to_last_ks_statistic": float(statistic),
                "first_to_last_ks_pvalue": float(pvalue),
            }
        )
    return records


def main() -> dict[str, Any]:
    root = Path.cwd()
    label_metadata = json.loads(labels.METADATA_PATH.read_text(encoding="utf-8"))
    with conformal.CACHE.open("rb") as handle:
        payload = pickle.load(handle)
    if label_metadata["dataset_manifest_sha256"] != payload["manifest_sha256"]:
        raise ValueError("300 ms labels target another evidence manifest")
    if label_metadata["pnl_sha256"] != base.sha256_path(labels.PNL_PATH):
        raise ValueError("300 ms label matrix fingerprint changed")
    if label_metadata["active_untouched_live_data_used"] is not False:
        raise ValueError("active untouched live evidence was consumed")
    rows = payload["rows"]
    pnl = np.load(labels.PNL_PATH, allow_pickle=False)
    exact, cache_audit = load_or_build_matrix(root, payload)
    existing_all = np.asarray([row.features for row in rows], dtype=np.float64)
    existing = existing_all[:, -len(actors.ACTOR_FEATURE_NAMES) :]
    windows = sorted({str(row.run_id) for row in rows}, key=int)
    window_index = {run_id: index for index, run_id in enumerate(windows)}
    row_windows = np.asarray(
        [window_index[str(row.run_id)] for row in rows], dtype=np.int16
    )
    epoch_masks = [
        (row_windows >= start) & (row_windows < start + WINDOWS_PER_EPOCH)
        for start in range(0, len(windows), WINDOWS_PER_EPOCH)
    ]
    labels_positive = pnl > 0
    exact_records = feature_records(exact, labels_positive, epoch_masks)
    existing_records = feature_records(existing, labels_positive, epoch_masks)
    existing_by_name = {row["feature"]: row for row in existing_records}
    comparisons = []
    for row in exact_records:
        baseline = existing_by_name[row["feature"]]
        comparisons.append(
            {
                **row,
                "existing_fast_memory_minimum_later_oriented_auc": baseline[
                    "minimum_later_oriented_auc"
                ],
                "minimum_later_auc_improvement": row[
                    "minimum_later_oriented_auc"
                ]
                - baseline["minimum_later_oriented_auc"],
                "existing_fast_memory_direction_consistent": baseline[
                    "direction_consistent_all_epochs"
                ],
            }
        )
    comparisons.sort(
        key=lambda row: (
            row["all_epochs_above_descriptive_floor"],
            row["minimum_later_auc_improvement"],
            row["minimum_later_oriented_auc"],
        ),
        reverse=True,
    )
    material = [
        row["feature"]
        for row in comparisons
        if row["all_epochs_above_descriptive_floor"]
        and row["minimum_later_auc_improvement"] >= MATERIAL_IMPROVEMENT_FLOOR
    ]
    identity = {
        "diagnostic_family": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(root),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "label_matrix_sha256": label_metadata["pnl_sha256"],
        "exact_actor_memory_matrix_sha256": cache_audit["matrix_sha256"],
        "feature_set_fingerprint": base.stable_hash(actors.ACTOR_FEATURE_NAMES),
        "evidence_epoch": windows,
        "latency_ms": LATENCY_MS,
        "resolution_delay_ms": RESOLUTION_DELAY_MS,
        "orientation_policy": (
            "orient from epoch 0 once, freeze direction for epochs 1-3"
        ),
        "descriptive_auc_floor": DESCRIPTIVE_AUC_FLOOR,
        "material_improvement_floor": MATERIAL_IMPROVEMENT_FLOOR,
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "label_audit": label_metadata,
        "actor_memory_cache_audit": cache_audit,
        "features": comparisons,
        "feature_count": len(comparisons),
        "all_epoch_auc_52_features": [
            row["feature"]
            for row in comparisons
            if row["all_epochs_above_descriptive_floor"]
        ],
        "materially_improved_stable_features": material,
        "materially_improved_stable_feature_count": len(material),
        "finding": (
            f"{len(material)} exact-300 ms actor-memory features improved "
            f"minimum later-epoch AUC by at least {MATERIAL_IMPROVEMENT_FLOOR:.2f} "
            "while remaining above the descriptive transport floor."
        ),
        "candidate_warranted": bool(material),
        "candidate_fitted": False,
        "retired_family": not bool(material),
        "failure_classification": (
            None if material else "VALIDATION_COLLAPSE"
        ),
        "material_change_required_before_rerun": (
            None
            if material
            else "A new causal risk-set feature family beyond buyer/creator "
            "outcome-memory relabelling; actor-memory target or threshold "
            "retuning is prohibited."
        ),
        "development_gate_passed": False,
        "active_untouched_live_data_used": False,
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
