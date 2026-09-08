#!/usr/bin/env python3
"""Autonomous, causal, profit-first launch-hazard research over the 66k corpus.

This program deliberately separates two questions:

1. Can public launch-time information identify E4 entry intent?
2. Can the same information identify launches that are profitable under one
   frozen, independent exit policy at every 0/1/2/5/10ms fill delay?

It consumes only the immutable research corpus.  It never imports production
execution code and never submits a transaction.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
    RandomForestClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

SCHEMA_VERSION = "e4-v12-allout-profit-hazard-v1"
LATENCIES = (0, 1, 2, 5, 10)
ENTRY_BUDGET_SOL = 0.0555
STARTING_BANKROLL_SOL = 3.0
POSITION_FRACTION = 0.0185
MAX_CONCURRENT = 2
RESERVE_SOL = 0.03
SEED = 51_204

BASE_NUMERIC = (
    "create_fdv_usd",
    "create_price_sol",
    "initial_virtual_sol",
    "initial_virtual_tokens",
    "initial_real_tokens",
    "mayhem_mode",
    "cashback_enabled",
    "name_length",
    "name_word_count",
    "name_digit_fraction",
    "name_uppercase_fraction",
    "name_has_url",
    "name_has_non_ascii",
    "symbol_length",
    "symbol_word_count",
    "symbol_digit_fraction",
    "symbol_uppercase_fraction",
    "symbol_has_url",
    "symbol_has_non_ascii",
    "creator_prior_launch_count",
    "creator_prior_selection_count",
    "creator_prior_landed_count",
    "creator_prior_failed_fill_count",
    "creator_prior_known_wins",
    "creator_prior_known_losses",
    "creator_prior_known_pnl_sol",
    "creator_prior_known_win_rate",
    "time_since_creator_launch_ms",
    "time_since_creator_selection_ms",
    "prior_exact_uri_count",
    "prior_exact_name_count",
    "prior_exact_symbol_count",
)
WINDOW_STEMS = (
    "buy_count",
    "sell_count",
    "creator_buy_count",
    "creator_buy_sol",
    "outside_buy_sol",
    "unique_outside_buyers",
    "distinct_buy_signatures",
    "max_buys_per_signature",
    "same_slot_buy_count",
    "same_slot_outside_buyer_count",
    "same_create_signature_buy_count",
    "fdv",
    "price",
    "virtual_sol",
    "virtual_tokens",
    "real_tokens",
    "known_e4_buyer_count",
    "buyer_prior_selection_sum",
    "buyer_prior_win_sum",
    "creator_buyer_pair_max",
)
CATEGORICAL_FIELDS = (
    ("creator", 64),
    ("metadata_uri_host", 16),
    ("token_program", 8),
    ("name", 32),
    ("symbol", 16),
)
REPRESENTATIVE_POLICIES = (
    "hold_1000ms",
    "hold_2000ms",
    "hold_3000ms",
    "hold_5000ms",
    "hold_10000ms",
    "hold_15000ms",
    "hold_30000ms",
    "hold_60000ms",
    "tp110_sl90",
    "tp120_sl90",
    "tp130_sl90",
    "tp150_sl90",
    "tp120_sl80",
    "tp130_sl80",
    "tp150_sl80",
    "tp200_sl80",
    "tp200_sl70",
    "tp300_sl70",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return [json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def stable_hash(value: Any) -> str:
    payload = json.dumps(json_safe(value), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def policy_pnl_key(latency: int, policy: str) -> str:
    return f"paper_{latency}ms_{policy}_pnl_sol"


def policy_exit_key(latency: int, policy: str) -> str | None:
    if policy.startswith("hold_"):
        return None
    return f"paper_{latency}ms_{policy}_exit_delay_ms"


def discover_policies(sample: Mapping[str, Any]) -> list[str]:
    patterns = []
    for key in sample:
        match = re.fullmatch(r"paper_0ms_(.+)_pnl_sol", key)
        if not match:
            continue
        policy = match.group(1)
        if all(policy_pnl_key(latency, policy) in sample for latency in LATENCIES):
            patterns.append(policy)
    preferred = [policy for policy in REPRESENTATIVE_POLICIES if policy in patterns]
    return preferred or sorted(patterns)


def _hash_bucket(value: str, namespace: str, buckets: int) -> tuple[int, float]:
    digest = hashlib.sha256(f"{namespace}\x1f{value}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % buckets
    sign = 1.0 if digest[8] & 1 else -1.0
    return bucket, sign


@dataclass
class Corpus:
    rows: list[dict[str, Any]]
    numeric_fields: list[str]
    feature_names_general: list[str]
    feature_names_identity: list[str]
    x_general: np.ndarray
    x_identity: np.ndarray
    splits: np.ndarray
    create_ns: np.ndarray
    run_ids: np.ndarray
    mints: np.ndarray
    selected: np.ndarray
    landed: np.ndarray
    policies: list[str]
    pnl: dict[str, np.ndarray]
    exit_delay_ms: dict[str, np.ndarray]


def load_corpus(path: Path, horizon_ms: int) -> Corpus:
    rows: list[dict[str, Any]] = []
    sample: dict[str, Any] | None = None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if sample is None:
                sample = row
            rows.append(row)
    if not rows or sample is None:
        raise ValueError("empty launch corpus")
    if len(rows) != 66_000:
        raise ValueError(f"expected 66,000 launch rows, got {len(rows)}")

    policies = discover_policies(sample)
    if not policies:
        raise ValueError("no common 0/1/2/5/10ms paper policies found")

    numeric_fields = list(BASE_NUMERIC) + [
        f"{stem}_{horizon_ms}ms" for stem in WINDOW_STEMS
    ]
    # Explicitly represent sparse and undefined values instead of silently
    # equating them with genuine numeric zero.
    train_rows = [row for row in rows if str(row.get("split")) == "train"]
    medians: dict[str, float] = {}
    for field in numeric_fields:
        values = [finite(row.get(field), float("nan")) for row in train_rows]
        clean = [value for value in values if math.isfinite(value)]
        medians[field] = statistics.median(clean) if clean else 0.0

    general_names = []
    for field in numeric_fields:
        general_names.extend((field, f"{field}__missing"))
    identity_names = list(general_names)
    for field, buckets in CATEGORICAL_FIELDS:
        identity_names.extend(f"hash:{field}:{index}" for index in range(buckets))

    x_general = np.zeros((len(rows), len(general_names)), dtype=np.float32)
    x_identity = np.zeros((len(rows), len(identity_names)), dtype=np.float32)
    for row_index, row in enumerate(rows):
        offset = 0
        for field in numeric_fields:
            raw = row.get(field)
            missing = raw is None
            value = finite(raw, medians[field]) if not missing else medians[field]
            x_general[row_index, offset] = value
            x_general[row_index, offset + 1] = float(missing)
            x_identity[row_index, offset] = value
            x_identity[row_index, offset + 1] = float(missing)
            offset += 2
        for field, buckets in CATEGORICAL_FIELDS:
            value = str(row.get(field) or "")
            if value:
                bucket, sign = _hash_bucket(value, field, buckets)
                x_identity[row_index, offset + bucket] = sign
            offset += buckets

    pnl: dict[str, np.ndarray] = {}
    exit_delay: dict[str, np.ndarray] = {}
    for policy in policies:
        matrix = np.asarray(
            [
                [finite(row.get(policy_pnl_key(latency, policy)), -ENTRY_BUDGET_SOL) for latency in LATENCIES]
                for row in rows
            ],
            dtype=np.float64,
        )
        pnl[policy] = matrix
        delays = np.zeros((len(rows), len(LATENCIES)), dtype=np.float64)
        if policy.startswith("hold_"):
            hold_ms = integer(policy.removeprefix("hold_").removesuffix("ms"))
            for column, latency in enumerate(LATENCIES):
                delays[:, column] = hold_ms + latency
        else:
            for column, latency in enumerate(LATENCIES):
                key = policy_exit_key(latency, policy)
                delays[:, column] = np.asarray(
                    [finite(row.get(key), 60_000.0 + latency) for row in rows],
                    dtype=np.float64,
                )
        exit_delay[policy] = delays

    return Corpus(
        rows=rows,
        numeric_fields=numeric_fields,
        feature_names_general=general_names,
        feature_names_identity=identity_names,
        x_general=x_general,
        x_identity=x_identity,
        splits=np.asarray([str(row.get("split") or "") for row in rows], dtype=object),
        create_ns=np.asarray([integer(row.get("create_ns")) for row in rows], dtype=np.int64),
        run_ids=np.asarray([str(row.get("source_run_id") or "") for row in rows], dtype=object),
        mints=np.asarray([str(row.get("mint") or "") for row in rows], dtype=object),
        selected=np.asarray([bool(row.get("selected_by_e4")) for row in rows], dtype=bool),
        landed=np.asarray([bool(row.get("landed_successfully")) for row in rows], dtype=bool),
        policies=policies,
        pnl=pnl,
        exit_delay_ms=exit_delay,
    )


def balanced_weights(target: np.ndarray) -> np.ndarray:
    target = np.asarray(target, dtype=bool)
    positives = max(1, int(target.sum()))
    negatives = max(1, int((~target).sum()))
    weights = np.ones(len(target), dtype=np.float64)
    weights[target] = len(target) / (2.0 * positives)
    weights[~target] = len(target) / (2.0 * negatives)
    return weights


def make_model(family: str, target: np.ndarray, seed: int):
    if family == "logistic":
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=0.08,
                max_iter=3_000,
                class_weight="balanced",
                solver="liblinear",
                random_state=seed,
            ),
        )
    if family == "hgb":
        return HistGradientBoostingClassifier(
            learning_rate=0.045,
            max_iter=260,
            max_leaf_nodes=15,
            min_samples_leaf=25,
            l2_regularization=2.0,
            early_stopping=True,
            validation_fraction=0.12,
            random_state=seed,
        )
    if family == "extra":
        return ExtraTreesClassifier(
            n_estimators=360,
            max_depth=12,
            min_samples_leaf=4,
            max_features="sqrt",
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=seed,
        )
    if family == "forest":
        return RandomForestClassifier(
            n_estimators=320,
            max_depth=12,
            min_samples_leaf=4,
            max_features="sqrt",
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=seed,
        )
    if family == "xgb":
        from xgboost import XGBClassifier

        positives = max(1, int(target.sum()))
        negatives = max(1, int((~target).sum()))
        return XGBClassifier(
            n_estimators=500,
            max_depth=5,
            learning_rate=0.035,
            subsample=0.85,
            colsample_bytree=0.75,
            min_child_weight=8,
            reg_alpha=0.25,
            reg_lambda=4.0,
            objective="binary:logistic",
            eval_metric="logloss",
            scale_pos_weight=negatives / positives,
            n_jobs=-1,
            random_state=seed,
        )
    if family == "cat":
        from catboost import CatBoostClassifier

        return CatBoostClassifier(
            iterations=500,
            depth=6,
            learning_rate=0.035,
            l2_leaf_reg=8.0,
            loss_function="Logloss",
            auto_class_weights="Balanced",
            random_seed=seed,
            verbose=False,
            allow_writing_files=False,
            thread_count=-1,
        )
    raise ValueError(f"unknown family {family}")


def fit_classifier(family: str, x: np.ndarray, target: np.ndarray, seed: int):
    model = make_model(family, target, seed)
    if family == "hgb":
        model.fit(x, target.astype(int), sample_weight=balanced_weights(target))
    else:
        model.fit(x, target.astype(int))
    return model


def predict_score(model: Any, x: np.ndarray) -> np.ndarray:
    if hasattr(model, "predict_proba"):
        return np.asarray(model.predict_proba(x)[:, 1], dtype=np.float64)
    if hasattr(model, "decision_function"):
        raw = np.asarray(model.decision_function(x), dtype=np.float64)
        return 1.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))
    return np.asarray(model.predict(x), dtype=np.float64)


def profit_factor(pnls: Sequence[float]) -> float:
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    if losses > 0:
        return gains / losses
    return 999.0 if gains > 0 else 0.0


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


def chronological_economics(
    indices: Sequence[int],
    scores: np.ndarray,
    corpus: Corpus,
    policy: str,
    latency_column: int,
) -> dict[str, Any]:
    ordered = sorted(
        {int(index) for index in indices},
        key=lambda index: (int(corpus.create_ns[index]), str(corpus.mints[index])),
    )
    balance = STARTING_BANKROLL_SOL
    peak = balance
    maximum_drawdown = 0.0
    active_exits: list[int] = []
    closed: list[dict[str, Any]] = []
    skipped_concurrency = 0
    static_pnl = corpus.pnl[policy][:, latency_column]
    exit_delays = corpus.exit_delay_ms[policy][:, latency_column]

    for index in ordered:
        now = int(corpus.create_ns[index])
        active_exits = [exit_ns for exit_ns in active_exits if exit_ns > now]
        if len(active_exits) >= MAX_CONCURRENT:
            skipped_concurrency += 1
            continue
        available = max(0.0, balance - RESERVE_SOL)
        stake = min(available, balance * POSITION_FRACTION)
        if stake <= 0:
            break
        raw_return = finite(static_pnl[index]) / ENTRY_BUDGET_SOL
        realised = stake * raw_return
        balance += realised
        peak = max(peak, balance)
        if peak > 0:
            maximum_drawdown = max(maximum_drawdown, (peak - balance) / peak)
        exit_ns = now + int(max(0.0, exit_delays[index]) * 1_000_000)
        active_exits.append(exit_ns)
        closed.append(
            {
                "index": index,
                "mint": str(corpus.mints[index]),
                "run_id": str(corpus.run_ids[index]),
                "create_ns": now,
                "score": float(scores[index]),
                "stake_sol": stake,
                "pnl_sol": realised,
                "selected_by_e4": bool(corpus.selected[index]),
                "landed_by_e4": bool(corpus.landed[index]),
            }
        )
    pnls = [row["pnl_sol"] for row in closed]
    wins = sum(value > 0 for value in pnls)
    gains = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    total_profit = sum(gains)
    largest = max(gains, default=0.0)
    return {
        "trades": len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": wins / len(closed) if closed else 0.0,
        "wilson_lower": wilson_lower(wins, len(closed)),
        "net_pnl_sol": balance - STARTING_BANKROLL_SOL,
        "ending_bankroll_sol": balance,
        "profit_factor": profit_factor(pnls),
        "maximum_drawdown_fraction": maximum_drawdown,
        "expectancy_sol": statistics.fmean(pnls) if pnls else 0.0,
        "average_win_sol": statistics.fmean(gains) if gains else 0.0,
        "average_loss_sol": statistics.fmean(losses) if losses else 0.0,
        "largest_winner_profit_share": largest / total_profit if total_profit > 0 else 0.0,
        "capture_windows": len({row["run_id"] for row in closed}),
        "e4_selected_overlap": sum(row["selected_by_e4"] for row in closed),
        "e4_landed_overlap": sum(row["landed_by_e4"] for row in closed),
        "skipped_for_concurrency": skipped_concurrency,
        "closed": closed,
    }


def multi_latency_economics(
    indices: Sequence[int], scores: np.ndarray, corpus: Corpus, policy: str
) -> dict[str, Any]:
    results = {
        str(latency): chronological_economics(
            indices, scores, corpus, policy, column
        )
        for column, latency in enumerate(LATENCIES)
    }
    blocks = list(results.values())
    return {
        "latencies": results,
        "minimum_trades": min((block["trades"] for block in blocks), default=0),
        "minimum_win_rate": min((block["win_rate"] for block in blocks), default=0.0),
        "minimum_wilson_lower": min(
            (block["wilson_lower"] for block in blocks), default=0.0
        ),
        "minimum_profit_factor": min(
            (block["profit_factor"] for block in blocks), default=0.0
        ),
        "minimum_net_pnl_sol": min(
            (block["net_pnl_sol"] for block in blocks), default=-999.0
        ),
        "maximum_drawdown_fraction": max(
            (block["maximum_drawdown_fraction"] for block in blocks), default=1.0
        ),
        "minimum_expectancy_sol": min(
            (block["expectancy_sol"] for block in blocks), default=-999.0
        ),
        "maximum_largest_winner_profit_share": max(
            (block["largest_winner_profit_share"] for block in blocks), default=1.0
        ),
        "minimum_capture_windows": min(
            (block["capture_windows"] for block in blocks), default=0
        ),
    }


def threshold_candidates(scores: np.ndarray, mask: np.ndarray) -> list[float]:
    values = scores[mask]
    if not len(values):
        return []
    thresholds = {
        float(np.quantile(values, quantile))
        for quantile in (
            0.90,
            0.95,
            0.97,
            0.98,
            0.985,
            0.99,
            0.9925,
            0.995,
            0.997,
            0.998,
            0.999,
            0.9995,
        )
    }
    ordered = np.sort(values)[::-1]
    for count in (8, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 250):
        if len(ordered) >= count:
            thresholds.add(float(ordered[count - 1]))
    return sorted(thresholds, reverse=True)


def objective(metrics: Mapping[str, Any]) -> tuple[float, ...]:
    return (
        finite(metrics.get("minimum_win_rate")),
        finite(metrics.get("minimum_wilson_lower")),
        min(20.0, finite(metrics.get("minimum_profit_factor"))),
        finite(metrics.get("minimum_net_pnl_sol")),
        -finite(metrics.get("maximum_drawdown_fraction")),
        finite(metrics.get("minimum_trades")),
    )


def validation_eligible(metrics: Mapping[str, Any]) -> bool:
    return bool(
        integer(metrics.get("minimum_trades")) >= 8
        and finite(metrics.get("minimum_win_rate")) >= 0.58
        and finite(metrics.get("minimum_profit_factor")) >= 1.05
        and finite(metrics.get("minimum_net_pnl_sol")) > 0
        and finite(metrics.get("maximum_drawdown_fraction")) <= 0.25
    )


def historical_pass(metrics: Mapping[str, Any]) -> tuple[bool, list[str]]:
    failures = []
    checks = {
        "at_least_20_trades": integer(metrics.get("minimum_trades")) >= 20,
        "at_least_two_windows": integer(metrics.get("minimum_capture_windows")) >= 2,
        "win_rate_at_least_65_percent": finite(metrics.get("minimum_win_rate")) >= 0.65,
        "wilson_lower_at_least_45_percent": finite(metrics.get("minimum_wilson_lower")) >= 0.45,
        "profit_factor_at_least_1_25": finite(metrics.get("minimum_profit_factor")) >= 1.25,
        "pnl_positive_every_latency": finite(metrics.get("minimum_net_pnl_sol")) > 0,
        "drawdown_at_most_15_percent": finite(metrics.get("maximum_drawdown_fraction")) <= 0.15,
        "expectancy_positive_every_latency": finite(metrics.get("minimum_expectancy_sol")) > 0,
        "largest_winner_at_most_50_percent": finite(metrics.get("maximum_largest_winner_profit_share")) <= 0.50,
    }
    failures.extend(name for name, passed in checks.items() if not passed)
    return not failures, failures


def fit_scores(
    lane: str,
    corpus: Corpus,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
    holdout_mask: np.ndarray,
    policy: str,
    seed: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    use_identity = lane.endswith("-identity") or lane in {"cat", "xgb-identity", "multitask-identity"}
    x = corpus.x_identity if use_identity else corpus.x_general
    family = lane.replace("-identity", "")
    worst_pnl = corpus.pnl[policy].min(axis=1)
    profit_target = worst_pnl > 0
    details: dict[str, Any] = {
        "feature_setting": "identity-aware-hashed" if use_identity else "generalised-history",
        "profit_target_train_prevalence": float(profit_target[train_mask].mean()),
        "e4_intent_train_prevalence": float(corpus.selected[train_mask].mean()),
    }

    if family.startswith("intent-"):
        model_family = family.removeprefix("intent-")
        model = fit_classifier(model_family, x[train_mask], corpus.selected[train_mask], seed)
        scores = predict_score(model, x)
        details["model_family"] = model_family
        details["target"] = "E4 selection intent"
        return scores, details

    if family.startswith("profit-"):
        model_family = family.removeprefix("profit-")
        model = fit_classifier(model_family, x[train_mask], profit_target[train_mask], seed)
        scores = predict_score(model, x)
        details["model_family"] = model_family
        details["target"] = "profitable at every latency"
        return scores, details

    if family == "utility-extra":
        target = worst_pnl
        model = ExtraTreesRegressor(
            n_estimators=400,
            max_depth=14,
            min_samples_leaf=4,
            max_features="sqrt",
            n_jobs=-1,
            random_state=seed,
        )
        model.fit(x[train_mask], target[train_mask])
        details["model_family"] = "ExtraTreesRegressor"
        details["target"] = "worst-latency PnL"
        return np.asarray(model.predict(x), dtype=np.float64), details

    if family == "utility-hgb":
        target = worst_pnl
        model = HistGradientBoostingRegressor(
            learning_rate=0.04,
            max_iter=300,
            max_leaf_nodes=15,
            min_samples_leaf=25,
            l2_regularization=3.0,
            random_state=seed,
        )
        model.fit(x[train_mask], target[train_mask])
        details["model_family"] = "HistGradientBoostingRegressor"
        details["target"] = "worst-latency PnL"
        return np.asarray(model.predict(x), dtype=np.float64), details

    if family in {"multitask", "multitask-hgb"}:
        intent = fit_classifier("hgb", x[train_mask], corpus.selected[train_mask], seed)
        profit = fit_classifier("hgb", x[train_mask], profit_target[train_mask], seed + 1)
        intent_score = predict_score(intent, x)
        profit_score = predict_score(profit, x)
        details["model_family"] = "two-head HGB"
        details["target"] = "geometric combination of E4 intent and profit"
        details["component_hash"] = stable_hash(
            {
                "intent_mean": float(intent_score[validation_mask].mean()),
                "profit_mean": float(profit_score[validation_mask].mean()),
            }
        )
        # Alpha is selected later by exposing several deterministic blends as a
        # packed score.  The default equal-weight geometric mean is deliberately
        # conservative about either head assigning near-zero confidence.
        return np.sqrt(np.clip(intent_score, 1e-9, 1) * np.clip(profit_score, 1e-9, 1)), details

    if family == "pu-bagging":
        positives = np.flatnonzero(train_mask & corpus.selected)
        unlabeled = np.flatnonzero(train_mask & ~corpus.selected)
        if len(positives) < 20:
            raise ValueError("insufficient positives for PU bagging")
        rng = np.random.default_rng(seed)
        score_sum = np.zeros(len(corpus.rows), dtype=np.float64)
        models = 12
        sample_size = min(len(unlabeled), len(positives) * 20)
        for iteration in range(models):
            sampled = rng.choice(unlabeled, size=sample_size, replace=False)
            indexes = np.concatenate((positives, sampled))
            target = np.concatenate(
                (np.ones(len(positives), dtype=bool), np.zeros(len(sampled), dtype=bool))
            )
            model = fit_classifier("hgb", x[indexes], target, seed + iteration)
            score_sum += predict_score(model, x)
        details["model_family"] = "12-member PU HGB bagging"
        details["target"] = "E4 intent with unlabeled negatives"
        return score_sum / models, details

    raise ValueError(f"unsupported lane {lane}")


def evaluate_lane(corpus: Corpus, lane: str, seed: int) -> dict[str, Any]:
    train_mask = corpus.splits == "train"
    validation_mask = corpus.splits == "validation"
    holdout_mask = corpus.splits == "holdout"
    if not all(mask.any() for mask in (train_mask, validation_mask, holdout_mask)):
        raise ValueError("train/validation/holdout split is incomplete")

    candidates = []
    for policy_index, policy in enumerate(corpus.policies):
        scores, details = fit_scores(
            lane,
            corpus,
            train_mask,
            validation_mask,
            holdout_mask,
            policy,
            seed + policy_index * 17,
        )
        for threshold in threshold_candidates(scores, validation_mask):
            indexes = np.flatnonzero(validation_mask & (scores >= threshold))
            metrics = multi_latency_economics(indexes, scores, corpus, policy)
            candidates.append(
                {
                    "policy": policy,
                    "threshold": threshold,
                    "model": details,
                    "validation": metrics,
                    "eligible": validation_eligible(metrics),
                    "scores": scores,
                }
            )

    eligible = [row for row in candidates if row["eligible"]]
    pool = eligible or candidates
    if not pool:
        raise ValueError("no candidate thresholds were generated")
    winner = max(pool, key=lambda row: objective(row["validation"]))
    holdout_indexes = np.flatnonzero(
        holdout_mask & (winner["scores"] >= winner["threshold"])
    )
    holdout = multi_latency_economics(
        holdout_indexes, winner["scores"], corpus, winner["policy"]
    )
    passed, failures = historical_pass(holdout)

    # Baselines use the identical frozen policy and holdout chronology.
    baseline_scores = {
        "youngest_launch": -np.asarray(
            [finite(row.get("decision_delay_ms"), 0.0) for row in corpus.rows]
        ),
        "highest_creator_seed": np.asarray(
            [finite(row.get("creator_buy_sol_0ms")) for row in corpus.rows]
        ),
        "strongest_creator_history": np.asarray(
            [finite(row.get("creator_prior_selection_count")) for row in corpus.rows]
        ),
        "highest_early_flow": np.asarray(
            [finite(row.get("outside_buy_sol_0ms")) for row in corpus.rows]
        ),
    }
    baseline_count = max(1, len(holdout_indexes))
    baselines = {}
    holdout_ids = np.flatnonzero(holdout_mask)
    for name, values in baseline_scores.items():
        ranked = holdout_ids[np.argsort(values[holdout_ids], kind="stable")[::-1]]
        selected = ranked[:baseline_count]
        baselines[name] = multi_latency_economics(
            selected, values, corpus, winner["policy"]
        )

    compact_candidates = sorted(
        (
            {
                "policy": row["policy"],
                "threshold": row["threshold"],
                "eligible": row["eligible"],
                "objective": objective(row["validation"]),
                "validation": {
                    key: value
                    for key, value in row["validation"].items()
                    if key != "latencies"
                },
            }
            for row in candidates
        ),
        key=lambda row: tuple(row["objective"]),
        reverse=True,
    )[:100]

    return {
        "version": SCHEMA_VERSION,
        "lane": lane,
        "seed": seed,
        "horizon_ms": 0,
        "dataset": {
            "rows": len(corpus.rows),
            "train": int(train_mask.sum()),
            "validation": int(validation_mask.sum()),
            "holdout": int(holdout_mask.sum()),
            "selected_total": int(corpus.selected.sum()),
            "selected_train": int((train_mask & corpus.selected).sum()),
            "selected_validation": int((validation_mask & corpus.selected).sum()),
            "selected_holdout": int((holdout_mask & corpus.selected).sum()),
            "policies": corpus.policies,
        },
        "winner": {
            "policy": winner["policy"],
            "threshold": winner["threshold"],
            "model": winner["model"],
            "validation": winner["validation"],
            "holdout": holdout,
            "historical_pass": passed,
            "failed_requirements": failures,
            "selected_holdout_launches": len(holdout_indexes),
            "selected_holdout_mints_sha256": stable_hash(
                sorted(str(corpus.mints[index]) for index in holdout_indexes)
            ),
        },
        "baselines": baselines,
        "top_validation_candidates": compact_candidates,
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "production_paths_changed": 0,
        "live_trading": False,
    }


def rule_predicates(corpus: Corpus, train_mask: np.ndarray) -> list[tuple[str, str, float, np.ndarray]]:
    values = corpus.x_general
    names = corpus.feature_names_general
    predicates = []
    for column, name in enumerate(names):
        if name.endswith("__missing"):
            continue
        train_values = values[train_mask, column]
        if not np.isfinite(train_values).any() or np.nanstd(train_values) <= 1e-12:
            continue
        for quantile in (0.05, 0.10, 0.20, 0.30, 0.50, 0.70, 0.80, 0.90, 0.95, 0.98, 0.99):
            threshold = float(np.quantile(train_values, quantile))
            predicates.append((name, ">=", threshold, values[:, column] >= threshold))
            predicates.append((name, "<=", threshold, values[:, column] <= threshold))
    return predicates


def evaluate_rules(corpus: Corpus, seed: int) -> dict[str, Any]:
    train_mask = corpus.splits == "train"
    validation_mask = corpus.splits == "validation"
    holdout_mask = corpus.splits == "holdout"
    predicates = rule_predicates(corpus, train_mask)
    best: dict[str, Any] | None = None
    diagnostics = []

    for policy in corpus.policies:
        target = corpus.pnl[policy].min(axis=1) > 0
        singles = []
        for name, operator, threshold, mask in predicates:
            chosen = train_mask & mask
            support = int(chosen.sum())
            if support < 8:
                continue
            precision = float(target[chosen].mean())
            e4_precision = float(corpus.selected[chosen].mean())
            singles.append((precision, e4_precision, support, name, operator, threshold, mask))
        singles.sort(reverse=True, key=lambda row: (row[0], row[1], row[2]))
        top = singles[:48]
        formulae: list[tuple[list[tuple[str, str, float]], np.ndarray]] = []
        for item in top:
            formulae.append(([(item[3], item[4], item[5])], item[6]))
        for left_index, left in enumerate(top[:30]):
            for right in top[left_index + 1 : 30]:
                if left[3] == right[3]:
                    continue
                formulae.append(
                    (
                        [(left[3], left[4], left[5]), (right[3], right[4], right[5])],
                        left[6] & right[6],
                    )
                )
        # Only the strongest pair foundations are extended to triples.
        pair_ranked = []
        for formula, mask in formulae:
            chosen = train_mask & mask
            if len(formula) != 2 or chosen.sum() < 8:
                continue
            pair_ranked.append((float(target[chosen].mean()), int(chosen.sum()), formula, mask))
        pair_ranked.sort(reverse=True, key=lambda row: (row[0], row[1]))
        for _, _, formula, mask in pair_ranked[:30]:
            used = {item[0] for item in formula}
            for extra in top[:20]:
                if extra[3] in used:
                    continue
                formulae.append((formula + [(extra[3], extra[4], extra[5])], mask & extra[6]))

        for formula, mask in formulae:
            validation_indices = np.flatnonzero(validation_mask & mask)
            if len(validation_indices) < 8:
                continue
            scores = mask.astype(np.float64)
            metrics = multi_latency_economics(
                validation_indices, scores, corpus, policy
            )
            candidate = {
                "policy": policy,
                "formula": formula,
                "validation": metrics,
            }
            diagnostics.append(
                {
                    "policy": policy,
                    "formula": formula,
                    "objective": objective(metrics),
                    "eligible": validation_eligible(metrics),
                }
            )
            if best is None:
                best = candidate
            elif validation_eligible(metrics) and not validation_eligible(best["validation"]):
                best = candidate
            elif validation_eligible(metrics) == validation_eligible(best["validation"]) and objective(metrics) > objective(best["validation"]):
                best = candidate

    if best is None:
        return {
            "version": SCHEMA_VERSION,
            "lane": "rules",
            "status": "NOT_CONCLUSIVE",
            "reason": "no rule had eight validation trades",
            "predicates_tested": len(predicates),
        }
    holdout_mask_formula = np.ones(len(corpus.rows), dtype=bool)
    name_to_column = {name: index for index, name in enumerate(corpus.feature_names_general)}
    for name, operator, threshold in best["formula"]:
        values = corpus.x_general[:, name_to_column[name]]
        holdout_mask_formula &= values >= threshold if operator == ">=" else values <= threshold
    scores = holdout_mask_formula.astype(np.float64)
    holdout_indices = np.flatnonzero(holdout_mask & holdout_mask_formula)
    holdout = multi_latency_economics(holdout_indices, scores, corpus, best["policy"])
    passed, failures = historical_pass(holdout)
    return {
        "version": SCHEMA_VERSION,
        "lane": "rules",
        "seed": seed,
        "winner": {
            "policy": best["policy"],
            "formula": best["formula"],
            "validation": best["validation"],
            "holdout": holdout,
            "historical_pass": passed,
            "failed_requirements": failures,
        },
        "top_validation_candidates": sorted(
            diagnostics, key=lambda row: tuple(row["objective"]), reverse=True
        )[:200],
        "status": "HISTORICAL_CANDIDATE" if passed else "NOT_CONCLUSIVE",
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--lane",
        choices=(
            "rules",
            "intent-logistic",
            "intent-hgb",
            "intent-extra",
            "intent-forest",
            "intent-xgb",
            "intent-cat",
            "intent-hgb-identity",
            "profit-logistic",
            "profit-hgb",
            "profit-extra",
            "profit-forest",
            "profit-xgb",
            "profit-cat",
            "profit-hgb-identity",
            "utility-extra",
            "utility-hgb",
            "multitask",
            "multitask-identity",
            "pu-bagging",
        ),
        required=True,
    )
    parser.add_argument("--horizon-ms", type=int, default=0)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.horizon_ms not in (0, 1, 2, 5, 10):
        raise SystemExit("initial all-out tournament permits only causal 0-10ms feature horizons")

    corpus = load_corpus(args.corpus, args.horizon_ms)
    report = (
        evaluate_rules(corpus, args.seed)
        if args.lane == "rules"
        else evaluate_lane(corpus, args.lane, args.seed)
    )
    report["corpus_sha256"] = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["experiment_id"] = "e4x-allout-" + stable_hash(
        {
            "version": SCHEMA_VERSION,
            "lane": args.lane,
            "horizon_ms": args.horizon_ms,
            "seed": args.seed,
            "corpus_sha256": report["corpus_sha256"],
        }
    )
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "lane": args.lane,
                "status": report.get("status"),
                "experiment_id": report["experiment_id"],
                "winner": {
                    key: value
                    for key, value in (report.get("winner") or {}).items()
                    if key in {"policy", "threshold", "formula", "historical_pass", "failed_requirements"}
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
