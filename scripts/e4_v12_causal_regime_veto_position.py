#!/usr/bin/env python3
"""Evaluate exact position sizing with two causal, interpretable risk vetoes.

This V10 experiment freezes one pair selected in an explicitly disclosed
development screen.  Every fold learns its veto thresholds from earlier
windows only.  The selected launches are then replayed from raw Pump reserves,
with every entry and exit re-quoted at the position implied by the evolving
bankroll.  No result produced here is an untouched holdout or live verdict.
"""

from __future__ import annotations

import json
import pickle
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_causal_actor_memory as actors
import e4_v12_causal_dynamic_exit as dynamic_exit
import e4_v12_direct_hazard_consensus as hazard
import e4_v12_profit_survival_search as base
import e4_v12_scale_out_profit_lock as scale_out
import numpy as np

SCHEMA_VERSION = "e4-v12-causal-regime-veto-position-v10"
THESIS_FAMILY = "causal-regime-veto-exact-position-sizing-v10"
POSITION_FRACTION = 0.04
BASELINE_POSITION_FRACTION = base.POSITION_FRACTION
POLICY = scale_out.ScaleOutPolicy(1.20, 0.55, 3.0, 1.25, 0.25)
FOLD_TRAIN_ENDS = (28, 33, 38, 43)
EXPLORATORY_PAIRS_TESTED = 59_508
EXPLORATORY_PAIRS_PASSING = 19


@dataclass(frozen=True, slots=True)
class CausalVeto:
    feature: str
    quantile: float
    keep: str

    @property
    def key(self) -> str:
        return f"{self.feature}|q={self.quantile:.2f}|keep={self.keep}"


REGIME_VETO = CausalVeto("prior_price_multiple_median", 0.93, "at_or_below")
ACTOR_VETO = CausalVeto("buyer_average_pnl_max", 0.05, "at_or_above")
VETOES = (REGIME_VETO, ACTOR_VETO)


def feature_index(veto: CausalVeto) -> int:
    return actors.FEATURE_NAMES.index(veto.feature)


def threshold(train: Sequence[base.ResearchRow], veto: CausalVeto) -> float:
    values = np.asarray([row.features[feature_index(veto)] for row in train], dtype=float)
    if not len(values) or not np.isfinite(values).all():
        raise ValueError(f"invalid threshold source for {veto.key}")
    return float(np.quantile(values, veto.quantile))


def passes(row: base.ResearchRow, veto: CausalVeto, value: float) -> bool:
    observed = row.features[feature_index(veto)]
    if veto.keep == "at_or_below":
        return observed <= value
    if veto.keep == "at_or_above":
        return observed >= value
    raise ValueError(f"unknown veto direction: {veto.keep}")


def load_traces(
    root: Path,
    selected: Sequence[base.ResearchRow],
    cache: Path,
) -> tuple[dict[tuple[str, str], base.Trace], list[dict[str, Any]]]:
    keys = {(row.run_id, row.mint) for row in selected}
    specs = {
        spec.run_id: spec
        for spec in adaptive.capture_specs(root, include_holdout=True)
        if any(run_id == spec.run_id for run_id, _ in keys)
    }
    manifest_sha = base.sha256_path(
        root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
    )
    if cache.exists():
        with cache.open("rb") as handle:
            payload = pickle.load(handle)
        if payload.get("manifest_sha256") != manifest_sha:
            raise ValueError("trace cache manifest fingerprint changed")
        traces = payload["traces"]
        audit = payload["audit"]
        for run_id, spec in specs.items():
            actual = base.sha256_path(spec.events_path)
            if actual != spec.expected_sha256:
                raise ValueError(f"capture hash changed: {run_id}")
        if keys - set(traces):
            raise ValueError("trace cache is missing selected launches")
        return traces, audit

    traces: dict[tuple[str, str], base.Trace] = {}
    audit = []
    for run_id in sorted(specs, key=int):
        spec = specs[run_id]
        actual = base.sha256_path(spec.events_path)
        if actual != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {run_id}")
        capture, parse_errors = base.load_capture(spec, Counter())
        chosen = [trace for trace in capture if (run_id, trace.mint) in keys]
        traces.update({(run_id, trace.mint): trace for trace in chosen})
        audit.append(
            {
                "run_id": run_id,
                "sha256": actual,
                "hash_match": True,
                "parse_errors": parse_errors,
                "selected_traces": len(chosen),
            }
        )
    missing = keys - set(traces)
    if missing:
        raise ValueError(f"selected traces missing: {sorted(missing)[:3]}")
    with cache.open("wb") as handle:
        pickle.dump(
            {"manifest_sha256": manifest_sha, "traces": traces, "audit": audit},
            handle,
        )
    return traces, audit


