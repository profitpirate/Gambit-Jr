#!/usr/bin/env python3
"""Evaluate the frozen conformal thesis once on ten untouched live windows."""

from __future__ import annotations

import argparse
import json
import pickle
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_causal_regime_veto_holdout as prior_holdout
import e4_v12_online_conformal_latency as latency
import e4_v12_online_conformal_precision as precision
import e4_v12_profit_survival_search as base
import e4_v12_relative_price_capped_actor_holdout as v11_holdout
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

SCHEMA_VERSION = "e4-v12-online-conformal-untouched-live-v1"
PROTOCOL_PATH = Path("artifacts/e4-v12-online-conformal-holdout-protocol.json")
DEFAULT_MANIFEST_PATH = Path(
    "artifacts/e4-v12-online-conformal-holdout-manifest.json"
)
DEFAULT_CACHE_PATH = Path(
    ".tmp-online-conformal-holdout/combined-rows.pkl"
)
DEFAULT_OUTPUT_PATH = Path(
    "artifacts/e4-v12-online-conformal-untouched-live.json"
)
SUPPLEMENTAL_MANIFEST = Path(
    ".tmp-regime-veto-holdout/e4-v12-causal-regime-veto-holdout-manifest.json"
)
V11_MANIFEST = Path(
    "artifacts/e4-v12-relative-price-capped-actor-holdout-manifest.json"
)
CURRENT_CACHE = precision.CACHE
PNL_MATRIX = precision.PNL_MATRIX
PNL_METADATA = PNL_MATRIX.with_suffix(".json")
MANIFEST_VERSION = "e4-v12-online-conformal-holdout-manifest-v1"
FINAL_ROLE = "untouched_live"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object in {path}")
    return value


def resolved_path(root: Path, value: Any) -> Path:
    path = Path(str(value))
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"capture path escapes repository: {path}")
    return resolved


def verify_protocol(root: Path) -> dict[str, Any]:
    protocol = read_json(root / PROTOCOL_PATH)
    if protocol.get("version") != "e4-v12-online-conformal-holdout-protocol-v1":
        raise ValueError("unsupported online-conformal holdout protocol")
    frozen_spec = protocol.get("frozen_candidate")
    if not isinstance(frozen_spec, dict):
        raise TypeError("frozen candidate specification is not an object")
    frozen_path = resolved_path(root, frozen_spec.get("path", ""))
    if precision.sha256_lf(frozen_path) != frozen_spec.get("sha256"):
        raise ValueError("frozen online-conformal candidate fingerprint changed")
    frozen = read_json(frozen_path)
    if frozen.get("experiment_id") != protocol.get("experiment_id"):
        raise ValueError("protocol and frozen experiment IDs differ")
    if frozen.get("source_commit") != frozen_spec.get("source_commit"):
        raise ValueError("protocol and frozen source commits differ")
    if frozen.get("holdout_status") != "strictly later evidence required":
        raise ValueError("candidate was not frozen for later evidence")
    for relative, digest in frozen.get("source_artifacts", {}).items():
        if precision.sha256_lf(resolved_path(root, relative)) != digest:
            raise ValueError(f"frozen source artifact changed: {relative}")
    if precision.source_code_fingerprint(root) != frozen["identity"][
        "source_code_fingerprint"
    ]:
        raise ValueError("frozen online-conformal source stack changed")
    result_policy = protocol.get("result_policy", {})
    if (
        frozen.get("production_paths_changed") != 0
        or result_policy.get("production_paths_changed") != 0
        or result_policy.get("production_promotion_authorised") is not False
        or result_policy.get("production_deployment_authorised") is not False
    ):
        raise ValueError("holdout contract contains production authority")
    return protocol


