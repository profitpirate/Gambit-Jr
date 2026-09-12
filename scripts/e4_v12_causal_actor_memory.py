#!/usr/bin/env python3
"""Build and ablate point-in-time buyer and creator performance memory."""

from __future__ import annotations

import heapq
import json
import pickle
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_direct_hazard_consensus as hazard
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

SCHEMA_VERSION = "e4-v12-causal-actor-memory-v6"
THESIS_FAMILY = "point-in-time-early-buyer-creator-memory-v6"
HORIZON_MS = 250
RESOLUTION_DELAY_MS = 60_000
RECENT_WINDOWS = 8
RANDOM_SEED = 12_094
QUANTILES = (0.995, 0.996, 0.997, 0.998)
ACTOR_FEATURE_NAMES = (
    "early_buyer_count",
    "known_early_buyer_count",
    "known_early_buyer_fraction",
    "buyer_history_count_mean",
    "buyer_history_count_max",
    "buyer_bayesian_win_rate_mean",
    "buyer_bayesian_win_rate_max",
    "buyer_average_pnl_mean",
    "buyer_average_pnl_max",
    "buyer_positive_expectancy_fraction",
    "buyer_severe_loss_rate_mean",
    "creator_resolved_launches",
    "creator_bayesian_win_rate",
    "creator_average_pnl",
    "creator_severe_loss_rate",
    "creator_profitable_history",
)
FEATURE_NAMES = (*adaptive.FEATURE_NAMES, *ACTOR_FEATURE_NAMES)


@dataclass(slots=True)
class ActorState:
    resolved: int = 0
    wins: int = 0
    pnl_sol: float = 0.0
    severe_losses: int = 0

    def update(self, pnl_sol: float) -> None:
        self.resolved += 1
        self.wins += int(pnl_sol > 0)
        self.pnl_sol += pnl_sol
        self.severe_losses += int(pnl_sol <= -0.010)

    @property
    def bayesian_win_rate(self) -> float:
        return (self.wins + 1) / (self.resolved + 2)

    @property
    def average_pnl(self) -> float:
        return self.pnl_sol / max(self.resolved, 1)

    @property
    def severe_loss_rate(self) -> float:
        return self.severe_losses / max(self.resolved, 1)


def early_buyers(trace: base.Trace) -> tuple[str, ...]:
    decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
    excluded = {trace.creator, base.choice_sets.E4_WALLET, ""}
    return tuple(
        sorted(
            {
                point.trader
                for point in trace.points
                if point.timestamp_ns <= decision_ns
                and point.kind in base.choice_sets.BUY_KINDS
                and point.trader not in excluded
            }
        )
    )


def actor_features(
    buyers: Sequence[str],
    creator: str,
    buyer_states: Mapping[str, ActorState],
    creator_states: Mapping[str, ActorState],
) -> tuple[float, ...]:
    known = [buyer_states[buyer] for buyer in buyers if buyer in buyer_states]
    creator_state = creator_states.get(creator, ActorState())

    def mean(values: Sequence[float]) -> float:
        return sum(values) / max(len(values), 1)

    buyer_counts = [float(state.resolved) for state in known]
    buyer_win_rates = [state.bayesian_win_rate for state in known]
    buyer_pnls = [state.average_pnl for state in known]
    buyer_severe_rates = [state.severe_loss_rate for state in known]
    return (
        float(len(buyers)),
        float(len(known)),
        len(known) / max(len(buyers), 1),
        mean(buyer_counts),
        max(buyer_counts, default=0.0),
        mean(buyer_win_rates),
        max(buyer_win_rates, default=0.0),
        mean(buyer_pnls),
        max(buyer_pnls, default=0.0),
        sum(state.average_pnl > 0 for state in known) / max(len(known), 1),
        mean(buyer_severe_rates),
        float(creator_state.resolved),
        creator_state.bayesian_win_rate,
        creator_state.average_pnl,
        creator_state.severe_loss_rate,
        float(creator_state.resolved > 0 and creator_state.average_pnl > 0),
    )


def add_market_context(rows: Sequence[base.ResearchRow]) -> list[base.ResearchRow]:
    base_only = [replace(row, features=row.features[: len(base.FEATURE_NAMES)]) for row in rows]
    contextual = adaptive.with_causal_market_context(base_only)
    return [
        replace(
            original,
            features=(
                *context.features,
                *original.features[len(base.FEATURE_NAMES) :],
            ),
        )
        for original, context in zip(rows, contextual, strict=True)
    ]


