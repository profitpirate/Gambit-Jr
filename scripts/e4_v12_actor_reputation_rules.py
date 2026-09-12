#!/usr/bin/env python3
"""Search interpretable, point-in-time buyer and creator reputation rules."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import e4_v12_causal_actor_memory as actors
import e4_v12_direct_hazard_consensus as hazard
import e4_v12_profit_survival_search as base
import numpy as np

SCHEMA_VERSION = "e4-v12-actor-reputation-rules-v7"
THESIS_FAMILY = "interpretable-point-in-time-actor-reputation-v7"
ACTOR_OFFSET = 42


@dataclass(frozen=True, slots=True)
class BuyerRule:
    known_buyers_min: int
    known_fraction_min: float
    history_max_min: int
    bayesian_win_rate_max_min: float
    average_pnl_max_min: float
    positive_expectancy_fraction_min: float

    @property
    def key(self) -> str:
        return (
            f"buyer|known={self.known_buyers_min}|fraction={self.known_fraction_min:.2f}"
            f"|history={self.history_max_min}|wr={self.bayesian_win_rate_max_min:.2f}"
            f"|pnl={self.average_pnl_max_min:.3f}"
            f"|positive={self.positive_expectancy_fraction_min:.2f}"
        )


@dataclass(frozen=True, slots=True)
class CreatorRule:
    resolved_min: int
    bayesian_win_rate_min: float
    average_pnl_min: float
    severe_loss_rate_max: float

    @property
    def key(self) -> str:
        return (
            f"creator|history={self.resolved_min}|wr={self.bayesian_win_rate_min:.2f}"
            f"|pnl={self.average_pnl_min:.3f}|severe={self.severe_loss_rate_max:.2f}"
        )


BUYER_RULES = tuple(
    BuyerRule(known, fraction, history, win_rate, pnl, positive)
    for known in (1, 2)
    for fraction in (0.0, 0.25, 0.50)
    for history in (2, 3, 5, 10)
    for win_rate in (0.60, 0.70, 0.80)
    for pnl in (0.0, 0.003, 0.010)
    for positive in (0.0, 0.50)
)
CREATOR_RULES = tuple(
    CreatorRule(history, win_rate, pnl, severe)
    for history in (1, 2, 3, 5, 10)
    for win_rate in (0.55, 0.60, 0.70, 0.80)
    for pnl in (0.0, 0.003, 0.010)
    for severe in (0.25, 0.50)
)


def buyer_matches(row: base.ResearchRow, rule: BuyerRule) -> bool:
    values = row.features[ACTOR_OFFSET:]
    return bool(
        values[1] >= rule.known_buyers_min
        and values[2] >= rule.known_fraction_min
        and values[4] >= rule.history_max_min
        and values[6] >= rule.bayesian_win_rate_max_min
        and values[8] >= rule.average_pnl_max_min
        and values[9] >= rule.positive_expectancy_fraction_min
    )


def creator_matches(row: base.ResearchRow, rule: CreatorRule) -> bool:
    values = row.features[ACTOR_OFFSET:]
    return bool(
        values[11] >= rule.resolved_min
        and values[12] >= rule.bayesian_win_rate_min
        and values[13] >= rule.average_pnl_min
        and values[14] <= rule.severe_loss_rate_max
    )


def evaluate_rule(
    validation_groups: Sequence[Sequence[base.ResearchRow]],
    predicate: Callable[[base.ResearchRow], bool],
    key: str,
) -> dict[str, Any]:
    folds = []
    for rows in validation_groups:
        mask = np.asarray([predicate(row) for row in rows], dtype=bool)
        folds.append(hazard.simulate_mask(rows, mask, 0))
    synthetic_folds = [{"candidates": {key: metrics}} for metrics in folds]
    return hazard.aggregate_candidate(synthetic_folds, key)


def compact(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in candidate.items()
        if key not in {"folds"}
    } | {
        "folds": [
            {
                key: fold[key]
                for key in (
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
                    "ledger_hash",
                )
            }
            for fold in candidate["folds"]
        ]
    }


def run(root: Path) -> dict[str, Any]:
    rows, actor_audit = actors.load_rows(root, root / ".tmp-actor-memory-development.pkl")
    windows = hazard.ordered_windows(rows)
    validation_groups = [
        [row for row in rows if row.run_id in set(windows[start : start + 5])]
        for start in (28, 33, 38, 43)
    ]
    candidates = []
    for number, rule in enumerate(BUYER_RULES, 1):
        if number % 100 == 0:
            print(f"evaluate buyer rule {number}/{len(BUYER_RULES)}", flush=True)
        candidate = evaluate_rule(
            validation_groups,
            lambda row, rule=rule: buyer_matches(row, rule),
            rule.key,
        )
        candidate["rule_type"] = "buyer"
        candidate["parameters"] = asdict(rule)
        candidates.append(candidate)
    for rule in CREATOR_RULES:
        candidate = evaluate_rule(
            validation_groups,
            lambda row, rule=rule: creator_matches(row, rule),
            rule.key,
        )
        candidate["rule_type"] = "creator"
        candidate["parameters"] = asdict(rule)
        candidates.append(candidate)
    candidates.sort(key=hazard.candidate_rank, reverse=True)
    winner = candidates[0]
    requirements = hazard.candidate_rank(winner)[0]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(actors.FEATURE_NAMES),
        "model_family": "deterministic point-in-time reputation rule",
        "full_parameters": winner["parameters"],
        "causal_horizon_ms": actors.HORIZON_MS,
        "candidate_risk_set_policy": winner["candidate"],
        "chronological_split": {
            "development_windows": windows[:28],
            "walk_forward_validation_windows": windows[28:],
            "future_holdout": "strictly later evidence still collecting",
        },
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "actor results delayed 60 seconds; no unresolved outcomes",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": json.loads(
            (root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json").read_text(
                encoding="utf-8"
            )
        )["identity"]["exit_policy"],
    }
    ready = requirements == 11
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "actor_audit_fingerprint": base.stable_hash(actor_audit),
        "buyer_rules_evaluated": len(BUYER_RULES),
        "creator_rules_evaluated": len(CREATOR_RULES),
        "winner": compact(winner),
        "top_candidates": [compact(row) for row in candidates[:20]],
        "walk_forward_requirements_passed": requirements,
        "walk_forward_gate_passed": ready,
        "ready_for_strictly_later_evidence": ready,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    base.write_json(root / "artifacts/e4-v12-actor-reputation-rules-development.json", report)
    winner = report["winner"]
    print(
        json.dumps(
            {
                "experiment_id": report["experiment_id"],
                "candidate": winner["candidate"],
                "trades": winner["trades"],
                "win_rate": winner["win_rate"],
                "pnl_sol": winner["net_pnl_sol"],
                "profit_factor": winner["profit_factor"],
                "positive_folds": winner["positive_folds"],
                "positive_windows": winner["positive_capture_windows"],
                "requirements_passed": report["walk_forward_requirements_passed"],
                "ready": report["ready_for_strictly_later_evidence"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
