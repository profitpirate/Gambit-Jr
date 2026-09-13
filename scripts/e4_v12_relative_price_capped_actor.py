#!/usr/bin/env python3
"""Develop V11: causal actor quality, relative strength, and capped exposure.

All 54 source windows are already-consumed development evidence.  Thresholds
are learned from strictly earlier windows in each chronological fold.  The
result can freeze a candidate for new evidence, but can never promote or alter
production V12.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import e4_v12_actor_reputation_rules as reputation
import e4_v12_causal_actor_memory as actors
import e4_v12_causal_regime_veto_holdout as prior_holdout
import e4_v12_causal_regime_veto_position as prior_v10
import e4_v12_profit_survival_search as base
import e4_v12_scale_out_profit_lock as scale_out
import numpy as np

SCHEMA_VERSION = "e4-v12-relative-price-capped-actor-v11"
THESIS_FAMILY = "causal-relative-price-capped-actor-v11"
SOURCE_MANIFEST = Path(
    "artifacts/e4-v12-relative-price-capped-actor-source-manifest.json"
)
CONSUMED_MANIFEST = Path(
    ".tmp-regime-veto-holdout/e4-v12-causal-regime-veto-holdout-manifest.json"
)
COMBINED_CACHE = Path(".tmp-regime-veto-holdout/combined-rows.pkl")
TRACE_CACHE = Path(".tmp-regime-veto-holdout/actor-scorecard-traces.pkl")
DEVELOPMENT_OUTPUT = Path(
    "artifacts/e4-v12-relative-price-capped-actor-development.json"
)
FROZEN_OUTPUT = Path(
    "artifacts/e4-v12-relative-price-capped-actor-frozen-candidate.json"
)

ACTOR_RULE = reputation.BuyerRule(1, 0.0, 5, 0.60, 0.010, 0.50)
REGIME_FEATURE = "relative_price_multiple"
REGIME_QUANTILE = 0.05
POLICY = scale_out.ScaleOutPolicy(1.20, 0.35, 3.0, 1.25, 0.25)
POSITION_FRACTION = 0.04
POSITION_CAP_SOL = base.STARTING_BANKROLL_SOL * POSITION_FRACTION
FOLD_TRAIN_ENDS = (24, 34, 44)
FOLD_WINDOWS = 10
LATENCIES_MS = (0, 1, 2, 5, 10)
MARKET_VETO_CONFIGURATIONS_SCREENED = 4_240
ACTOR_EXIT_CONFIGURATIONS_SCREENED = 9_000


def sha256_lf(path: Path) -> str:
    """Hash text reproducibly across LF and CRLF Git checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object in {path}")
    return value


def verify_source_manifest(
    root: Path, consumed: Mapping[str, Any]
) -> dict[str, Any]:
    source = read_json(root / SOURCE_MANIFEST)
    if source.get("version") != (
        "e4-v12-relative-price-capped-actor-source-manifest-v1"
    ):
        raise ValueError("unsupported V11 source manifest")
    parent = source["parent_manifest"]
    if sha256_lf(root / str(parent["path"])) != parent["sha256_lf"]:
        raise ValueError("parent development manifest fingerprint changed")
    expected = {
        str(row["run_id"]): row for row in source["supplemental_captures"]
    }
    actual = {str(row["run_id"]): row for row in consumed.get("captures", [])}
    if set(expected) != set(actual):
        raise ValueError("consumed supplemental capture set changed")
    fields = (
        "capture_start_ns",
        "capture_end_ns",
        "events_sha256",
        "batch_sha256",
        "launches",
        "capture_errors",
    )
    for run_id, expected_row in expected.items():
        if any(actual[run_id].get(field) != expected_row[field] for field in fields):
            raise ValueError(f"consumed capture fingerprint changed: {run_id}")
    if source["totals"] != {
        "captures": 54,
        "launches": 162000,
        "supplemental_captures": 6,
        "supplemental_launches": 18000,
    }:
        raise ValueError("unexpected V11 source totals")
    if source.get("production_paths_changed") != 0:
        raise ValueError("source manifest indicates a production change")
    return source


