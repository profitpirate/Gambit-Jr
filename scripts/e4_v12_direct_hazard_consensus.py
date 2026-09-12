#!/usr/bin/env python3
"""Walk-forward development of a direct profit/margin/loss-hazard consensus.

All 48 captures used here are already-consumed historical evidence.  Four
expanding chronological folds test the final 20 windows.  Candidate selection
uses only a rolling distribution of earlier model scores, never a future-window
rank.  A new, strictly later evidence epoch remains mandatory before any thesis
can be called a historical or live success.
"""

from __future__ import annotations

import json
import pickle
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import e4_v12_adaptive_exit_search as adaptive
import e4_v12_profit_survival_search as base
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

SCHEMA_VERSION = "e4-v12-direct-hazard-consensus-v5"
THESIS_FAMILY = "direct-profit-margin-loss-hazard-consensus-v5"
HORIZON_MS = 250
RECENT_WINDOWS = 8
ROLLING_SCORE_HISTORY = 1_000
RANDOM_SEED = 12_093
QUANTILES = (0.990, 0.993, 0.995, 0.996, 0.9965, 0.997, 0.998)
SCORE_FAMILIES = (
    "profit_consensus",
    "margin_consensus",
    "profit_hazard_veto",
    "profit_margin_hazard_geometric",
    "strict_joint_minimum",
)


def consumed_rows(root: Path, cache_path: Path) -> tuple[list[base.ResearchRow], dict[str, Any]]:
    """Load all known rows, appending the five now-consumed V3 holdout windows."""
    with (root / ".tmp-adaptive-exit-development.pkl").open("rb") as handle:
        development_cache = pickle.load(handle)
    development = [
        row for row in development_cache["rows"] if row.horizon_ms == HORIZON_MS
    ]
    if cache_path.exists():
        with cache_path.open("rb") as handle:
            later_cache = pickle.load(handle)
        later = later_cache["rows"]
        later_audit = later_cache["audit"]
    else:
        later_specs = [
            spec
            for spec in adaptive.capture_specs(root, include_holdout=True)
            if spec.split == "holdout"
        ]
        later, later_audit = adaptive.extract_specs(
            later_specs,
            Counter(development_cache["creator_counts"]),
            verify_hashes=True,
            horizons=(HORIZON_MS,),
        )
        with cache_path.open("wb") as handle:
            pickle.dump({"rows": later, "audit": later_audit}, handle)
    raw_rows = sorted(
        [*development, *later],
        key=lambda row: (row.decision_ns, int(row.run_id), row.mint),
    )
    rows = [replace(row, split="development") for row in raw_rows]
    contextual = adaptive.with_causal_market_context(rows)
    audit = {
        "version": SCHEMA_VERSION,
        "captures": len({row.run_id for row in contextual}),
        "launch_rows": len(contextual),
        "features": len(adaptive.FEATURE_NAMES),
        "later_capture_audit": later_audit,
        "all_evidence_previously_consumed": True,
        "future_holdout_claimed": False,
        "production_paths_changed": 0,
    }
    return contextual, audit


def ordered_windows(rows: Sequence[base.ResearchRow]) -> list[str]:
    starts: dict[str, int] = {}
    for row in rows:
        starts[row.run_id] = min(starts.get(row.run_id, row.decision_ns), row.decision_ns)
    return sorted(starts, key=lambda run_id: (starts[run_id], int(run_id)))


def labels(
    rows: Sequence[base.ResearchRow], policy_index: int
) -> dict[str, np.ndarray]:
    position = base.STARTING_BANKROLL_SOL * base.POSITION_FRACTION
    pnls = np.asarray(
        [base.net_pnl(row.outcomes[policy_index], 0.001, position) for row in rows],
        dtype=float,
    )
    return {
        "profit": pnls > 0,
        "margin": pnls >= 0.003,
        "hazard": pnls <= -0.010,
    }


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
    train: Sequence[base.ResearchRow], policy_index: int
) -> dict[str, HistGradientBoostingClassifier]:
    windows = ordered_windows(train)
    recent_ids = set(windows[-RECENT_WINDOWS:])
    recent = [row for row in train if row.run_id in recent_ids]
    all_labels = labels(train, policy_index)
    recent_labels = labels(recent, policy_index)
    matrix_all = base.feature_matrix(train)
    matrix_recent = base.feature_matrix(recent)
    models = {}
    for target in ("profit", "margin", "hazard"):
        models[f"{target}_all"] = classifier().fit(
            matrix_all, all_labels[target].astype(int)
        )
        models[f"{target}_recent"] = classifier().fit(
            matrix_recent, recent_labels[target].astype(int)
        )
    return models


