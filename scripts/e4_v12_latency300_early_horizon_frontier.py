#!/usr/bin/env python3
"""Measure the exact-300 ms opportunity frontier at earlier decision horizons.

This is a label-only diagnostic.  It does not fit, select, freeze, or promote a
candidate.  Every comparison uses the same immutable development population,
exit policy, fee model, and 300 ms decision-to-fill delay; only the causal
decision horizon (and therefore the launch-relative fill time) changes.
"""

from __future__ import annotations

import json
import pickle
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_online_conformal_precision as conformal
import e4_v12_profit_survival_search as base

OUTPUT = Path("artifacts/e4-v12-latency300-early-horizon-frontier.json")
SCHEMA_VERSION = "e4-v12-latency300-early-horizon-frontier-v1"
DIAGNOSTIC_FAMILY = "latency300-causal-early-decision-frontier-v12"
DECISION_HORIZONS_MS = (0, 25, 50, 100, 150, 200, 250)
LATENCY_MS = 300
REFERENCE_HORIZON_MS = 250
PARSE_WORKERS = 2
SOURCE_FILES = (
    "scripts/e4_v12_latency300_early_horizon_frontier.py",
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


def empty_horizon() -> dict[str, Any]:
    return {
        "population_rows": 0,
        "valid_snapshot_rows": 0,
        "executable_rows": 0,
        "positive_rows": 0,
        "net_pnl_sol": 0.0,
        "gross_profit_sol": 0.0,
        "gross_loss_sol": 0.0,
    }


def evaluate_trace(trace: base.Trace) -> dict[int, float | None]:
    """Return exact nominal PnL for every causal decision horizon."""
    policy = adaptive.POLICIES[conformal.POLICY_INDEX]
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    output: dict[int, float | None] = {}
    for horizon_ms in DECISION_HORIZONS_MS:
        if base.snapshot(trace, horizon_ms) is None:
            output[horizon_ms] = None
            continue
        outcome = adaptive.adaptive_outcome(
            trace,
            horizon_ms,
            policy,
            latency_ms=LATENCY_MS,
        )
        output[horizon_ms] = (
            base.net_pnl(outcome, 0.001, position)
            if outcome is not None
            else None
        )
    return output


def summarize_traces(
    traces: list[base.Trace], row_lookup: set[tuple[str, str]]
) -> dict[str, Any]:
    by_horizon = {horizon: empty_horizon() for horizon in DECISION_HORIZONS_MS}
    transitions = {
        horizon: Counter() for horizon in DECISION_HORIZONS_MS if horizon != 250
    }
    eligible = 0
    for trace in traces:
        key = (str(trace.run_id), str(trace.mint))
        if key not in row_lookup:
            continue
        eligible += 1
        values = evaluate_trace(trace)
        reference = values[REFERENCE_HORIZON_MS]
        for horizon, pnl in values.items():
            summary = by_horizon[horizon]
            summary["population_rows"] += 1
            if base.snapshot(trace, horizon) is None:
                continue
            summary["valid_snapshot_rows"] += 1
            if pnl is None:
                continue
            summary["executable_rows"] += 1
            summary["positive_rows"] += int(pnl > 0)
            summary["net_pnl_sol"] += pnl
            if pnl > 0:
                summary["gross_profit_sol"] += pnl
            else:
                summary["gross_loss_sol"] += pnl
            if horizon == REFERENCE_HORIZON_MS or reference is None:
                continue
            category = (
                f"{'win' if reference > 0 else 'loss'}_to_"
                f"{'win' if pnl > 0 else 'loss'}"
            )
            transitions[horizon][category] += 1
    return {
        "eligible_rows": eligible,
        "by_horizon": by_horizon,
        "transitions": transitions,
    }


def parse_spec(
    spec: base.CaptureSpec, row_lookup: set[tuple[str, str]]
) -> dict[str, Any]:
    actual_sha256 = base.sha256_path(spec.events_path)
    if actual_sha256 != spec.expected_sha256:
        raise ValueError(f"capture hash changed: {spec.run_id}")
    traces, parse_errors = base.load_capture(spec, Counter())
    if parse_errors:
        raise ValueError(f"capture {spec.run_id} has {parse_errors} parse errors")
    result = summarize_traces(traces, row_lookup)
    return {
        "run_id": spec.run_id,
        "sha256": actual_sha256,
        "launches": len(traces),
        "parse_errors": parse_errors,
        **result,
    }


def merge_horizon(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in target:
        target[key] += source[key]


def finalize_horizon(horizon: int, row: dict[str, Any]) -> dict[str, Any]:
    executable = int(row["executable_rows"])
    positives = int(row["positive_rows"])
    loss = float(row["gross_loss_sol"])
    return {
        **row,
        "decision_horizon_ms": horizon,
        "launch_relative_fill_ms": horizon + LATENCY_MS,
        "snapshot_coverage": row["valid_snapshot_rows"]
        / max(row["population_rows"], 1),
        "quote_coverage": executable / max(row["valid_snapshot_rows"], 1),
        "positive_rate": positives / max(executable, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(positives, executable),
        "mean_pnl_sol": row["net_pnl_sol"] / max(executable, 1),
        "oracle_profit_factor": row["gross_profit_sol"]
        / max(abs(loss), 1e-12),
    }


def main() -> dict[str, Any]:
    root = Path.cwd()
    with (root / conformal.CACHE).open("rb") as handle:
        payload = pickle.load(handle)
    rows = payload["rows"]
    row_lookup = {(str(row.run_id), str(row.mint)) for row in rows}
    if len(row_lookup) != len(rows):
        raise ValueError("development population does not have unique run/mint keys")

    specs = adaptive.capture_specs(root, include_holdout=True)
    source_runs = {spec.run_id for spec in specs}
    audits: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=PARSE_WORKERS) as executor:
        futures = {
            executor.submit(parse_spec, spec, row_lookup): spec.run_id for spec in specs
        }
        for number, future in enumerate(as_completed(futures), 1):
            audit = future.result()
            audits.append(audit)
            print(
                f"parsed raw capture {number}/{len(specs)} run={audit['run_id']} "
                f"eligible={audit['eligible_rows']}",
                flush=True,
            )

    cached_traces = payload.get("traces")
    if not isinstance(cached_traces, dict):
        raise TypeError("combined cache does not contain later traces")
    later = [
        trace
        for trace in cached_traces.values()
        if str(trace.run_id) not in source_runs
    ]
    later_by_run: dict[str, list[base.Trace]] = {}
    for trace in later:
        later_by_run.setdefault(str(trace.run_id), []).append(trace)
    for run_id, traces in later_by_run.items():
        result = summarize_traces(traces, row_lookup)
        audits.append(
            {
                "run_id": run_id,
                "sha256": "bound-by-combined-cache-manifest",
                "launches": len(traces),
                "parse_errors": 0,
                **result,
            }
        )

    # Worker completion order is intentionally nondeterministic.  Canonicalize
    # before floating-point reduction so byte-identical evidence is produced.
    audits.sort(key=lambda row: int(row["run_id"]))

    totals = {horizon: empty_horizon() for horizon in DECISION_HORIZONS_MS}
    transitions = {
        horizon: Counter() for horizon in DECISION_HORIZONS_MS if horizon != 250
    }
    for audit in audits:
        for horizon in DECISION_HORIZONS_MS:
            merge_horizon(totals[horizon], audit["by_horizon"][horizon])
        for horizon, counts in audit["transitions"].items():
            transitions[horizon].update(counts)

    population_coverage = sum(audit["eligible_rows"] for audit in audits)
    if population_coverage != len(rows):
        raise ValueError(
            f"frontier covered {population_coverage}/{len(rows)} development rows"
        )
    frontier = [finalize_horizon(horizon, totals[horizon]) for horizon in DECISION_HORIZONS_MS]
    best = max(frontier, key=lambda row: (row["positive_rate"], row["mean_pnl_sol"]))
    identity = {
        "diagnostic_family": DIAGNOSTIC_FAMILY,
        "source_code_fingerprint": source_code_fingerprint(root),
        "dataset_source_manifest_fingerprint": payload["manifest_sha256"],
        "evidence_epoch": sorted({str(row.run_id) for row in rows}, key=int),
        "decision_horizons_ms": list(DECISION_HORIZONS_MS),
        "latency_ms": LATENCY_MS,
        "reference_horizon_ms": REFERENCE_HORIZON_MS,
        "exit_policy": adaptive.POLICIES[conformal.POLICY_INDEX].key,
        "fee_model": {
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_transaction_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
        },
        "population_policy": "exact immutable combined development run/mint population",
    }
    output = {
        "version": SCHEMA_VERSION,
        "diagnostic_id": f"e4d-{base.stable_hash(identity)}",
        "identity": identity,
        "rows": len(rows),
        "windows": len(audits),
        "raw_capture_runs": len(specs),
        "raw_capture_bytes": sum(spec.events_path.stat().st_size for spec in specs),
        "cached_later_runs": len(later_by_run),
        "raw_source_hashes_verified": True,
        "frontier": frontier,
        "reference_transitions": {
            str(horizon): dict(counts)
            for horizon, counts in sorted(transitions.items())
        },
        "best_descriptive_horizon_ms": best["decision_horizon_ms"],
        "finding": (
            "Earlier launch-relative execution has enough descriptive advantage to "
            "warrant a separately registered causal early-decision model."
            if best["decision_horizon_ms"] != REFERENCE_HORIZON_MS
            and best["positive_rate"]
            > next(
                row["positive_rate"]
                for row in frontier
                if row["decision_horizon_ms"] == REFERENCE_HORIZON_MS
            )
            else "Earlier launch-relative execution did not improve the exact-300 ms opportunity frontier."
        ),
        "candidate_warranted": bool(
            best["decision_horizon_ms"] != REFERENCE_HORIZON_MS
            and best["positive_rate"]
            > next(
                row["positive_rate"]
                for row in frontier
                if row["decision_horizon_ms"] == REFERENCE_HORIZON_MS
            )
        ),
        "candidate_fitted": False,
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