def load_consumed_rows(
    root: Path,
) -> tuple[
    list[base.ResearchRow],
    dict[tuple[str, str], base.Trace],
    dict[str, Any],
    dict[str, Any],
]:
    protocol = prior_holdout.verify_protocol(root)
    manifest_path = root / CONSUMED_MANIFEST
    manifest = read_json(manifest_path)
    captures = prior_holdout.validate_manifest(root, manifest, protocol)
    source = verify_source_manifest(root, manifest)
    rows, future_traces, audit = prior_holdout.load_combined_rows(
        root,
        captures,
        base.sha256_path(manifest_path),
        root / COMBINED_CACHE,
    )
    windows = sorted({row.run_id for row in rows}, key=base.integer)
    if len(windows) != 54 or audit["captures"] != 54:
        raise ValueError("V11 requires exactly 54 consumed chronological windows")
    if audit["launches"] != 162_000 or audit["parse_errors"] != 0:
        raise ValueError("V11 source evidence is incomplete or contains parse errors")
    if audit["future_values_in_features"] is not False:
        raise ValueError("point-in-time feature audit failed")
    actor_rows = [row for row in rows if reputation.buyer_matches(row, ACTOR_RULE)]
    future_ids = {str(row["run_id"]) for row in captures}
    historical = [row for row in actor_rows if row.run_id not in future_ids]
    historical_traces, trace_audit = prior_v10.load_traces(
        root,
        historical,
        root / TRACE_CACHE,
    )
    traces = historical_traces | future_traces
    missing = {(row.run_id, row.mint) for row in actor_rows} - set(traces)
    if missing:
        raise ValueError(f"actor traces missing: {sorted(missing)[:3]}")
    evidence_audit = {
        **audit,
        "trace_audit": trace_audit,
        "actor_rows": len(actor_rows),
        "actor_trace_keys": len(traces),
    }
    return rows, traces, evidence_audit, source


def regime_threshold(rows: Sequence[base.ResearchRow]) -> float:
    index = actors.FEATURE_NAMES.index(REGIME_FEATURE)
    values = np.asarray([row.features[index] for row in rows], dtype=float)
    if not len(values) or not np.isfinite(values).all():
        raise ValueError("invalid causal relative-price threshold source")
    return float(np.quantile(values, REGIME_QUANTILE))


def passes_regime(row: base.ResearchRow, threshold: float) -> bool:
    index = actors.FEATURE_NAMES.index(REGIME_FEATURE)
    return bool(row.features[index] >= threshold)