def score_models(
    models: Mapping[str, HistGradientBoostingClassifier],
    rows: Sequence[base.ResearchRow],
) -> dict[str, np.ndarray]:
    matrix = base.feature_matrix(rows)
    probabilities = {
        name: model.predict_proba(matrix)[:, 1] for name, model in models.items()
    }
    profit = np.minimum(probabilities["profit_all"], probabilities["profit_recent"])
    margin = np.minimum(probabilities["margin_all"], probabilities["margin_recent"])
    safety = 1 - np.maximum(probabilities["hazard_all"], probabilities["hazard_recent"])
    return {
        "profit_consensus": profit,
        "margin_consensus": margin,
        "profit_hazard_veto": profit * safety,
        "profit_margin_hazard_geometric": np.cbrt(profit * margin * safety),
        "strict_joint_minimum": np.minimum(np.minimum(profit, margin), safety),
    }


def causal_quantile_mask(
    seed_scores: Sequence[float], validation_scores: Sequence[float], quantile: float
) -> np.ndarray:
    history: deque[float] = deque(
        (float(value) for value in seed_scores[-ROLLING_SCORE_HISTORY:]),
        maxlen=ROLLING_SCORE_HISTORY,
    )
    selected = []
    for value in validation_scores:
        threshold = float(np.quantile(np.asarray(history, dtype=float), quantile))
        selected.append(float(value) >= threshold)
        history.append(float(value))
    return np.asarray(selected, dtype=bool)


def simulate_mask(
    rows: Sequence[base.ResearchRow], mask: np.ndarray, policy_index: int
) -> dict[str, Any]:
    return base.simulate_predictions(
        rows,
        mask.astype(float),
        0.5,
        policy_index=policy_index,
        priority_fee_sol=0.001,
    )


def fold_result(
    rows: Sequence[base.ResearchRow],
    windows: Sequence[str],
    train_end: int,
    policy_index: int,
) -> dict[str, Any]:
    train_ids = set(windows[:train_end])
    validation_ids = set(windows[train_end : train_end + 5])
    train = [row for row in rows if row.run_id in train_ids]
    validation = [row for row in rows if row.run_id in validation_ids]
    models = fit_models(train, policy_index)
    train_scores = score_models(models, train)
    validation_scores = score_models(models, validation)
    candidates = {}
    for family in SCORE_FAMILIES:
        seed = train_scores[family]
        for quantile in QUANTILES:
            mask = causal_quantile_mask(seed, validation_scores[family], quantile)
            candidates[f"{family}|q={quantile:.3f}"] = simulate_mask(
                validation, mask, policy_index
            )
    return {
        "train_windows": list(windows[:train_end]),
        "validation_windows": list(windows[train_end : train_end + 5]),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "candidates": candidates,
    }


def aggregate_candidate(
    folds: Sequence[Mapping[str, Any]], key: str
) -> dict[str, Any]:
    metrics = [fold["candidates"][key] for fold in folds]
    trades = sum(item["trades"] for item in metrics)
    wins = sum(item["wins"] for item in metrics)
    pnl = sum(item["net_pnl_sol"] for item in metrics)
    gross_profits = sum(
        sum(row["pnl_sol"] for row in item["ledger"] if row["pnl_sol"] > 0)
        for item in metrics
    )
    gross_losses = abs(
        sum(
            sum(row["pnl_sol"] for row in item["ledger"] if row["pnl_sol"] <= 0)
            for item in metrics
        )
    )
    windows = [
        window
        for item in metrics
        for window in item["by_capture_window"].values()
    ]
    profits = [
        row["pnl_sol"] for item in metrics for row in item["ledger"] if row["pnl_sol"] > 0
    ]
    return {
        "candidate": key,
        "folds": metrics,
        "trades": trades,
        "wins": wins,
        "win_rate": wins / max(trades, 1),
        "wilson_95_lower_bound": base.wilson_lower_bound(wins, trades),
        "net_pnl_sol": pnl,
        "profit_factor": gross_profits / max(gross_losses, 1e-12),
        "positive_folds": sum(item["net_pnl_sol"] > 0 for item in metrics),
        "minimum_fold_profit_factor": min(item["profit_factor"] for item in metrics),
        "minimum_fold_win_rate": min(item["win_rate"] for item in metrics),
        "capture_windows": sum(item["capture_windows"] for item in metrics),
        "positive_capture_windows": sum(window["pnl_sol"] > 0 for window in windows),
        "maximum_fold_drawdown_fraction": max(
            item["maximum_drawdown_fraction"] for item in metrics
        ),
        "largest_winner_contribution": max(profits, default=0.0)
        / max(sum(profits), 1e-12),
        "ledger_hash": base.stable_hash([item["ledger_hash"] for item in metrics]),
    }