def simulate_exact(
    rows: Sequence[base.ResearchRow],
    traces: Mapping[tuple[str, str], base.Trace],
    position_fraction: float,
) -> dict[str, Any]:
    candidates = sorted(
        rows,
        key=lambda row: (base.integer(row.run_id), row.decision_ns, row.mint),
    )
    bankroll = base.STARTING_BANKROLL_SOL
    active: list[tuple[int, str]] = []
    ledger = []
    rejected_concurrency = 0
    for row in candidates:
        active = [item for item in active if item[0] > row.decision_ns]
        if len(active) >= base.MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        position = bankroll * position_fraction
        trace = traces[(row.run_id, row.mint)]
        outcome = scale_out.scale_out_outcome(
            trace,
            POLICY,
            position_sol=position,
        )
        if outcome is None:
            raise ValueError(f"invalid exact quote: {row.run_id}/{row.mint}")
        pnl = base.net_pnl(outcome, scale_out.PRIORITY_FEE_SOL, position)
        bankroll += pnl
        exit_ns = row.decision_ns + int(outcome.exit_offset_ms * 1_000_000)
        active.append((exit_ns, row.mint))
        ledger.append(
            {
                "run_id": row.run_id,
                "mint": row.mint,
                "decision_ns": row.decision_ns,
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
        "position_fraction": position_fraction,
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


def compact(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in candidate.items() if key != "folds"
    } | {
        "folds": [
            {
                key: fold[key]
                for key in (
                    "position_fraction",
                    "trades",
                    "wins",
                    "win_rate",
                    "wilson_95_lower_bound",
                    "net_pnl_sol",
                    "profit_factor",
                    "maximum_drawdown_fraction",
                    "capture_windows",
                    "largest_winner_contribution",
                    "by_capture_window",
                    "exit_reasons",
                    "ledger_hash",
                )
            }
            for fold in candidate["folds"]
        ]
    }


def run(root: Path) -> dict[str, Any]:
    rule = dynamic_exit.actor_rule(root)
    rows, _ = dynamic_exit.load_cohort(
        root,
        root / ".tmp-dynamic-exit-cohort.pkl",
        rule,
    )
    traces, source_audit = load_traces(
        root,
        rows,
        root / ".tmp-regime-veto-position-traces.pkl",
    )
    all_actor_rows, actor_audit = actors.load_rows(
        root,
        root / ".tmp-actor-memory-development.pkl",
    )
    windows = hazard.ordered_windows(all_actor_rows)
    folds: list[dict[str, Any]] = []
    candidate_names = (
        "baseline_1p85_no_veto",
        "size_4_no_veto",
        "size_4_regime_veto",
        "size_4_regime_and_actor_veto",
    )
    for train_end in FOLD_TRAIN_ENDS:
        train_ids = set(windows[:train_end])
        validation_ids = set(windows[train_end : train_end + 5])
        train = [row for row in rows if row.run_id in train_ids]
        validation = [row for row in rows if row.run_id in validation_ids]
        learned = {veto.key: threshold(train, veto) for veto in VETOES}
        regime_rows = [
            row
            for row in validation
            if passes(row, REGIME_VETO, learned[REGIME_VETO.key])
        ]
        joint_rows = [
            row
            for row in regime_rows
            if passes(row, ACTOR_VETO, learned[ACTOR_VETO.key])
        ]
        candidates = {
            "baseline_1p85_no_veto": simulate_exact(
                validation, traces, BASELINE_POSITION_FRACTION
            ),
            "size_4_no_veto": simulate_exact(validation, traces, POSITION_FRACTION),
            "size_4_regime_veto": simulate_exact(
                regime_rows, traces, POSITION_FRACTION
            ),
            "size_4_regime_and_actor_veto": simulate_exact(
                joint_rows, traces, POSITION_FRACTION
            ),
        }
        folds.append(
            {
                "train_windows": list(windows[:train_end]),
                "validation_windows": list(windows[train_end : train_end + 5]),
                "train_actor_rows": len(train),
                "validation_actor_rows": len(validation),
                "thresholds": learned,
                "candidates": candidates,
            }
        )
    aggregated = {
        name: hazard.aggregate_candidate(folds, name) for name in candidate_names
    }
    selected_name = "size_4_regime_and_actor_veto"
    selected = aggregated[selected_name]
    baseline = aggregated["size_4_no_veto"]
    requirements = hazard.candidate_rank(selected)[0]
    ablation = {
        "versus_size_4_no_veto": {
            "win_rate_delta": selected["win_rate"] - baseline["win_rate"],
            "profit_factor_delta": selected["profit_factor"]
            - baseline["profit_factor"],
            "positive_capture_windows_delta": selected["positive_capture_windows"]
            - baseline["positive_capture_windows"],
            "pnl_delta_sol": selected["net_pnl_sol"] - baseline["net_pnl_sol"],
        },
        "selected_improves_win_rate": selected["win_rate"] > baseline["win_rate"],
        "selected_improves_profit_factor": selected["profit_factor"]
        > baseline["profit_factor"],
        "selected_improves_time_coverage": selected["positive_capture_windows"]
        > baseline["positive_capture_windows"],
    }
    ablation_passed = all(
        ablation[key]
        for key in (
            "selected_improves_win_rate",
            "selected_improves_profit_factor",
            "selected_improves_time_coverage",
        )
    )
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "frozen buyer rule plus prior-window scalar vetoes",
        "full_parameters": {
            "buyer_rule": asdict(rule),
            "position_fraction": POSITION_FRACTION,
            "vetoes": [asdict(veto) for veto in VETOES],
            "scale_out": asdict(POLICY),
            "preformal_pairs_tested": EXPLORATORY_PAIRS_TESTED,
            "preformal_pairs_passing": EXPLORATORY_PAIRS_PASSING,
        },
        "causal_horizon_ms": actors.HORIZON_MS,
        "candidate_risk_set_policy": {
            "actor_rule": rule.key,
            "veto_threshold_source": "all earlier windows in each fold only",
            "selected": selected_name,
        },
        "chronological_split": [
            {
                "train": fold["train_windows"],
                "validation": fold["validation_windows"],
            }
            for fold in folds
        ],
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol_per_transaction": scale_out.PRIORITY_FEE_SOL,
            "tip_sol_per_transaction": base.TIP_SOL,
            "base_fee_sol_per_transaction": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
            "additional_scale_out_transaction_charged": True,
        },
        "output_guard": "resolved actors delayed 60s; prior-window vetoes; raw reserve re-quotes",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; evolving bankroll; maximum two exits",
        "exit_policy": asdict(POLICY),
    }
    ready = requirements == 11 and ablation_passed
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "preformal_screen": {
            "pairs_tested": EXPLORATORY_PAIRS_TESTED,
            "pairs_passing": EXPLORATORY_PAIRS_PASSING,
            "holdout_status": "development_only_not_untouched",
            "selection_rationale": (
                "highest gate rank with 69% WR, strongest minimum fold PF among "
                "top-PnL interpretable pairs, and distinct regime/actor mechanisms"
            ),
        },
        "actor_memory_audit": {
            "captures": actor_audit["captures"],
            "launches": actor_audit["launches"],
            "rows": actor_audit["rows"],
            "future_values_in_features": actor_audit["future_values_in_features"],
            "actor_updates_delayed_ms": actor_audit["actor_updates_delayed_ms"],
        },
        "source_audit": source_audit,
        "actor_cohort_rows": len(rows),
        "folds": folds,
        "selected_candidate": selected_name,
        "winner": compact(selected),
        "ablations": {name: compact(candidate) for name, candidate in aggregated.items()},
        "ablation_verdict": ablation,
        "walk_forward_requirements_passed": requirements,
        "walk_forward_gate_passed": requirements == 11,
        "ablation_gate_passed": ablation_passed,
        "ready_for_strictly_later_evidence": ready,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    development_path = root / "artifacts/e4-v12-causal-regime-veto-position-development.json"
    base.write_json(development_path, report)
    if report["ready_for_strictly_later_evidence"]:
        frozen = {
            "version": SCHEMA_VERSION,
            "experiment_id": report["experiment_id"],
            "identity": report["identity"],
            "candidate": report["winner"],
            "development_artifact": development_path.name,
            "development_sha256": base.sha256_path(development_path),
            "holdout_status": "strictly later evidence required",
            "untouched_holdout_passed": False,
            "live_confirmation_authorised": False,
            "production_promotion_authorised": False,
            "production_paths_changed": 0,
        }
        base.write_json(
            root / "artifacts/e4-v12-causal-regime-veto-position-frozen-candidate.json",
            frozen,
        )
    winner = report["winner"]
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "candidate": report["selected_candidate"],
                "trades": winner["trades"],
                "win_rate": winner["win_rate"],
                "wilson": winner["wilson_95_lower_bound"],
                "pnl_sol": winner["net_pnl_sol"],
                "profit_factor": winner["profit_factor"],
                "positive_folds": winner["positive_folds"],
                "positive_windows": winner["positive_capture_windows"],
                "requirements_passed": report["walk_forward_requirements_passed"],
                "ablation_passed": report["ablation_gate_passed"],
                "ready": report["ready_for_strictly_later_evidence"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
