#!/usr/bin/env python3
"""Freeze the passing online-conformal development candidate for new evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import e4_v12_profit_survival_search as base

DEVELOPMENT = Path("artifacts/e4-v12-online-conformal-development.json")
LATENCY = Path("artifacts/e4-v12-online-conformal-latency.json")
ABLATION = Path("artifacts/e4-v12-online-conformal-ablation.json")
FROZEN = Path("artifacts/e4-v12-online-conformal-frozen-candidate.json")
PROTOCOL = Path("artifacts/e4-v12-online-conformal-holdout-protocol.json")
MANIFEST = Path("artifacts/e4-v12-online-conformal-holdout-manifest.json")
SOURCE_COMMIT = "c38b929711ccb5a715f2f17162d994b96f614368"
CONSUMED_EVIDENCE_END_NS = 1_789_445_163_804_856_012


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object in {path}")
    return value


def sha256_lf(path: Path) -> str:
    return __import__("hashlib").sha256(
        path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def commit_timestamp_ns(commit: str) -> int:
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%ct", commit],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(completed.stdout.strip()) * 1_000_000_000


def main() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    development = read_json(DEVELOPMENT)
    latency = read_json(LATENCY)
    ablation = read_json(ABLATION)
    experiment_id = str(development["experiment_id"])
    if {latency["experiment_id"], ablation["experiment_id"]} != {experiment_id}:
        raise ValueError("development, latency, and ablation identities differ")
    if not development["development_gate_passed"]:
        raise ValueError("development gate did not pass")
    if not latency["exact_trace_gate_passed"]:
        raise ValueError("exact trace and latency gate did not pass")
    if not ablation["ablation_gate_passed"]:
        raise ValueError("ablation gate did not pass")
    if any(
        report.get("production_paths_changed") != 0
        or report.get("production_promotion_authorised") is not False
        or report.get("production_deployment_authorised") is not False
        for report in (development, latency, ablation)
    ):
        raise ValueError("research evidence contains production authority")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != SOURCE_COMMIT:
        raise ValueError(f"freeze must run from development commit {SOURCE_COMMIT}, found {head}")
    frozen_at_ns = max(commit_timestamp_ns(SOURCE_COMMIT), CONSUMED_EVIDENCE_END_NS + 1)
    frozen = {
        "version": "e4-v12-online-conformal-frozen-candidate-v1",
        "experiment_id": experiment_id,
        "identity": development["identity"],
        "source_commit": SOURCE_COMMIT,
        "source_artifacts": {
            str(DEVELOPMENT).replace("\\", "/"): sha256_lf(DEVELOPMENT),
            str(LATENCY).replace("\\", "/"): sha256_lf(LATENCY),
            str(ABLATION).replace("\\", "/"): sha256_lf(ABLATION),
        },
        "candidate": {
            "development": development["aggregate"],
            "latencies": latency["latencies"],
            "ablation_requirements": ablation["requirements"],
            "policy_index": development["policy_index"],
            "policy": development["policy"],
            "score_fraction": development["score_fraction"],
            "rolling_history_rows": development["rolling_history_rows"],
            "threshold_refresh_rows": development["threshold_refresh_rows"],
            "model": development["model"],
        },
        "frozen_at_epoch_ns": frozen_at_ns,
        "consumed_evidence_end_ns": CONSUMED_EVIDENCE_END_NS,
        "holdout_status": "strictly later evidence required",
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }
    base.write_json(FROZEN, frozen)
    frozen_sha = sha256_lf(FROZEN)
    protocol = {
        "version": "e4-v12-online-conformal-holdout-protocol-v1",
        "experiment_id": experiment_id,
        "frozen_candidate": {
            "path": str(FROZEN).replace("\\", "/"),
            "sha256": frozen_sha,
            "source_commit": SOURCE_COMMIT,
            "frozen_at_epoch_ns": frozen_at_ns,
        },
        "consumed_development_evidence": {
            "capture_windows": 64,
            "launches": 192_000,
            "last_capture_run_id": "34918286614",
            "last_capture_end_ns": CONSUMED_EVIDENCE_END_NS,
            "may_never_be_relabelled_as_untouched": True,
        },
        "final_evidence_contract": {
            "role": "untouched_live",
            "required_capture_windows": 10,
            "launches_per_window": 3_000,
            "required_total_launches": 30_000,
            "every_capture_starts_after_consumed_evidence_epoch_ns": CONSUMED_EVIDENCE_END_NS,
            "every_workflow_and_capture_starts_after_frozen_at_epoch_ns": frozen_at_ns,
            "captures_must_not_overlap": True,
            "capture_errors_required": 0,
            "hypothesis_only_required": True,
            "mainnet_transactions_sent_required": 0,
            "mainnet_funds_risked_sol_required": 0.0,
            "optional_stopping_allowed": False,
        },
        "golden_gate": {
            "minimum_closed_trades": 50,
            "minimum_capture_windows_represented": 8,
            "minimum_profitable_capture_windows": 7,
            "minimum_win_rate": 0.65,
            "minimum_wilson_95_lower_bound": 0.55,
            "minimum_net_pnl_sol": 0.0,
            "minimum_profit_factor": 1.25,
            "maximum_drawdown_fraction": 0.15,
            "maximum_largest_winner_contribution": 0.25,
            "every_latency_net_pnl_positive": True,
            "every_latency_minimum_profit_factor": 1.25,
            "every_latency_quote_coverage": 1.0,
        },
        "result_policy": {
            "final_gate_is_evaluated_once": True,
            "deterministic_replay_required": True,
            "sentinel_cannot_declare_found": True,
            "untouched_live_pass_required_to_declare_found": True,
            "production_promotion_authorised": False,
            "production_deployment_authorised": False,
            "production_paths_changed": 0,
        },
    }
    base.write_json(PROTOCOL, protocol)
    manifest = {
        "version": "e4-v12-online-conformal-holdout-manifest-v1",
        "experiment_id": experiment_id,
        "protocol_sha256_lf": sha256_lf(PROTOCOL),
        "captures": [],
        "production_paths_changed": 0,
    }
    base.write_json(MANIFEST, manifest)
    print(json.dumps({"frozen": frozen, "protocol": protocol, "manifest": manifest}, indent=2))
    return frozen, protocol, manifest


if __name__ == "__main__":
    main()