def candidate_rank(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    requirements = (
        candidate["trades"] >= 200,
        candidate["win_rate"] >= 0.65,
        candidate["wilson_95_lower_bound"] >= 0.60,
        candidate["net_pnl_sol"] > 0,
        candidate["profit_factor"] >= 1.25,
        candidate["positive_folds"] == 4,
        candidate["minimum_fold_profit_factor"] >= 1.10,
        candidate["minimum_fold_win_rate"] >= 0.55,
        candidate["positive_capture_windows"] >= 16,
        candidate["maximum_fold_drawdown_fraction"] <= 0.15,
        candidate["largest_winner_contribution"] <= 0.15,
    )
    return (
        sum(requirements),
        *requirements,
        candidate["positive_folds"],
        candidate["positive_capture_windows"],
        candidate["win_rate"],
        candidate["net_pnl_sol"],
    )


def run(root: Path) -> dict[str, Any]:
    rows, audit = consumed_rows(root, root / ".tmp-hazard-consensus-later.pkl")
    windows = ordered_windows(rows)
    if len(windows) != 48:
        raise ValueError(f"expected 48 consumed capture windows, got {len(windows)}")
    frozen = json.loads(
        (root / "artifacts/e4-v12-adaptive-exit-frozen-candidate.json").read_text(
            encoding="utf-8"
        )
    )
    policy_index = base.integer(frozen["candidate"]["policy_index"])
    folds = []
    for number, train_end in enumerate((28, 33, 38, 43), 1):
        print(f"fit walk-forward fold {number}/4", flush=True)
        folds.append(fold_result(rows, windows, train_end, policy_index))
    keys = sorted(folds[0]["candidates"])
    candidates = [aggregate_candidate(folds, key) for key in keys]
    candidates.sort(key=candidate_rank, reverse=True)
    winner = candidates[0]
    requirements_passed = candidate_rank(winner)[0]
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "parent_experiment_id": frozen["experiment_id"],
        "dataset_source_manifest_fingerprint": base.sha256_path(
            root / "artifacts/e4-v12-adaptive-exit-source-manifest.json"
        ),
        "evidence_epoch": windows,
        "feature_set_fingerprint": base.stable_hash(adaptive.FEATURE_NAMES),
        "model_family": "six-HGB direct profit/margin/hazard all-history/recent consensus",
        "full_parameters": {
            "random_seed": RANDOM_SEED,
            "learning_rate": 0.035,
            "max_iter": 180,
            "max_leaf_nodes": 15,
            "min_samples_leaf": 100,
            "l2_regularization": 12.0,
            "recent_windows": RECENT_WINDOWS,
            "rolling_score_history": ROLLING_SCORE_HISTORY,
            "selected_candidate": winner["candidate"],
        },
        "causal_horizon_ms": HORIZON_MS,
        "candidate_risk_set_policy": "rolling prior-score quantile; current score appended after decision",
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
        "output_guard": "reserve-valid exact constant-product quote",
        "latency_assumptions_ms": list(base.LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": frozen["identity"]["exit_policy"],
    }
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": f"e4x-{base.stable_hash(identity)}",
        "identity": identity,
        "audit": audit,
        "folds": folds,
        "candidates_evaluated": len(candidates),
        "winner": winner,
        "top_candidates": candidates[:20],
        "walk_forward_requirements_passed": requirements_passed,
        "walk_forward_gate_passed": requirements_passed == 11,
        "ready_for_strictly_later_evidence": requirements_passed == 11,
        "untouched_holdout_passed": False,
        "live_confirmation_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def main() -> None:
    root = Path.cwd().resolve()
    report = run(root)
    base.write_json(root / "artifacts/e4-v12-direct-hazard-consensus-development.json", report)
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
