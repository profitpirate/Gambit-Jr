#!/usr/bin/env python3
"""Replay the frozen V10 candidate on sealed, strictly later live captures."""

from __future__ import annotations

import argparse
import heapq
import json
import pickle
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_actor_reputation_rules as reputation
import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_causal_regime_veto_position as v10
import e4_v12_profit_survival_search as base
import e4_v12_scale_out_profit_lock as scale_out

SCHEMA_VERSION = "e4-v12-causal-regime-veto-holdout-v1"
PROTOCOL_PATH = Path(
    "artifacts/e4-v12-causal-regime-veto-position-holdout-protocol.json"
)
DEFAULT_MANIFEST_PATH = Path(
    ".tmp-regime-veto-holdout/e4-v12-causal-regime-veto-holdout-manifest.json"
)
DEFAULT_CACHE_PATH = Path(".tmp-regime-veto-holdout/combined-rows.pkl")
DEFAULT_OUTPUT_PATH = Path(
    "artifacts/e4-v12-causal-regime-veto-position-untouched-live.json"
)
MANIFEST_VERSION = "e4-v12-causal-regime-veto-holdout-manifest-v1"
FINAL_ROLE = "untouched_live"
QUARANTINE_ROLE = "quarantine"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object in {path}")
    return value


def verify_protocol(root: Path) -> dict[str, Any]:
    protocol = read_json(root / PROTOCOL_PATH)
    frozen_spec = protocol["frozen_candidate"]
    frozen_path = root / str(frozen_spec["path"])
    if base.sha256_path(frozen_path) != frozen_spec["sha256"]:
        raise ValueError("frozen candidate hash no longer matches the protocol")
    frozen = read_json(frozen_path)
    if frozen["experiment_id"] != protocol["experiment_id"]:
        raise ValueError("protocol and frozen candidate experiment IDs differ")
    if frozen["production_paths_changed"] != 0:
        raise ValueError("frozen candidate changed production paths")
    return protocol


def resolved_path(root: Path, value: Any) -> Path:
    path = Path(str(value))
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"capture path escapes repository: {path}")
    return resolved


