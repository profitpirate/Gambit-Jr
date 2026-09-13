#!/usr/bin/env python3
"""Evaluate frozen V11 once on ten strictly later live-capture windows."""

from __future__ import annotations

import argparse
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_actor_reputation_rules as reputation
import e4_v12_causal_regime_veto_holdout as prior_holdout
import e4_v12_profit_survival_search as base
import e4_v12_relative_price_capped_actor as v11

SCHEMA_VERSION = "e4-v12-relative-price-capped-actor-holdout-v1"
PROTOCOL_PATH = Path(
    "artifacts/e4-v12-relative-price-capped-actor-holdout-protocol.json"
)
DEFAULT_MANIFEST_PATH = Path(
    "artifacts/e4-v12-relative-price-capped-actor-holdout-manifest.json"
)
DEFAULT_CACHE_PATH = Path(
    ".tmp-relative-price-capped-actor-holdout/combined-rows.pkl"
)
DEFAULT_OUTPUT_PATH = Path(
    "artifacts/e4-v12-relative-price-capped-actor-untouched-live.json"
)
MANIFEST_VERSION = "e4-v12-relative-price-capped-actor-holdout-manifest-v1"
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
    if protocol.get("version") != (
        "e4-v12-relative-price-capped-actor-holdout-protocol-v1"
    ):
        raise ValueError("unsupported V11 holdout protocol")
    frozen_spec = protocol["frozen_candidate"]
    frozen_path = root / str(frozen_spec["path"])
    if v11.sha256_lf(frozen_path) != frozen_spec["sha256_lf"]:
        raise ValueError("frozen V11 candidate fingerprint changed")
    frozen = read_json(frozen_path)
    if frozen["experiment_id"] != protocol["experiment_id"]:
        raise ValueError("protocol and frozen V11 experiment IDs differ")
    source_path = root / "scripts/e4_v12_relative_price_capped_actor.py"
    if v11.sha256_lf(source_path) != frozen["identity"]["source_code_fingerprint"]:
        raise ValueError("frozen V11 source code fingerprint changed")
    if frozen["holdout_status"] != "strictly later evidence required":
        raise ValueError("V11 candidate was not ready for later evidence")
    if any(
        payload.get("production_paths_changed") != 0
        for payload in (frozen, protocol["result_policy"])
    ):
        raise ValueError("V11 freeze or protocol changed production paths")
    if protocol["result_policy"]["production_deployment_authorised"] is not False:
        raise ValueError("V11 protocol cannot authorise production deployment")
    return protocol


def validate_manifest(
    root: Path,
    manifest: dict[str, Any],
    protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported V11 holdout manifest")
    if manifest.get("experiment_id") != protocol["experiment_id"]:
        raise ValueError("V11 holdout manifest targets another experiment")
    captures = [dict(row) for row in manifest.get("captures", [])]
    required_windows = int(
        protocol["final_evidence_contract"]["required_capture_windows"]
    )
    if len(captures) > required_windows:
        raise ValueError("manifest exceeds the predeclared ten capture windows")
    required_launches = int(
        protocol["final_evidence_contract"]["launches_per_window"]
    )
    freeze_ns = int(protocol["frozen_candidate"]["frozen_at_epoch_ns"])
    consumed_ns = int(
        protocol["final_evidence_contract"][
            "every_capture_starts_after_consumed_evidence_epoch_ns"
        ]
    )
    seen: set[str] = set()
    for row in captures:
        run_id = str(row.get("run_id", ""))
        if not run_id or run_id in seen:
            raise ValueError(f"invalid or duplicate run ID: {run_id}")
        seen.add(run_id)
        if row.get("role") != FINAL_ROLE:
            raise ValueError(f"invalid V11 evidence role for {run_id}")
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
            raise ValueError(f"capture {run_id} did not begin after the V11 freeze")
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
) -> tuple[list[base.ResearchRow], dict[tuple[str, str], base.Trace], dict[str, Any]]:
    consumed_manifest = read_json(root / v11.CONSUMED_MANIFEST)
    consumed_protocol = prior_holdout.verify_protocol(root)
    consumed = prior_holdout.validate_manifest(
        root,
        consumed_manifest,
        consumed_protocol,
    )
    v11.verify_source_manifest(root, consumed_manifest)
    combined = [*consumed, *captures]
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
            for row in combined
        ]
    )
    rows, traces, audit = prior_holdout.load_combined_rows(
        root,
        combined,
        fingerprint,
        cache_path,
    )
    expected_captures = 54 + len(captures)
    expected_launches = 162_000 + 3_000 * len(captures)
    if audit["captures"] != expected_captures or audit["launches"] != expected_launches:
        raise ValueError("combined V11 evidence totals differ from the manifest")
    if audit["parse_errors"] != 0 or audit["future_values_in_features"] is not False:
        raise ValueError("combined V11 evidence failed integrity or anti-lookahead")
    return rows, traces, audit


def compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "ledger"}


def golden_gate(
    economics: dict[str, dict[str, Any]],
    baseline: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    zero = economics["0"]
    gate = protocol["golden_gate"]
    windows = list(zero["by_capture_window"].values())
    requirements = {
        "minimum_closed_trades": zero["trades"] >= gate["minimum_closed_trades"],
        "minimum_capture_windows_represented": zero["capture_windows"]
        >= gate["minimum_capture_windows_represented"],
        "minimum_profitable_windows": sum(row["pnl_sol"] > 0 for row in windows)
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
        "beats_capped_actor_baseline_pnl": zero["net_pnl_sol"]
        > baseline["net_pnl_sol"],
        "beats_capped_actor_baseline_win_rate": zero["win_rate"]
        > baseline["win_rate"],
        "beats_capped_actor_baseline_profit_factor": zero["profit_factor"]
        > baseline["profit_factor"],
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
        "status": "UNTOUCHED_LIVE_GATE_PASSED" if passed else "NOT_CONCLUSIVE",
        "untouched_live_gate_passed": passed,
        "requirements": requirements,
        "failed_requirements": [name for name, value in requirements.items() if not value],
        "failure_classification": failure,
        "live_confirmation_authorised": passed,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }


def run(root: Path, manifest_path: Path, cache_path: Path) -> dict[str, Any]:
    protocol = verify_protocol(root)
    manifest = read_json(manifest_path)
    captures = validate_manifest(root, manifest, protocol)
    required = int(protocol["final_evidence_contract"]["required_capture_windows"])
    report: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "experiment_id": protocol["experiment_id"],
        "protocol_sha256_lf": v11.sha256_lf(root / PROTOCOL_PATH),
        "manifest_sha256_lf": v11.sha256_lf(manifest_path),
        "captures": captures,
        "final_capture_windows": len(captures),
        "final_total_launches": sum(int(row["launches"]) for row in captures),
        "data_audit": {},
        "selection_audit": [],
        "relative_price_threshold": None,
        "latencies": {},
        "same_size_capped_actor_only_baseline": {},
        "deterministic_replay": False,
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
    if len(captures) != required:
        return report
    rows, traces, data_audit = combined_rows(root, captures, cache_path)
    final_ids = {str(row["run_id"]) for row in captures}
    consumed_rows = [row for row in rows if row.run_id not in final_ids]
    consumed_actor = [
        row for row in consumed_rows if reputation.buyer_matches(row, v11.ACTOR_RULE)
    ]
    threshold = v11.regime_threshold(consumed_actor)
    final_actor = [
        row
        for row in rows
        if row.run_id in final_ids and reputation.buyer_matches(row, v11.ACTOR_RULE)
    ]
    selected = [row for row in final_actor if v11.passes_regime(row, threshold)]
    selection_audit = [
        {
            "run_id": run_id,
            "actor_rows": sum(row.run_id == run_id for row in final_actor),
            "selected_rows": sum(row.run_id == run_id for row in selected),
        }
        for run_id in sorted(final_ids, key=int)
    ]
    economics = {
        str(latency): v11.simulate_capped_latency(selected, traces, latency)
        for latency in v11.LATENCIES_MS
    }
    repeat = {
        str(latency): v11.simulate_capped_latency(selected, traces, latency)[
            "ledger_hash"
        ]
        for latency in v11.LATENCIES_MS
    }
    deterministic = all(
        economics[key]["ledger_hash"] == repeat[key] for key in economics
    )
    if not deterministic:
        raise ValueError("V11 untouched replay is not deterministic")
    if any(
        trade["position_sol"] > v11.POSITION_CAP_SOL + 1e-12
        for metrics in economics.values()
        for trade in metrics["ledger"]
    ):
        raise ValueError("V11 untouched replay exceeded its frozen position cap")
    baseline = v11.simulate_capped_latency(final_actor, traces, 0)
    verdict = golden_gate(economics, baseline, protocol)
    report.update(
        {
            "data_audit": data_audit,
            "selection_audit": selection_audit,
            "relative_price_threshold": threshold,
            "latencies": {key: compact(value) for key, value in economics.items()},
            "same_size_capped_actor_only_baseline": compact(baseline),
            "deterministic_replay": deterministic,
            "verdict": verdict,
            "untouched_holdout_passed": verdict["untouched_live_gate_passed"],
            "live_confirmation_authorised": verdict["live_confirmation_authorised"],
        }
    )
    return report


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