def extract_rows(root: Path) -> tuple[list[base.ResearchRow], dict[str, Any]]:
    frozen = json.loads(
        (root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json").read_text(
            encoding="utf-8"
        )
    )
    policy = adaptive.POLICIES[base.integer(frozen["candidate"]["policy_index"])]
    buyer_states: dict[str, ActorState] = {}
    creator_states: dict[str, ActorState] = {}
    pending: list[tuple[int, int, str, tuple[str, ...], float]] = []
    sequence = 0
    creator_counts: Counter[str] = Counter()
    rows = []
    audits = []

    def flush(timestamp_ns: int) -> None:
        while pending and pending[0][0] <= timestamp_ns:
            _, _, creator, buyers, pnl_sol = heapq.heappop(pending)
            creator_states.setdefault(creator, ActorState()).update(pnl_sol)
            for buyer in buyers:
                buyer_states.setdefault(buyer, ActorState()).update(pnl_sol)

    for spec in adaptive.capture_specs(root, include_holdout=True):
        actual_sha = base.sha256_path(spec.events_path)
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        traces, parse_errors = base.load_capture(spec, creator_counts)
        traces.sort(key=lambda trace: (trace.create_ns, trace.mint))
        counts: Counter[str] = Counter()
        for trace in traces:
            decision_ns = trace.create_ns + HORIZON_MS * 1_000_000
            if decision_ns + RESOLUTION_DELAY_MS * 1_000_000 > spec.end_ns:
                counts["insufficient_tail"] += 1
                continue
            flush(decision_ns)
            snapshot = base.snapshot(trace, HORIZON_MS)
            outcome = adaptive.adaptive_outcome(trace, HORIZON_MS, policy)
            if snapshot is None or outcome is None:
                counts["invalid"] += 1
                continue
            buyers = early_buyers(trace)
            memory = actor_features(buyers, trace.creator, buyer_states, creator_states)
            rows.append(
                base.ResearchRow(
                    run_id=spec.run_id,
                    split="development",
                    mint=trace.mint,
                    decision_ns=decision_ns,
                    horizon_ms=HORIZON_MS,
                    features=(*snapshot, *memory),
                    outcomes=(outcome,),
                )
            )
            position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
            pnl_sol = base.net_pnl(outcome, 0.001, position)
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
            counts["rows_with_known_buyer"] += int(memory[1] > 0)
            counts["rows_with_known_creator"] += int(memory[11] > 0)
        audits.append(
            {
                "run_id": spec.run_id,
                "launches": len(traces),
                "parse_errors": parse_errors,
                "sha256": actual_sha,
                "hash_match": True,
                **dict(counts),
            }
        )
    contextual = add_market_context(rows)
    audit = {
        "version": SCHEMA_VERSION,
        "captures": len(audits),
        "launches": sum(row["launches"] for row in audits),
        "rows": len(contextual),
        "features": len(FEATURE_NAMES),
        "feature_names": list(FEATURE_NAMES),
        "runs": audits,
        "rows_with_known_buyer": sum(
            row.get("rows_with_known_buyer", 0) for row in audits
        ),
        "rows_with_known_creator": sum(
            row.get("rows_with_known_creator", 0) for row in audits
        ),
        "actor_updates_delayed_ms": RESOLUTION_DELAY_MS,
        "future_values_in_features": False,
        "future_holdout_claimed": False,
        "production_paths_changed": 0,
    }
    return contextual, audit


def load_rows(root: Path, cache: Path) -> tuple[list[base.ResearchRow], dict[str, Any]]:
    if cache.exists():
        with cache.open("rb") as handle:
            payload = pickle.load(handle)
        return payload["rows"], payload["audit"]
    rows, audit = extract_rows(root)
    with cache.open("wb") as handle:
        pickle.dump({"rows": rows, "audit": audit}, handle)
    return rows, audit


def classifier() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.035,
        max_iter=180,
        max_leaf_nodes=15,
        min_samples_leaf=100,
        l2_regularization=12.0,
        random_state=RANDOM_SEED,
    )


def fit_models(
    train: Sequence[base.ResearchRow], feature_count: int
) -> dict[str, HistGradientBoostingClassifier]:
    windows = hazard.ordered_windows(train)
    recent_ids = set(windows[-RECENT_WINDOWS:])
    recent = [row for row in train if row.run_id in recent_ids]

    def matrix(rows: Sequence[base.ResearchRow]) -> np.ndarray:
        return base.feature_matrix(rows)[:, :feature_count]

    train_labels = hazard.labels(train, 0)
    recent_labels = hazard.labels(recent, 0)
    models = {}
    for target in ("profit", "hazard"):
        models[f"{target}_all"] = classifier().fit(
            matrix(train), train_labels[target].astype(int)
        )
        models[f"{target}_recent"] = classifier().fit(
            matrix(recent), recent_labels[target].astype(int)
        )
    return models


def scores(
    models: Mapping[str, HistGradientBoostingClassifier],
    rows: Sequence[base.ResearchRow],
    feature_count: int,
) -> dict[str, np.ndarray]:
    matrix = base.feature_matrix(rows)[:, :feature_count]
    profit = np.minimum(
        models["profit_all"].predict_proba(matrix)[:, 1],
        models["profit_recent"].predict_proba(matrix)[:, 1],
    )
    safety = 1 - np.maximum(
        models["hazard_all"].predict_proba(matrix)[:, 1],
        models["hazard_recent"].predict_proba(matrix)[:, 1],
    )
    return {"profit_consensus": profit, "profit_hazard_veto": profit * safety}