def validate_manifest(
    root: Path,
    manifest: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported holdout manifest version")
    if manifest.get("experiment_id") != protocol["experiment_id"]:
        raise ValueError("holdout manifest targets a different experiment")
    captures = [dict(row) for row in manifest.get("captures", [])]
    if not captures:
        raise ValueError("holdout manifest has no captures")
    required_launches = int(
        protocol["final_evidence_contract"]["launches_per_window"]
    )
    freeze_ns = int(protocol["frozen_candidate"]["frozen_at_epoch_ns"])
    seen: set[str] = set()
    for row in captures:
        run_id = str(row.get("run_id", ""))
        role = str(row.get("role", ""))
        if not run_id or run_id in seen:
            raise ValueError(f"invalid or duplicate run_id: {run_id}")
        seen.add(run_id)
        if role not in {QUARANTINE_ROLE, FINAL_ROLE}:
            raise ValueError(f"invalid evidence role for {run_id}: {role}")
        if int(row.get("launches", 0)) != required_launches:
            raise ValueError(f"capture {run_id} does not contain 3000 launches")
        if int(row.get("capture_errors", -1)) != 0:
            raise ValueError(f"capture {run_id} contains capture errors")
        start_ns = int(row.get("capture_start_ns", 0))
        end_ns = int(row.get("capture_end_ns", 0))
        workflow_start_ns = int(row.get("workflow_run_started_at_epoch_ns", 0))
        if start_ns <= 0 or end_ns <= start_ns:
            raise ValueError(f"invalid capture bounds for {run_id}")
        if role == FINAL_ROLE and (
            start_ns <= freeze_ns or workflow_start_ns <= freeze_ns
        ):
            raise ValueError(f"final capture {run_id} did not begin after freeze")
        events_path = resolved_path(root, row.get("events_path", ""))
        if not events_path.is_file():
            raise ValueError(f"events file missing for {run_id}")
        if base.sha256_path(events_path) != row.get("events_sha256"):
            raise ValueError(f"events hash mismatch for {run_id}")
        batch_path = resolved_path(root, row.get("batch_path", ""))
        if not batch_path.is_file():
            raise ValueError(f"batch file missing for {run_id}")
        if base.sha256_path(batch_path) != row.get("batch_sha256"):
            raise ValueError(f"batch hash mismatch for {run_id}")
        row["run_id"] = run_id
        row["role"] = role
        row["events_path"] = str(events_path.relative_to(root)).replace("\\", "/")
        row["batch_path"] = str(batch_path.relative_to(root)).replace("\\", "/")
    captures.sort(key=lambda row: (int(row["capture_start_ns"]), row["run_id"]))
    historical_end_ns = max(
        spec.end_ns for spec in adaptive.capture_specs(root, include_holdout=True)
    )
    if int(captures[0]["capture_start_ns"]) <= historical_end_ns:
        raise ValueError("new evidence overlaps the consumed historical corpus")
    for left, right in pairwise(captures):
        if int(right["capture_start_ns"]) <= int(left["capture_end_ns"]):
            raise ValueError(
                f"captures overlap or are not chronological: {left['run_id']}/{right['run_id']}"
            )
    finals = [row for row in captures if row["role"] == FINAL_ROLE]
    required_windows = int(
        protocol["final_evidence_contract"]["required_capture_windows"]
    )
    if len(finals) > required_windows:
        raise ValueError("manifest contains more than the predeclared five final windows")
    return captures


def future_specs(root: Path, captures: Sequence[Mapping[str, Any]]) -> list[base.CaptureSpec]:
    return [
        base.CaptureSpec(
            run_id=str(row["run_id"]),
            split=str(row["role"]),
            start_ns=int(row["capture_start_ns"]),
            end_ns=int(row["capture_end_ns"]),
            events_path=resolved_path(root, row["events_path"]),
            expected_sha256=str(row["events_sha256"]),
        )
        for row in captures
    ]


def extract_combined_rows(
    root: Path,
    captures: Sequence[Mapping[str, Any]],
) -> tuple[list[base.ResearchRow], dict[tuple[str, str], base.Trace], dict[str, Any]]:
    historical = [
        replace(spec, split="history")
        for spec in adaptive.capture_specs(root, include_holdout=True)
    ]
    future = future_specs(root, captures)
    specs = sorted((*historical, *future), key=lambda spec: (spec.start_ns, int(spec.run_id)))
    frozen = read_json(root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json")
    policy = adaptive.POLICIES[base.integer(frozen["candidate"]["policy_index"])]
    buyer_states: dict[str, actors.ActorState] = {}
    creator_states: dict[str, actors.ActorState] = {}
    pending: list[tuple[int, int, str, tuple[str, ...], float]] = []
    sequence = 0
    creator_counts: Counter[str] = Counter()
    rows: list[base.ResearchRow] = []
    future_traces: dict[tuple[str, str], base.Trace] = {}
    audit_runs = []

    def flush(timestamp_ns: int) -> None:
        while pending and pending[0][0] <= timestamp_ns:
            _, _, creator, buyers, pnl_sol = heapq.heappop(pending)
            creator_states.setdefault(creator, actors.ActorState()).update(pnl_sol)
            for buyer in buyers:
                buyer_states.setdefault(buyer, actors.ActorState()).update(pnl_sol)

    for spec in specs:
        actual = base.sha256_path(spec.events_path)
        if actual != spec.expected_sha256:
            raise ValueError(f"capture hash changed while extracting: {spec.run_id}")
        traces, parse_errors = base.load_capture(spec, creator_counts)
        traces.sort(key=lambda trace: (trace.create_ns, trace.mint))
        counts: Counter[str] = Counter()
        for trace in traces:
            decision_ns = trace.create_ns + actors.HORIZON_MS * 1_000_000
            if decision_ns + actors.RESOLUTION_DELAY_MS * 1_000_000 > spec.end_ns:
                counts["insufficient_tail"] += 1
                continue
            flush(decision_ns)
            snapshot = base.snapshot(trace, actors.HORIZON_MS)
            actor_outcome = adaptive.adaptive_outcome(
                trace,
                actors.HORIZON_MS,
                policy,
            )
            if snapshot is None or actor_outcome is None:
                counts["invalid"] += 1
                continue
            buyers = actors.early_buyers(trace)
            memory = actors.actor_features(
                buyers,
                trace.creator,
                buyer_states,
                creator_states,
            )
            rows.append(
                base.ResearchRow(
                    run_id=spec.run_id,
                    split=spec.split,
                    mint=trace.mint,
                    decision_ns=decision_ns,
                    horizon_ms=actors.HORIZON_MS,
                    features=(*snapshot, *memory),
                    outcomes=(actor_outcome,),
                )
            )
            if spec.split != "history":
                future_traces[(spec.run_id, trace.mint)] = trace
            actor_position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
            actor_pnl = base.net_pnl(actor_outcome, 0.001, actor_position)
            sequence += 1
            heapq.heappush(
                pending,
                (
                    decision_ns + actors.RESOLUTION_DELAY_MS * 1_000_000,
                    sequence,
                    trace.creator,
                    buyers,
                    actor_pnl,
                ),
            )
            counts["rows"] += 1
            counts["rows_with_known_buyer"] += int(memory[1] > 0)
            counts["rows_with_known_creator"] += int(memory[11] > 0)
        audit_runs.append(
            {
                "run_id": spec.run_id,
                "split": spec.split,
                "launches": len(traces),
                "parse_errors": parse_errors,
                "sha256": actual,
                "hash_match": True,
                **dict(counts),
            }
        )
    contextual = actors.add_market_context(rows)
    return contextual, future_traces, {
        "runs": audit_runs,
        "captures": len(audit_runs),
        "launches": sum(row["launches"] for row in audit_runs),
        "rows": len(contextual),
        "parse_errors": sum(row["parse_errors"] for row in audit_runs),
        "future_values_in_features": False,
        "actor_updates_delayed_ms": actors.RESOLUTION_DELAY_MS,
    }


def load_combined_rows(
    root: Path,
    captures: Sequence[Mapping[str, Any]],
    manifest_sha256: str,
    cache_path: Path,
) -> tuple[list[base.ResearchRow], dict[tuple[str, str], base.Trace], dict[str, Any]]:
    if cache_path.exists():
        with cache_path.open("rb") as handle:
            payload = pickle.load(handle)
        if payload.get("manifest_sha256") == manifest_sha256:
            for spec in adaptive.capture_specs(root, include_holdout=True):
                if base.sha256_path(spec.events_path) != spec.expected_sha256:
                    raise ValueError(f"historical capture hash changed: {spec.run_id}")
            return payload["rows"], payload["traces"], payload["audit"]
    rows, traces, audit = extract_combined_rows(root, captures)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as handle:
        pickle.dump(
            {
                "manifest_sha256": manifest_sha256,
                "rows": rows,
                "traces": traces,
                "audit": audit,
            },
            handle,
        )
    return rows, traces, audit


def simulate_exact_latency(
    rows: Sequence[base.ResearchRow],
    traces: Mapping[tuple[str, str], base.Trace],
    latency_ms: int,
) -> dict[str, Any]:
    candidates = sorted(
        rows,
        key=lambda row: (base.integer(row.run_id), row.decision_ns, row.mint),
    )
    bankroll = base.STARTING_BANKROLL_SOL
    active: list[tuple[int, str]] = []
    ledger = []
    rejected_concurrency = 0
    rejected_quote = 0
    attempted_quotes = 0
    for row in candidates:
        active = [item for item in active if item[0] > row.decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        attempted_quotes += 1
        position = bankroll * v10.POSITION_FRACTION
        outcome = scale_out.scale_out_outcome(
            traces[(row.run_id, row.mint)],
            v10.POLICY,
            latency_ms=latency_ms,
            position_sol=position,
        )
        if outcome is None:
            rejected_quote += 1
            continue
        pnl = base.net_pnl(outcome, scale_out.PRIORITY_FEE_SOL, position)
        bankroll += pnl
        exit_ns = row.decision_ns + int(outcome.exit_offset_ms * 1_000_000)
        active.append((exit_ns, row.mint))
        ledger.append(
            {
                "run_id": row.run_id,
                "mint": row.mint,
                "decision_ns": row.decision_ns,
                "latency_ms": latency_ms,
                "position_sol": position,
                "gross_multiple": outcome.gross_multiple,
                "pnl_sol": pnl,
                "win": pnl > 0,
                "exit_reason": outcome.exit_reason,
            }
        )
    profits = [row["pnl_sol"] for row in ledger if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in ledger if row["pnl_sol"] <= 0]
    equity = base.STARTING_BANKROLL_SOL
    peak = equity
    drawdown = 0.0
    for row in ledger:
        equity += row["pnl_sol"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    run_ids = sorted({row["run_id"] for row in ledger}, key=int)
    return {
        "latency_ms": latency_ms,
        "trades": len(ledger),
        "wins": len(profits),
        "win_rate": len(profits) / max(len(ledger), 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(len(profits), len(ledger)),
        "net_pnl_sol": bankroll - base.STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": drawdown / base.STARTING_BANKROLL_SOL,
        "capture_windows": len(run_ids),
        "largest_winner_contribution": max(profits, default=0.0)
        / max(sum(profits), 1e-12),
        "attempted_quotes": attempted_quotes,
        "quote_coverage": len(ledger) / max(attempted_quotes, 1),
        "rejected_quote": rejected_quote,
        "rejected_concurrency": rejected_concurrency,
        "ledger_hash": base.stable_hash(ledger),
        "by_capture_window": {
            run_id: {
                "trades": sum(row["run_id"] == run_id for row in ledger),
                "wins": sum(
                    row["run_id"] == run_id and row["win"] for row in ledger
                ),
                "pnl_sol": sum(
                    row["pnl_sol"] for row in ledger if row["run_id"] == run_id
                ),
            }
            for run_id in run_ids
        },
        "exit_reasons": dict(Counter(row["exit_reason"] for row in ledger)),
        "ledger": ledger,
    }


def golden_gate(
    economics: Mapping[str, Mapping[str, Any]],
    no_veto_baseline: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    zero = economics["0"]
    gate = protocol["golden_gate"]
    windows = list(zero["by_capture_window"].values())
    requirements = {
        "minimum_closed_trades": zero["trades"] >= gate["minimum_closed_trades"],
        "all_five_windows_represented": zero["capture_windows"]
        == gate["required_capture_windows_represented"],
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
        "beats_same_size_no_veto_baseline_pnl": zero["net_pnl_sol"]
        > no_veto_baseline["net_pnl_sol"],
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
        "production_paths_changed": 0,
    }


def select_rows(
    rows: Sequence[base.ResearchRow],
    captures: Sequence[Mapping[str, Any]],
    rule: reputation.BuyerRule,
) -> tuple[dict[str, list[base.ResearchRow]], list[dict[str, Any]]]:
    actor_rows = [row for row in rows if reputation.buyer_matches(row, rule)]
    selected: dict[str, list[base.ResearchRow]] = {}
    audit = []
    for capture in captures:
        run_id = str(capture["run_id"])
        prior = [
            row
            for row in actor_rows
            if row.decision_ns < int(capture["capture_start_ns"])
        ]
        current = [row for row in actor_rows if row.run_id == run_id]
        learned = {veto.key: v10.threshold(prior, veto) for veto in v10.VETOES}
        chosen = [
            row
            for row in current
            if all(v10.passes(row, veto, learned[veto.key]) for veto in v10.VETOES)
        ]
        selected[run_id] = chosen
        audit.append(
            {
                "run_id": run_id,
                "role": capture["role"],
                "prior_actor_rows": len(prior),
                "current_actor_rows": len(current),
                "selected_rows": len(chosen),
                "thresholds": learned,
            }
        )
    return selected, audit


def compact(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "ledger"}


def run(
    root: Path,
    manifest_path: Path,
    cache_path: Path,
) -> dict[str, Any]:
    protocol = verify_protocol(root)
    manifest = read_json(manifest_path)
    captures = validate_manifest(root, manifest, protocol)
    manifest_sha = base.sha256_path(manifest_path)
    rows, traces, data_audit = load_combined_rows(
        root,
        captures,
        manifest_sha,
        cache_path,
    )
    allowed_parse_errors = int(
        protocol["final_evidence_contract"]["parse_errors_allowed"]
    )
    if data_audit["parse_errors"] > allowed_parse_errors:
        raise ValueError("combined evidence contains parse errors")
    frozen = read_json(
        root / "artifacts/e4-v12-causal-regime-veto-position-frozen-candidate.json"
    )
    rule = reputation.BuyerRule(**frozen["identity"]["full_parameters"]["buyer_rule"])
    selected, selection_audit = select_rows(rows, captures, rule)
    quarantine_ids = [
        str(row["run_id"]) for row in captures if row["role"] == QUARANTINE_ROLE
    ]
    quarantine_rows = [row for run_id in quarantine_ids for row in selected[run_id]]
    quarantine_economics = {
        str(latency): simulate_exact_latency(quarantine_rows, traces, latency)
        for latency in base.LATENCIES_MS
    }
    quarantine_repeat = {
        str(latency): simulate_exact_latency(quarantine_rows, traces, latency)[
            "ledger_hash"
        ]
        for latency in base.LATENCIES_MS
    }
    quarantine_deterministic = all(
        quarantine_economics[key]["ledger_hash"] == quarantine_repeat[key]
        for key in quarantine_economics
    )
    if not quarantine_deterministic:
        raise ValueError("quarantine replay is not deterministic")
    final_ids = [
        str(row["run_id"]) for row in captures if row["role"] == FINAL_ROLE
    ]
    final_rows = [row for run_id in final_ids for row in selected[run_id]]
    final_actor_rows = [
        row
        for row in rows
        if row.run_id in set(final_ids) and reputation.buyer_matches(row, rule)
    ]
    required_windows = int(
        protocol["final_evidence_contract"]["required_capture_windows"]
    )
    final_ready = len(final_ids) == required_windows
    economics: dict[str, Any] = {}
    baseline: dict[str, Any] = {}
    verdict: dict[str, Any] = {
        "status": "WAITING_FOR_FIVE_STRICTLY_LATER_WINDOWS",
        "untouched_live_gate_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }
    deterministic = False
    if final_ready:
        economics = {
            str(latency): simulate_exact_latency(final_rows, traces, latency)
            for latency in base.LATENCIES_MS
        }
        baseline = simulate_exact_latency(final_actor_rows, traces, 0)
        repeat = {
            str(latency): simulate_exact_latency(final_rows, traces, latency)[
                "ledger_hash"
            ]
            for latency in base.LATENCIES_MS
        }
        deterministic = all(
            economics[key]["ledger_hash"] == repeat[key] for key in economics
        )
        if not deterministic:
            raise ValueError("untouched replay is not deterministic")
        verdict = golden_gate(economics, baseline, protocol)
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": protocol["experiment_id"],
        "protocol_sha256": base.sha256_path(root / PROTOCOL_PATH),
        "manifest_sha256": manifest_sha,
        "captures": captures,
        "final_capture_windows": len(final_ids),
        "final_total_launches": sum(
            int(row["launches"]) for row in captures if row["role"] == FINAL_ROLE
        ),
        "data_audit": data_audit,
        "selection_audit": selection_audit,
        "quarantine": {
            "capture_windows": len(quarantine_ids),
            "may_count_toward_final_gate": False,
            "may_change_frozen_candidate": False,
            "deterministic_replay": quarantine_deterministic,
            "latencies": {
                key: compact(value) for key, value in quarantine_economics.items()
            },
        },
        "latencies": {key: compact(value) for key, value in economics.items()},
        "same_size_no_veto_baseline": compact(baseline) if baseline else {},
        "deterministic_replay": deterministic,
        "verdict": verdict,
        "untouched_holdout_passed": verdict["untouched_live_gate_passed"],
        "live_confirmation_authorised": verdict["live_confirmation_authorised"],
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