def validate_manifest(
    root: Path,
    manifest: dict[str, Any],
    protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported online-conformal holdout manifest")
    if manifest.get("experiment_id") != protocol.get("experiment_id"):
        raise ValueError("holdout manifest targets another experiment")
    if manifest.get("protocol_sha256_lf") != precision.sha256_lf(
        root / PROTOCOL_PATH
    ):
        raise ValueError("holdout manifest protocol fingerprint changed")
    if manifest.get("production_paths_changed") != 0:
        raise ValueError("holdout manifest indicates a production change")
    captures = [dict(row) for row in manifest.get("captures", [])]
    contract = protocol["final_evidence_contract"]
    required_windows = int(contract["required_capture_windows"])
    required_launches = int(contract["launches_per_window"])
    if len(captures) > required_windows:
        raise ValueError("manifest exceeds the predeclared ten capture windows")
    freeze_ns = int(protocol["frozen_candidate"]["frozen_at_epoch_ns"])
    consumed_ns = int(
        contract["every_capture_starts_after_consumed_evidence_epoch_ns"]
    )
    seen: set[str] = set()
    for row in captures:
        run_id = str(row.get("run_id", ""))
        if not run_id or run_id in seen:
            raise ValueError(f"invalid or duplicate run ID: {run_id}")
        seen.add(run_id)
        if row.get("role") != FINAL_ROLE:
            raise ValueError(f"invalid untouched evidence role for {run_id}")
        if int(row.get("launches", 0)) != required_launches:
            raise ValueError(f"capture {run_id} does not contain 3000 launches")
        if int(row.get("capture_errors", -1)) != 0:
            raise ValueError(f"capture {run_id} contains capture errors")
        if row.get("hypothesis_only") is not True:
            raise ValueError(f"capture {run_id} was not hypothesis-only")
        if int(row.get("mainnet_transactions_sent", -1)) != 0:
            raise ValueError(f"capture {run_id} sent mainnet transactions")
        if float(row.get("mainnet_funds_risked_sol", -1)) != 0:
            raise ValueError(f"capture {run_id} risked mainnet funds")
        workflow_ns = int(row.get("workflow_run_started_at_epoch_ns", 0))
        start_ns = int(row.get("capture_start_ns", 0))
        end_ns = int(row.get("capture_end_ns", 0))
        if workflow_ns <= freeze_ns or start_ns <= max(freeze_ns, consumed_ns):
            raise ValueError(f"capture {run_id} did not begin after the freeze")
        if end_ns <= start_ns:
            raise ValueError(f"capture {run_id} has invalid bounds")
        events_path = resolved_path(root, row.get("events_path", ""))
        batch_path = resolved_path(root, row.get("batch_path", ""))
        if not events_path.is_file() or not batch_path.is_file():
            raise ValueError(f"capture files missing for {run_id}")
        if base.sha256_path(events_path) != row.get("events_sha256"):
            raise ValueError(f"events hash mismatch for {run_id}")
        if base.sha256_path(batch_path) != row.get("batch_sha256"):
            raise ValueError(f"batch hash mismatch for {run_id}")
        row["run_id"] = run_id
        row["events_path"] = str(events_path.relative_to(root)).replace("\\", "/")
        row["batch_path"] = str(batch_path.relative_to(root)).replace("\\", "/")
    captures.sort(key=lambda row: (int(row["capture_start_ns"]), row["run_id"]))
    for left, right in pairwise(captures):
        if int(right["capture_start_ns"]) <= int(left["capture_end_ns"]):
            raise ValueError(
                "captures overlap or are not chronological: "
                f"{left['run_id']}/{right['run_id']}"
            )
    return captures


def combined_rows(
    root: Path,
    captures: list[dict[str, Any]],
    cache_path: Path,
    protocol: dict[str, Any],
) -> tuple[
    list[base.ResearchRow],
    dict[tuple[str, str], base.Trace],
    dict[str, Any],
]:
    supplemental_protocol = prior_holdout.verify_protocol(root)
    supplemental_manifest = read_json(root / SUPPLEMENTAL_MANIFEST)
    supplemental = prior_holdout.validate_manifest(
        root, supplemental_manifest, supplemental_protocol
    )
    v11_protocol = v11_holdout.verify_protocol(root)
    v11_manifest = read_json(root / V11_MANIFEST)
    v11_captures = v11_holdout.validate_manifest(root, v11_manifest, v11_protocol)
    future = [*supplemental, *v11_captures, *captures]
    fingerprint = base.stable_hash(
        [
            {
                key: row[key]
                for key in (
                    "run_id",
                    "capture_start_ns",
                    "capture_end_ns",
                    "events_sha256",
                    "batch_sha256",
                )
            }
            for row in future
        ]
    )
    rows, traces, audit = prior_holdout.load_combined_rows(
        root, future, fingerprint, cache_path
    )
    expected_captures = 64 + len(captures)
    expected_launches = 192_000 + 3_000 * len(captures)
    if audit["captures"] != expected_captures or audit["launches"] != expected_launches:
        raise ValueError("combined online-conformal evidence totals differ")
    if audit["parse_errors"] != 0 or audit["future_values_in_features"] is not False:
        raise ValueError("combined evidence failed integrity or anti-lookahead")
    windows = sorted({row.run_id for row in rows}, key=int)
    expected_prefix = protocol["frozen_candidate"]
    frozen = read_json(resolved_path(root, expected_prefix["path"]))
    evidence_epoch = list(frozen["identity"]["evidence_epoch"])
    if windows[:64] != evidence_epoch:
        raise ValueError("frozen training evidence chronology changed")
    if windows[64:] != [row["run_id"] for row in captures]:
        raise ValueError("untouched evidence chronology changed")
    return rows, traces, audit


def label_map(root: Path) -> tuple[dict[tuple[str, str, int], bool], dict[str, Any]]:
    with (root / CURRENT_CACHE).open("rb") as handle:
        cached = pickle.load(handle)
    current_rows = cached["rows"]
    pnl = np.load(root / PNL_MATRIX, allow_pickle=False)[:, precision.POLICY_INDEX]
    metadata = read_json(root / PNL_METADATA)
    if len(current_rows) != len(pnl) or metadata.get("rows") != len(current_rows):
        raise ValueError("frozen training labels do not align with cached rows")
    if metadata.get("dataset_manifest_sha256") != cached.get("manifest_sha256"):
        raise ValueError("frozen training label manifest changed")
    labels = {
        (row.run_id, row.mint, row.decision_ns): bool(value > 0)
        for row, value in zip(current_rows, pnl, strict=True)
    }
    return labels, metadata


def conservative_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    active: list[int] = []
    accepted: list[dict[str, Any]] = []
    rejected = 0
    for row in sorted(
        candidates,
        key=lambda value: (
            int(value["run_id"]),
            int(value["decision_ns"]),
            str(value["mint"]),
        ),
    ):
        decision_ns = int(row["decision_ns"])
        active = [exit_ns for exit_ns in active if exit_ns > decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected += 1
            continue
        accepted.append(row)
        active.append(decision_ns + precision.MAXIMUM_HOLD_MS * 1_000_000)
    return accepted, rejected


def compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "ledger"}


def golden_gate(
    economics: dict[str, dict[str, Any]], protocol: dict[str, Any]
) -> dict[str, Any]:
    zero = economics["0"]
    gate = protocol["golden_gate"]
    requirements = {
        "minimum_closed_trades": zero["trades"] >= gate["minimum_closed_trades"],
        "minimum_capture_windows_represented": zero["capture_windows"]
        >= gate["minimum_capture_windows_represented"],
        "minimum_profitable_capture_windows": zero["profitable_capture_windows"]
        >= gate["minimum_profitable_capture_windows"],
        "minimum_win_rate": zero["win_rate"] >= gate["minimum_win_rate"],
        "minimum_wilson_bound": zero["wilson_95_lower_bound"]
        >= gate["minimum_wilson_95_lower_bound"],
        "positive_net_pnl": zero["net_pnl_sol"] > gate["minimum_net_pnl_sol"],
        "minimum_profit_factor": zero["profit_factor"]
        >= gate["minimum_profit_factor"],
        "maximum_drawdown": zero["maximum_drawdown_fraction"]
        <= gate["maximum_drawdown_fraction"],
        "largest_winner_limit": zero["largest_winner_contribution"]
        <= gate["maximum_largest_winner_contribution"],
        "every_latency_positive": all(
            row["net_pnl_sol"] > 0 for row in economics.values()
        ),
        "every_latency_profit_factor": all(
            row["profit_factor"] >= gate["every_latency_minimum_profit_factor"]
            for row in economics.values()
        ),
        "every_latency_full_quote_coverage": all(
            row["quote_coverage"] >= gate["every_latency_quote_coverage"]
            for row in economics.values()
        ),
    }
    passed = all(requirements.values())
    if passed:
        failure = None
    elif not requirements["every_latency_full_quote_coverage"]:
        failure = "LATENCY_FAILURE"
    elif not requirements["minimum_closed_trades"]:
        failure = "INSUFFICIENT_LABELS"
    elif not requirements["positive_net_pnl"]:
        failure = "NEGATIVE_EXPECTANCY"
    elif not requirements["minimum_profit_factor"]:
        failure = "INADEQUATE_PROFIT_FACTOR"
    elif not requirements["minimum_win_rate"]:
        failure = "FALSE_POSITIVE_OVERLOAD"
    else:
        failure = "HOLDOUT_COLLAPSE"
    return {
        "status": (
            "UNTOUCHED_LIVE_GATE_PASSED"
            if passed
            else "UNTOUCHED_LIVE_GATE_FAILED"
        ),
        "untouched_live_gate_passed": passed,
        "requirements": requirements,
        "failed_requirements": [
            name for name, value in requirements.items() if not value
        ],
        "failure_classification": failure,
        "live_confirmation_authorised": passed,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }


def waiting_report(
    root: Path,
    protocol: dict[str, Any],
    manifest_path: Path,
    captures: list[dict[str, Any]],
) -> dict[str, Any]:
    required = int(protocol["final_evidence_contract"]["required_capture_windows"])
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": protocol["experiment_id"],
        "protocol_sha256_lf": precision.sha256_lf(root / PROTOCOL_PATH),
        "manifest_sha256_lf": precision.sha256_lf(manifest_path),
        "captures": captures,
        "final_capture_windows": len(captures),
        "final_total_launches": sum(int(row["launches"]) for row in captures),
        "final_evaluation_complete": False,
        "deterministic_replay": False,
        "latencies": {},
        "verdict": {
            "status": f"WAITING_FOR_{required}_STRICTLY_LATER_WINDOWS",
            "untouched_live_gate_passed": False,
            "live_confirmation_authorised": False,
            "production_promotion_authorised": False,
            "production_deployment_authorised": False,
            "production_paths_changed": 0,
        },
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }


def run(root: Path, manifest_path: Path, cache_path: Path) -> dict[str, Any]:
    protocol = verify_protocol(root)
    manifest = read_json(manifest_path)
    captures = validate_manifest(root, manifest, protocol)
    required = int(protocol["final_evidence_contract"]["required_capture_windows"])
    if len(captures) != required:
        return waiting_report(root, protocol, manifest_path, captures)
    rows, traces, audit = combined_rows(root, captures, cache_path, protocol)
    frozen = read_json(
        resolved_path(root, protocol["frozen_candidate"]["path"])
    )
    split = frozen["identity"]["chronological_split"]["frozen_live_training"]
    fit_ids = set(split["fit"])
    calibration_ids = set(split["calibration"])
    final_ids = {row["run_id"] for row in captures}
    labels, label_audit = label_map(root)
    fit_rows = [row for row in rows if row.run_id in fit_ids]
    calibration_rows = sorted(
        (row for row in rows if row.run_id in calibration_ids),
        key=lambda row: (row.decision_ns, row.mint),
    )
    final_rows = sorted(
        (row for row in rows if row.run_id in final_ids),
        key=lambda row: (row.decision_ns, row.mint),
    )
    if len(fit_rows) + len(calibration_rows) != len(labels):
        raise ValueError("frozen training row count changed")
    fit_labels = np.asarray(
        [labels[(row.run_id, row.mint, row.decision_ns)] for row in fit_rows],
        dtype=bool,
    )
    model_parameters = frozen["identity"]["full_parameters"]["model"]
    model = HistGradientBoostingClassifier(
        learning_rate=float(model_parameters["learning_rate"]),
        max_iter=int(model_parameters["max_iter"]),
        max_leaf_nodes=int(model_parameters["max_leaf_nodes"]),
        min_samples_leaf=int(model_parameters["min_samples_leaf"]),
        l2_regularization=float(model_parameters["l2_regularization"]),
        class_weight=str(model_parameters["class_weight"]),
        early_stopping=False,
        random_state=int(model_parameters["random_seed_base"]),
    )
    fit_features = np.asarray([row.features for row in fit_rows], dtype=np.float32)
    calibration_features = np.asarray(
        [row.features for row in calibration_rows], dtype=np.float32
    )
    final_features = np.asarray([row.features for row in final_rows], dtype=np.float32)
    model.fit(fit_features, fit_labels)
    calibration_scores = model.predict_proba(calibration_features)[:, 1]
    final_scores = model.predict_proba(final_features)[:, 1]
    selected_mask, thresholds = precision.rolling_selection(
        calibration_scores, final_scores
    )
    selected = [
        {
            "run_id": row.run_id,
            "mint": row.mint,
            "decision_ns": row.decision_ns,
            "score": float(score),
        }
        for row, score, accepted in zip(
            final_rows, final_scores, selected_mask, strict=True
        )
        if accepted
    ]
    selected, conservative_rejections = conservative_candidates(selected)
    final_traces = {
        key: trace for key, trace in traces.items() if key[0] in final_ids
    }
    economics = {
        str(delay): latency.replay(selected, final_traces, delay)
        for delay in latency.LATENCIES_MS
    }
    replay_hashes = {
        str(delay): latency.replay(selected, final_traces, delay)["ledger_hash"]
        for delay in latency.LATENCIES_MS
    }
    deterministic = all(
        economics[key]["ledger_hash"] == replay_hashes[key]
        for key in economics
    )
    if not deterministic:
        raise ValueError("untouched live replay is not deterministic")
    verdict = golden_gate(economics, protocol)
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": protocol["experiment_id"],
        "protocol_sha256_lf": precision.sha256_lf(root / PROTOCOL_PATH),
        "manifest_sha256_lf": precision.sha256_lf(manifest_path),
        "captures": captures,
        "final_capture_windows": len(captures),
        "final_total_launches": sum(int(row["launches"]) for row in captures),
        "final_evaluation_complete": True,
        "data_audit": audit,
        "label_audit": label_audit,
        "selection_audit": {
            "fit_rows": len(fit_rows),
            "calibration_rows": len(calibration_rows),
            "untouched_rows": len(final_rows),
            "selected_before_conservative_concurrency": int(selected_mask.sum()),
            "selected_after_conservative_concurrency": len(selected),
            "conservative_concurrency_rejections": conservative_rejections,
            "threshold_count": len(thresholds),
            "threshold_hash": base.stable_hash(thresholds),
            "outcomes_used_for_selection": 0,
        },
        "latencies": {key: compact(value) for key, value in economics.items()},
        "deterministic_replay": deterministic,
        "verdict": verdict,
        "untouched_holdout_passed": verdict["untouched_live_gate_passed"],
        "live_confirmation_authorised": verdict[
            "live_confirmation_authorised"
        ],
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    manifest = args.manifest if args.manifest.is_absolute() else root / args.manifest
    cache = args.cache if args.cache.is_absolute() else root / args.cache
    output = args.output if args.output.is_absolute() else root / args.output
    if output.is_file():
        existing = read_json(output)
        if existing.get("final_evaluation_complete") is True:
            if existing.get("manifest_sha256_lf") != precision.sha256_lf(manifest):
                raise ValueError("final holdout was already evaluated on another manifest")
            print(json.dumps(existing["verdict"], indent=2, sort_keys=True))
            return
    report = run(root, manifest.resolve(), cache.resolve())
    base.write_json(output.resolve(), report)
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "final_capture_windows": report["final_capture_windows"],
                "deterministic_replay": report["deterministic_replay"],
                "verdict": report["verdict"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