def fold_result(
    rows: Sequence[base.ResearchRow], windows: Sequence[str], train_end: int
) -> dict[str, Any]:
    train_ids = set(windows[:train_end])
    validation_ids = set(windows[train_end : train_end + 5])
    train = [row for row in rows if row.run_id in train_ids]
    validation = [row for row in rows if row.run_id in validation_ids]
    candidates = {}
    for feature_set, count in (
        ("base", len(adaptive.FEATURE_NAMES)),
        ("actor", len(FEATURE_NAMES)),
    ):
        models = fit_models(train, count)
        train_scores = scores(models, train, count)
        validation_scores = scores(models, validation, count)
        for family in train_scores:
            for quantile in QUANTILES:
                mask = hazard.causal_quantile_mask(
                    train_scores[family], validation_scores[family], quantile
                )
                key = f"{feature_set}|{family}|q={quantile:.3f}"
                candidates[key] = hazard.simulate_mask(validation, mask, 0)
    return {
        "train_windows": list(windows[:train_end]),
        "validation_windows": list(windows[train_end : train_end + 5]),
        "candidates": candidates,
    }


def run(root: Path) -> dict[str, Any]:
    rows, audit = load_rows(root, root / ".tmp-actor-memory-development.pkl")
    windows = hazard.ordered_windows(rows)
    if len(windows) != 48:
        raise ValueError(f"expected 48 windows, got {len(windows)}")
    folds = []
    for number, train_end in enumerate((28, 33, 38, 43), 1):
        print(f"fit actor-memory fold {number}/4", flush=True)
        folds.append(fold_result(rows, windows, train_end))
    keys = sorted(folds[0]["candidates"])
    candidates = [hazard.aggregate_candidate(folds, key) for key in keys]
    candidates.sort(key=hazard.candidate_rank, reverse=True)
    actor = max(
        (row for row in candidates if row["candidate"].startswith("actor|")),
        key=hazard.candidate_rank,
    )
    baseline = max(
        (row for row in candidates if row["candidate"].startswith("base|")),
        key=hazard.candidate_rank,
    )
    actor_requirements = hazard.candidate_rank(actor)[0]
    ablation = {
        "actor_candidate": actor["candidate"],
        "base_candidate": baseline["candidate"],
        "actor_win_rate_delta": actor["win_rate"] - baseline["win_rate"],
        "actor_pnl_delta_sol": actor["net_pnl_sol"] - baseline["net_pnl_sol"],
        "actor_profit_factor_delta": actor["profit_factor"] - baseline["profit_factor"],
        "actor_rank_requirements_delta": actor_requirements
        - hazard.candidate_rank(baseline)[0],
        "actor_improves_win_rate": actor["win_rate"] > baseline["win_rate"],
        "actor_improves_pnl": actor["net_pnl_sol"] > baseline["net_pnl_sol"],
    }
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(FEATURE_NAMES),
        "model_family": "direct profit/loss-hazard HGB with all/recent actor consensus",
        "full_parameters": {
            "random_seed": RANDOM_SEED,
            "recent_windows": RECENT_WINDOWS,
            "quantile": actor["candidate"].split("q=")[-1],
            "resolution_delay_ms": RESOLUTION_DELAY_MS,
        },
        "causal_horizon_ms": HORIZON_MS,
        "candidate_risk_set_policy": "rolling causal prior-score quantile",
        "chronological_split": [
            {
                "train": fold["train_windows"],
                "validation": fold["validation_windows"],
            }
            for fold in folds
        ],
        "bankroll": base.STARTING_BANKROLL_SOL,
        "position_sizing_fraction": base.POSITION_FRACTION,
        "fee_model": {
            "priority_fee_sol": 0.001,
            "tip_sol": base.TIP_SOL,
            "base_fee_sol": base.BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": base.PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "resolved actor observations only; reserve-valid quote",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": json.loads(
            (root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json").read_text(
                encoding="utf-8"
            )
        )["identity"]["exit_policy"],
    }
    ready = (
        actor_requirements == 11
        and ablation["actor_improves_win_rate"]
        and ablation["actor_improves_pnl"]
    )
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "audit": audit,
        "folds": folds,
        "candidates_evaluated": len(candidates),
        "winner": actor,
        "base_ablation": baseline,
        "ablation": ablation,
        "walk_forward_requirements_passed": actor_requirements,
        "walk_forward_gate_passed": actor_requirements == 11,
        "ready_for_strictly_later_evidence": ready,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    base.write_json(root / "artifacts/e4-v12-causal-actor-memory-development.json", report)
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
                "ablation": report["ablation"],
                "requirements_passed": report["walk_forward_requirements_passed"],
                "ready": report["ready_for_strictly_later_evidence"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