def simulate_capped_latency(
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
    ledger: list[dict[str, Any]] = []
    rejected_concurrency = 0
    rejected_quote = 0
    attempted_quotes = 0
    for row in candidates:
        active = [item for item in active if item[0] > row.decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        position = min(bankroll * POSITION_FRACTION, POSITION_CAP_SOL)
        if position <= 0:
            raise ValueError("bankroll exhausted during capped replay")
        attempted_quotes += 1
        outcome = scale_out.scale_out_outcome(
            traces[(row.run_id, row.mint)],
            POLICY,
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
    run_ids = sorted({str(row["run_id"]) for row in ledger}, key=int)
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


def compact(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "ledger"}


def aggregate(folds: Sequence[Mapping[str, Any]], candidate: str) -> dict[str, Any]:
    metrics = [fold["candidates"][candidate] for fold in folds]
    trades = sum(row["trades"] for row in metrics)
    wins = sum(row["wins"] for row in metrics)
    profits = [
        item["pnl_sol"]
        for row in metrics
        for item in row["ledger"]
        if item["pnl_sol"] > 0
    ]
    losses = [
        item["pnl_sol"]
        for row in metrics
        for item in row["ledger"]
        if item["pnl_sol"] <= 0
    ]
    window_rows = [
        value for row in metrics for value in row["by_capture_window"].values()
    ]
    return {
        "candidate": candidate,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": sum(row["net_pnl_sol"] for row in metrics),
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "positive_folds": sum(row["net_pnl_sol"] > 0 for row in metrics),
        "minimum_fold_profit_factor": min(row["profit_factor"] for row in metrics),
        "minimum_fold_win_rate": min(row["win_rate"] for row in metrics),
        "capture_windows": sum(row["capture_windows"] for row in metrics),
        "positive_capture_windows": sum(row["pnl_sol"] > 0 for row in window_rows),
        "maximum_fold_drawdown_fraction": max(
            row["maximum_drawdown_fraction"] for row in metrics
        ),
        "largest_winner_contribution": max(profits, default=0.0)
        / max(sum(profits), 1e-12),
        "ledger_hash": base.stable_hash([row["ledger_hash"] for row in metrics]),
        "folds": [compact(row) for row in metrics],
    }


def run(root: Path) -> dict[str, Any]:
    rows, traces, data_audit, source_manifest = load_consumed_rows(root)
    windows = sorted({row.run_id for row in rows}, key=base.integer)
    folds: list[dict[str, Any]] = []
    for train_end in FOLD_TRAIN_ENDS:
        train_ids = set(windows[:train_end])
        validation_ids = set(windows[train_end : train_end + FOLD_WINDOWS])
        train = [
            row
            for row in rows
            if row.run_id in train_ids and reputation.buyer_matches(row, ACTOR_RULE)
        ]
        validation = [
            row
            for row in rows
            if row.run_id in validation_ids
            and reputation.buyer_matches(row, ACTOR_RULE)
        ]
        learned = regime_threshold(train)
        selected = [row for row in validation if passes_regime(row, learned)]
        actor_only = simulate_capped_latency(validation, traces, 0)
        veto_latencies = {
            str(latency): simulate_capped_latency(selected, traces, latency)
            for latency in LATENCIES_MS
        }
        repeat = {
            str(latency): simulate_capped_latency(selected, traces, latency)[
                "ledger_hash"
            ]
            for latency in LATENCIES_MS
        }
        if any(
            veto_latencies[key]["ledger_hash"] != repeat[key]
            for key in veto_latencies
        ):
            raise ValueError("capped replay is not deterministic")
        folds.append(
            {
                "train_windows": list(windows[:train_end]),
                "validation_windows": list(
                    windows[train_end : train_end + FOLD_WINDOWS]
                ),
                "train_actor_rows": len(train),
                "validation_actor_rows": len(validation),
                "relative_price_threshold": learned,
                "candidates": {
                    "capped_actor_only": actor_only,
                    "capped_actor_relative_price_veto": veto_latencies["0"],
                },
                "veto_latencies": {
                    key: compact(value) for key, value in veto_latencies.items()
                },
            }
        )
    selected = aggregate(folds, "capped_actor_relative_price_veto")
    baseline = aggregate(folds, "capped_actor_only")
    latency_requirements = all(
        metrics["net_pnl_sol"] > 0
        and metrics["profit_factor"] >= 1.25
        and metrics["quote_coverage"] == 1.0
        for fold in folds
        for metrics in fold["veto_latencies"].values()
    )
    requirements = {
        "minimum_closed_trades": selected["trades"] >= 300,
        "minimum_win_rate": selected["win_rate"] >= 0.65,
        "minimum_wilson_bound": selected["wilson_95_lower_bound"] >= 0.60,
        "positive_net_pnl": selected["net_pnl_sol"] > 0,
        "minimum_profit_factor": selected["profit_factor"] >= 1.25,
        "all_folds_profitable": selected["positive_folds"] == len(FOLD_TRAIN_ENDS),
        "minimum_fold_profit_factor": selected["minimum_fold_profit_factor"]
        >= 1.25,
        "minimum_fold_win_rate": selected["minimum_fold_win_rate"] >= 0.65,
        "minimum_profitable_windows": selected["positive_capture_windows"] >= 22,
        "maximum_drawdown": selected["maximum_fold_drawdown_fraction"] <= 0.15,
        "largest_winner_limit": selected["largest_winner_contribution"] <= 0.15,
        "every_fold_latency_gate": latency_requirements,
    }
    ablation = {
        "win_rate_delta": selected["win_rate"] - baseline["win_rate"],
        "profit_factor_delta": selected["profit_factor"] - baseline["profit_factor"],
        "pnl_delta_sol": selected["net_pnl_sol"] - baseline["net_pnl_sol"],
        "positive_capture_windows_delta": selected["positive_capture_windows"]
        - baseline["positive_capture_windows"],
    }
    ablation_requirements = {
        "improves_win_rate": ablation["win_rate_delta"] > 0,
        "improves_profit_factor": ablation["profit_factor_delta"] > 0,
        "improves_pnl": ablation["pnl_delta_sol"] > 0,
        "improves_time_coverage": ablation["positive_capture_windows_delta"] > 0,
    }
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": sha256_lf(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.stable_hash(source_manifest),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "interpretable buyer reputation plus causal relative-price lower-tail veto",
        "full_parameters": {
            "actor_rule": asdict(ACTOR_RULE),
            "regime_feature": REGIME_FEATURE,
            "regime_quantile": REGIME_QUANTILE,
            "regime_keep": "at_or_above",
            "position_fraction": POSITION_FRACTION,
            "position_cap_sol": POSITION_CAP_SOL,
            "scale_out_policy": asdict(POLICY),
        },
        "causal_horizon_ms": actors.HORIZON_MS,
        "candidate_risk_set_policy": {
            "actor_rule": ACTOR_RULE.key,
            "threshold_source": "actor-rule matches in strictly earlier windows only",
            "relative_price_rule": "keep values at or above the earlier-window fifth percentile",
        },
        "chronological_split": [
            {
                "train": fold["train_windows"],
                "validation": fold["validation_windows"],
            }
            for fold in folds
        ],
        "bankroll_sol": base.STARTING_BANKROLL_SOL,
        "position_sizing": (
            "min(current bankroll * 4%, initial 3 SOL bankroll * 4%); no upward compounding"
        ),
        "fee_model": {
            "priority_fee_sol_per_transaction": scale_out.PRIORITY_FEE_SOL,
            "tip_sol_per_transaction": base.TIP_SOL,
            "base_fee_sol_per_transaction": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "additional_scale_out_transaction_charged": True,
        },
        "output_guard": "60-second actor maturity delay; causal relative context; exact reserve re-quotes",
        "latency_assumptions_ms": list(LATENCIES_MS),
        "execution_policy": "paper replay; capped exposure; maximum two concurrent positions",
        "exit_policy": asdict(POLICY),
    }
    ready = all(requirements.values()) and all(ablation_requirements.values())
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "search_disclosure": {
            "actor_rules_available": len(reputation.BUYER_RULES),
            "actor_exit_configurations_screened": ACTOR_EXIT_CONFIGURATIONS_SCREENED,
            "market_veto_configurations_screened": MARKET_VETO_CONFIGURATIONS_SCREENED,
            "latest_ten_windows_status": "consumed development diagnostic; never untouched",
        },
        "data_audit": data_audit,
        "folds": [
            {
                key: value
                for key, value in fold.items()
                if key != "candidates"
            }
            | {
                "candidates": {
                    key: compact(value)
                    for key, value in fold["candidates"].items()
                }
            }
            for fold in folds
        ],
        "selected_candidate": "capped_actor_relative_price_veto",
        "winner": selected,
        "same_size_capped_actor_only_baseline": baseline,
        "development_requirements": requirements,
        "ablation": ablation,
        "ablation_requirements": ablation_requirements,
        "development_gate_passed": all(requirements.values()),
        "ablation_gate_passed": all(ablation_requirements.values()),
        "ready_for_strictly_later_evidence": ready,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_deployment_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    development_path = root / DEVELOPMENT_OUTPUT
    base.write_json(development_path, report)
    if report["ready_for_strictly_later_evidence"]:
        frozen = {
            "version": SCHEMA_VERSION,
            "experiment_id": report["experiment_id"],
            "identity": report["identity"],
            "candidate": report["winner"],
            "development_artifact": str(DEVELOPMENT_OUTPUT).replace("\\", "/"),
            "development_sha256": sha256_lf(development_path),
            "holdout_status": "strictly later evidence required",
            "untouched_holdout_passed": False,
            "live_confirmation_authorised": False,
            "production_promotion_authorised": False,
            "production_deployment_authorised": False,
            "production_paths_changed": 0,
        }
        base.write_json(root / FROZEN_OUTPUT, frozen)
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "trades": report["winner"]["trades"],
                "win_rate": report["winner"]["win_rate"],
                "wilson": report["winner"]["wilson_95_lower_bound"],
                "pnl_sol": report["winner"]["net_pnl_sol"],
                "profit_factor": report["winner"]["profit_factor"],
                "positive_windows": report["winner"]["positive_capture_windows"],
                "maximum_fold_drawdown": report["winner"][
                    "maximum_fold_drawdown_fraction"
                ],
                "development_gate_passed": report["development_gate_passed"],
                "ablation_gate_passed": report["ablation_gate_passed"],
                "ready": report["ready_for_strictly_later_evidence"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
