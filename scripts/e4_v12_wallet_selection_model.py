#!/usr/bin/env python3
"""Reconstruct E4 intent without copying E4's transactions.

This is an offline, research-only pipeline.  It learns whether E4 would submit a
buy (landed or failed), using only launch data available by a declared decision
horizon.  Model/threshold/exit choices are frozen on train+validation captures
before the final ten chronological captures are opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import sys
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e4_v12_profit_survival_search as base

SCHEMA_VERSION = "e4-v12-wallet-selection-v1"
RANDOM_SEED = 12_498
DECISION_HORIZONS_MS = (0, 5, 20, 250)
EXECUTION_HORIZON_MS = 250
E4_WALLET = base.choice_sets.E4_WALLET
STARTING_BANKROLL_SOL = 3.0
POSITION_FRACTION = 0.10
MAX_CONCURRENT_POSITIONS = 2
VARIABLE_FEE_BPS = 175.0
FIXED_TRANSACTION_FEE_SOL = 0.002005
TEXT_BUCKETS = 32


@dataclass(frozen=True, slots=True)
class LaunchRow:
    run_id: str
    split: str
    mint: str
    create_ns: int
    horizon_ms: int
    features: tuple[float, ...]
    selected: bool
    landed: bool


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    stop: float
    first_take: float
    first_fraction: float
    hold_ms: int
    trail_retrace: float

    @property
    def key(self) -> str:
        return (
            f"stop={self.stop:.2f}|first={self.first_take:.2f}:"
            f"{self.first_fraction:.2f}|hold={self.hold_ms}|trail={self.trail_retrace:.2f}"
        )


POLICIES = tuple(
    ExitPolicy(stop, take, fraction, hold, trail)
    for stop in (0.70, 0.80)
    for take in (1.15, 1.30, 1.60)
    for fraction in (0.0, 0.20, 0.30)
    for hold in (2_000, 4_000, 10_000)
    for trail in (0.15, 0.25)
)

BASE_FEATURE_NAMES = (
    "horizon_ms",
    "creator_seed_sol",
    "creator_buy_count",
    "public_buy_sol",
    "public_buy_count",
    "unique_public_buyers",
    "public_signature_count",
    "maximum_public_buy_sol",
    "median_public_buy_sol",
    "buyer_concentration",
    "same_create_signature_buyers",
    "same_create_slot_buyers",
    "initial_fdv_usd",
    "initial_price_sol",
    "initial_virtual_sol",
    "initial_virtual_tokens",
    "initial_real_tokens",
    "creator_prior_launches",
    "creator_prior_e4_attempts",
    "creator_prior_e4_landed",
    "creator_prior_e4_failed",
    "milliseconds_since_creator_launch",
    "launches_previous_1s",
    "launches_previous_10s",
    "name_length",
    "symbol_length",
    "name_digit_fraction",
    "symbol_upper_fraction",
    "metadata_content_addressed",
    "mayhem_mode",
    "cashback_enabled",
)
FEATURE_NAMES = (*BASE_FEATURE_NAMES, *(f"text_hash_{index}" for index in range(TEXT_BUCKETS)))


def finite(value: Any, default: float = 0.0) -> float:
    return base.finite(value, default)


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def text_hash_features(name: str, symbol: str) -> tuple[float, ...]:
    text = f"{name.lower()} | {symbol.lower()}"
    padded = f"  {text}  "
    tokens = text.split() + [padded[index : index + 3] for index in range(len(padded) - 2)]
    values = [0.0] * TEXT_BUCKETS
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).digest()
        bucket = int.from_bytes(digest[:2], "big") % TEXT_BUCKETS
        values[bucket] += -1.0 if digest[2] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return tuple(value / norm for value in values)


def load_labels(path: Path) -> dict[tuple[str, str], bool]:
    labels: dict[tuple[str, str], bool] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if not row.get("selected_by_e4"):
                continue
            key = (str(row["source_run_id"]), str(row["candidate_mint"]))
            landed = bool(row.get("landed_successfully"))
            if key in labels and labels[key] != landed:
                raise ValueError(f"conflicting E4 label for {key}")
            labels[key] = landed
    return labels


def load_create_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if '"kind":"CREATE"' not in line:
                continue
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if mint and mint not in rows:
                rows[mint] = row
    return rows


def _fraction(predicate: Any, text: str) -> float:
    return sum(bool(predicate(char)) for char in text) / max(len(text), 1)


def selector_features(
    trace: base.Trace,
    create_row: Mapping[str, Any],
    horizon_ms: int,
    *,
    creator_prior: tuple[int, int, int, int, int | None],
    launches_1s: int,
    launches_10s: int,
) -> tuple[float, ...]:
    """Return causal features with every E4-authored event removed."""
    cutoff = trace.create_ns + horizon_ms * 1_000_000
    observed = [point for point in trace.points if point.timestamp_ns <= cutoff]
    public_buys = [
        point
        for point in observed
        if point.kind in base.choice_sets.BUY_KINDS
        and point.trader
        and point.trader not in {trace.creator, E4_WALLET}
    ]
    creator_buys = [
        point
        for point in observed
        if point.kind in base.choice_sets.BUY_KINDS and point.trader == trace.creator
    ]
    amounts = [point.sol_amount for point in public_buys if point.sol_amount > 0]
    by_buyer: Counter[str] = Counter()
    for point in public_buys:
        by_buyer[point.trader] += point.sol_amount
    initial = next(
        (
            point
            for point in trace.points
            if point.timestamp_ns == trace.create_ns
            and point.virtual_sol > 0
            and point.virtual_tokens > 0
        ),
        None,
    )
    raw = create_row.get("raw") if isinstance(create_row.get("raw"), Mapping) else {}
    name = str(raw.get("name") or create_row.get("name") or "")
    symbol = str(raw.get("symbol") or create_row.get("symbol") or "")
    uri = str(raw.get("uri") or create_row.get("uri") or "").lower()
    prior_launches, prior_attempts, prior_landed, prior_failed, previous_ns = creator_prior
    elapsed = (
        (trace.create_ns - previous_ns) / 1_000_000 if previous_ns is not None else 0.0
    )
    public_total = sum(amounts)
    base_values = (
        float(horizon_ms),
        sum(point.sol_amount for point in creator_buys),
        float(len(creator_buys)),
        public_total,
        float(len(public_buys)),
        float(len({point.trader for point in public_buys})),
        float(len({point.signature for point in public_buys if point.signature})),
        max(amounts, default=0.0),
        float(np.median(amounts)) if amounts else 0.0,
        max(by_buyer.values(), default=0.0) / max(public_total, 1e-12),
        float(
            len(
                {
                    point.trader
                    for point in public_buys
                    if point.signature == trace.create_signature
                }
            )
        ),
        float(len({point.trader for point in public_buys if point.slot == trace.create_slot})),
        finite(create_row.get("fdv_usd")),
        finite(initial.price_sol if initial else 0.0),
        finite(initial.virtual_sol if initial else 0.0),
        finite(initial.virtual_tokens if initial else 0.0),
        finite(initial.real_tokens if initial else 0.0),
        float(prior_launches),
        float(prior_attempts),
        float(prior_landed),
        float(prior_failed),
        elapsed,
        float(launches_1s),
        float(launches_10s),
        float(len(name)),
        float(len(symbol)),
        _fraction(str.isdigit, name),
        _fraction(str.isupper, symbol),
        float("ipfs" in uri or "arweave" in uri),
        float(bool(raw.get("is_mayhem_mode"))),
        float(bool(raw.get("is_cashback_enabled"))),
    )
    result = (*base_values, *text_hash_features(name, symbol))
    if len(result) != len(FEATURE_NAMES) or not np.isfinite(result).all():
        raise ValueError("invalid selector feature vector")
    return result


def extract_dataset(
    root: Path,
    *,
    include_holdout: bool,
    only_holdout: bool = False,
) -> tuple[list[LaunchRow], dict[tuple[str, str], base.Trace], dict[str, Any]]:
    labels = load_labels(root / "artifacts/e4-v12-canonical-choice-risksets-v2.jsonl")
    specs = base.capture_specs(root, include_holdout=include_holdout)
    if not only_holdout:
        specs = [spec for spec in specs if spec.split != "holdout"]
    rows: list[LaunchRow] = []
    trace_index: dict[tuple[str, str], base.Trace] = {}
    creator_counts: Counter[str] = Counter()
    creator_attempts: Counter[str] = Counter()
    creator_landed: Counter[str] = Counter()
    creator_failed: Counter[str] = Counter()
    creator_last: dict[str, int] = {}
    launch_times: deque[int] = deque()
    audit = []
    for spec in specs:
        emit_rows = not only_holdout or spec.split == "holdout"
        actual = base.sha256_path(spec.events_path)
        if actual != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        print(f"extract selector {spec.split} capture {spec.run_id}", flush=True)
        creates = load_create_rows(spec.events_path)
        traces, parse_errors = base.load_capture(spec, Counter())
        traces.sort(key=lambda trace: (trace.create_ns, trace.mint))
        run_counts = Counter()
        for trace in traces:
            create_row = creates.get(trace.mint)
            if create_row is None:
                run_counts["missing_create"] += 1
                continue
            while launch_times and launch_times[0] < trace.create_ns - 10_000_000_000:
                launch_times.popleft()
            launches_10s = len(launch_times)
            launches_1s = sum(value >= trace.create_ns - 1_000_000_000 for value in launch_times)
            prior = (
                creator_counts[trace.creator],
                creator_attempts[trace.creator],
                creator_landed[trace.creator],
                creator_failed[trace.creator],
                creator_last.get(trace.creator),
            )
            selected_key = (spec.run_id, trace.mint)
            selected = selected_key in labels
            landed = labels.get(selected_key, False)
            if emit_rows:
                for horizon in DECISION_HORIZONS_MS:
                    rows.append(
                        LaunchRow(
                            run_id=spec.run_id,
                            split=spec.split,
                            mint=trace.mint,
                            create_ns=trace.create_ns,
                            horizon_ms=horizon,
                            features=selector_features(
                                trace,
                                create_row,
                                horizon,
                                creator_prior=prior,
                                launches_1s=launches_1s,
                                launches_10s=launches_10s,
                            ),
                            selected=selected,
                            landed=landed,
                        )
                    )
                trace_index[selected_key] = trace
            creator_counts[trace.creator] += 1
            creator_last[trace.creator] = trace.create_ns
            if selected:
                creator_attempts[trace.creator] += 1
                creator_landed[trace.creator] += int(landed)
                creator_failed[trace.creator] += int(not landed)
            launch_times.append(trace.create_ns)
            run_counts["launches"] += 1
            run_counts["selected"] += int(selected)
            run_counts["landed"] += int(landed)
        audit.append(
            {
                "run_id": spec.run_id,
                "split": spec.split,
                "sha256": actual,
                "hash_match": True,
                "parse_errors": parse_errors,
                "history_warmup_only": not emit_rows,
                "emitted_launches": run_counts.get("launches", 0) if emit_rows else 0,
                "emitted_selected": run_counts.get("selected", 0) if emit_rows else 0,
                "emitted_landed": run_counts.get("landed", 0) if emit_rows else 0,
                **dict(run_counts),
            }
        )
    return rows, trace_index, {
        "schema_version": SCHEMA_VERSION,
        "runs": audit,
        "launches": sum(item["emitted_launches"] for item in audit),
        "selected_attempts": sum(item["emitted_selected"] for item in audit),
        "landed_attempts": sum(item["emitted_landed"] for item in audit),
        "failed_attempts": sum(
            item["emitted_selected"] - item["emitted_landed"] for item in audit
        ),
        "history_warmup_launches": sum(
            item.get("launches", 0) for item in audit if item["history_warmup_only"]
        ),
        "e4_events_excluded_from_features": True,
        "production_paths_changed": 0,
    }


def matrix(rows: Sequence[LaunchRow]) -> np.ndarray:
    return np.asarray([row.features for row in rows], dtype=float)


def fit_models(
    rows: Sequence[LaunchRow], *, target: str = "selected"
) -> dict[str, tuple[Any, StandardScaler | None]]:
    x = matrix(rows)
    y = np.asarray([getattr(row, target) for row in rows], dtype=int)
    positive_weight = max((len(y) - int(y.sum())) / max(int(y.sum()), 1), 1.0)
    weights = np.where(y == 1, positive_weight, 1.0)
    scaler = StandardScaler().fit(x)
    logistic = LogisticRegression(
        C=0.1, class_weight="balanced", max_iter=2_000, random_state=RANDOM_SEED
    ).fit(scaler.transform(x), y)
    hgb = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=180,
        max_leaf_nodes=15,
        min_samples_leaf=30,
        l2_regularization=8.0,
        random_state=RANDOM_SEED,
    ).fit(x, y, sample_weight=weights)
    extra = ExtraTreesClassifier(
        n_estimators=300,
        min_samples_leaf=8,
        max_features=0.7,
        class_weight="balanced",
        n_jobs=-1,
        random_state=RANDOM_SEED,
    ).fit(x, y)
    return {"logistic": (logistic, scaler), "hgb": (hgb, None), "extra_trees": (extra, None)}


def score_model(model: Any, scaler: StandardScaler | None, rows: Sequence[LaunchRow]) -> np.ndarray:
    x = matrix(rows)
    if scaler is not None:
        x = scaler.transform(x)
    return model.predict_proba(x)[:, 1]


def intent_metrics(rows: Sequence[LaunchRow], scores: np.ndarray, threshold: float) -> dict[str, Any]:
    selected = scores >= threshold
    truth = np.asarray([row.selected for row in rows], dtype=bool)
    landed = np.asarray([row.landed for row in rows], dtype=bool)
    tp = int(np.sum(selected & truth))
    predicted = int(selected.sum())
    positives = int(truth.sum())
    return {
        "launches": len(rows),
        "e4_attempts": positives,
        "e4_landed": int(landed.sum()),
        "predicted": predicted,
        "true_positive_attempts": tp,
        "true_positive_landed": int(np.sum(selected & landed)),
        "precision": tp / max(predicted, 1),
        "recall": tp / max(positives, 1),
        "average_precision": float(average_precision_score(truth, scores)),
        "roc_auc": float(roc_auc_score(truth, scores)),
    }


def _sell_quote(tokens: float, point: base.Point) -> float:
    return tokens * point.virtual_sol / max(point.virtual_tokens + tokens, 1e-12)


def trade_result(
    trace: base.Trace,
    policy: ExitPolicy,
    position_sol: float,
    *,
    latency_ms: int,
    decision_horizon_ms: int = EXECUTION_HORIZON_MS,
) -> tuple[float, int, str] | None:
    entry_ns = trace.create_ns + (decision_horizon_ms + latency_ms) * 1_000_000
    entry = base.latest_point(trace.points, entry_ns)
    create = base.latest_point(trace.points, trace.create_ns)
    if entry is None or create is None or entry.complete:
        return None
    create_price = create.price_sol or create.virtual_sol / max(create.virtual_tokens, 1e-12)
    entry_price = entry.price_sol or entry.virtual_sol / max(entry.virtual_tokens, 1e-12)
    if entry_price / max(create_price, 1e-18) > 1.50:
        return None
    tokens = base.choice_sets.buy_tokens(position_sol, entry.virtual_sol, entry.virtual_tokens)
    if entry.real_tokens > 0:
        tokens = min(tokens, entry.real_tokens)
    create_output = base.choice_sets.buy_tokens(position_sol, create.virtual_sol, create.virtual_tokens)
    if tokens <= 0 or tokens / max(create_output, 1e-12) < 0.65:
        return None
    deadline = entry_ns + policy.hold_ms * 1_000_000
    peak = 1.0
    remaining = tokens
    proceeds = 0.0
    exits = 0
    first_done = False
    last = entry
    reason = "MAXIMUM_HOLD"
    for point in trace.points:
        if point.timestamp_ns <= entry_ns:
            continue
        if point.timestamp_ns > deadline:
            break
        if point.price_sol <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
            continue
        last = point
        multiple = point.price_sol / max(entry_price, 1e-18)
        peak = max(peak, multiple)
        if policy.first_fraction > 0 and not first_done and multiple >= policy.first_take:
            amount = tokens * policy.first_fraction
            proceeds += _sell_quote(amount, point) * (1 - VARIABLE_FEE_BPS / 10_000)
            remaining -= amount
            exits += 1
            first_done = True
        floor = policy.stop
        if first_done:
            floor = max(floor, 1.02, peak * (1 - policy.trail_retrace))
        elif peak >= policy.first_take:
            floor = max(floor, peak * (1 - policy.trail_retrace))
        if point.complete or multiple <= floor or (policy.first_fraction == 0 and multiple >= policy.first_take):
            reason = (
                "LIQUIDITY_EMERGENCY"
                if point.complete
                else "TAKE_PROFIT"
                if policy.first_fraction == 0 and multiple >= policy.first_take
                else "TRAILING_OR_STOP"
            )
            break
    if remaining > 0:
        proceeds += _sell_quote(remaining, last) * (1 - VARIABLE_FEE_BPS / 10_000)
        exits += 1
    entry_cost = position_sol * (1 + VARIABLE_FEE_BPS / 10_000) + FIXED_TRANSACTION_FEE_SOL
    pnl = proceeds - entry_cost - exits * FIXED_TRANSACTION_FEE_SOL
    exit_ns = last.timestamp_ns
    return pnl, exit_ns, reason


def simulate(
    rows: Sequence[LaunchRow],
    traces: Mapping[tuple[str, str], base.Trace],
    scores: np.ndarray,
    threshold: float,
    policy: ExitPolicy,
    *,
    latency_ms: int,
    decision_horizon_ms: int = EXECUTION_HORIZON_MS,
) -> dict[str, Any]:
    candidates = sorted(
        ((row.create_ns, row, float(score)) for row, score in zip(rows, scores) if score >= threshold),
        key=lambda item: (item[0], item[1].run_id, item[1].mint),
    )
    bankroll = STARTING_BANKROLL_SOL
    peak = bankroll
    maximum_drawdown = 0.0
    active: list[int] = []
    ledger = []
    rejected = Counter()
    for _, row, score in candidates:
        active = [end for end in active if end > row.create_ns]
        if len(active) >= MAX_CONCURRENT_POSITIONS:
            rejected["concurrency"] += 1
            continue
        position = bankroll * POSITION_FRACTION
        result = trade_result(
            traces[(row.run_id, row.mint)],
            policy,
            position,
            latency_ms=latency_ms,
            decision_horizon_ms=decision_horizon_ms,
        )
        if result is None:
            rejected["execution_guard"] += 1
            continue
        pnl, exit_ns, reason = result
        bankroll += pnl
        peak = max(peak, bankroll)
        maximum_drawdown = max(maximum_drawdown, peak - bankroll)
        active.append(exit_ns)
        ledger.append(
            {
                "run_id": row.run_id,
                "mint": row.mint,
                "score": score,
                "e4_attempted": row.selected,
                "e4_landed": row.landed,
                "pnl_sol": pnl,
                "win": pnl > 0,
                "exit_reason": reason,
            }
        )
    profits = [item["pnl_sol"] for item in ledger if item["pnl_sol"] > 0]
    losses = [item["pnl_sol"] for item in ledger if item["pnl_sol"] <= 0]
    wins = len(profits)
    return {
        "latency_ms": latency_ms,
        "decision_horizon_ms": decision_horizon_ms,
        "trades": len(ledger),
        "wins": wins,
        "win_rate": wins / max(len(ledger), 1),
        "wilson_lower_bound": base.wilson_lower_bound(wins, len(ledger)),
        "net_pnl_sol": bankroll - STARTING_BANKROLL_SOL,
        "ending_bankroll_sol": bankroll,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": maximum_drawdown / STARTING_BANKROLL_SOL,
        "capture_windows": len({item["run_id"] for item in ledger}),
        "e4_overlap": sum(item["e4_attempted"] for item in ledger),
        "rejected": dict(rejected),
        "ledger_hash": base.stable_hash(ledger),
        "ledger": ledger,
    }


def candidate_rank(economics: Mapping[str, Any], intent: Mapping[str, Any]) -> tuple[Any, ...]:
    gates = (
        economics["trades"] >= 25,
        economics["capture_windows"] >= 3,
        economics["net_pnl_sol"] > 0,
        economics["profit_factor"] >= 1.25,
        economics["maximum_drawdown_fraction"] <= 0.15,
        intent["precision"] >= 0.20,
        intent["recall"] >= 0.30,
    )
    return (sum(gates), *gates, economics["net_pnl_sol"], economics["profit_factor"], intent["average_precision"])


def wallet_oracle_search(
    rows: Sequence[LaunchRow],
    traces: Mapping[tuple[str, str], base.Trace],
) -> dict[str, Any]:
    """Measure the ceiling from knowing E4's choices; never a deployable model."""
    unique = [row for row in rows if row.horizon_ms == EXECUTION_HORIZON_MS]
    results = {}
    for label in ("selected", "landed"):
        chosen = [row for row in unique if getattr(row, label)]
        scores = np.ones(len(chosen), dtype=float)
        candidates = []
        for horizon in DECISION_HORIZONS_MS:
            for policy in POLICIES:
                economics = simulate(
                    chosen,
                    traces,
                    scores,
                    0.5,
                    policy,
                    latency_ms=0,
                    decision_horizon_ms=horizon,
                )
                candidates.append(
                    {
                        "label": label,
                        "decision_horizon_ms": horizon,
                        "policy": asdict(policy) | {"key": policy.key},
                        "economics": economics,
                    }
                )
        candidates.sort(
            key=lambda item: (
                item["economics"]["net_pnl_sol"],
                item["economics"]["profit_factor"],
                item["economics"]["win_rate"],
            ),
            reverse=True,
        )
        results[label] = candidates[0]
    return {
        "purpose": "non-deployable exact-wallet-choice ceiling",
        "uses_future_e4_selection_label": True,
        "best_by_label": results,
    }


def develop(root: Path) -> dict[str, Any]:
    rows, traces, audit = extract_dataset(root, include_holdout=False)
    horizon_results = []
    frozen_options = []
    for horizon in DECISION_HORIZONS_MS:
        train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
        validation = [row for row in rows if row.split == "validation" and row.horizon_ms == horizon]
        intent_models = fit_models(train)
        landed_train = [row for row in train if row.selected]
        landed_validation = [row for row in validation if row.selected]
        landing_models = fit_models(landed_train, target="landed")
        for family, (model, scaler) in intent_models.items():
            scores = score_model(model, scaler, validation)
            truth = np.asarray([row.selected for row in validation], dtype=bool)
            horizon_results.append(
                {
                    "horizon_ms": horizon,
                    "model_family": family,
                    "validation_average_precision": float(average_precision_score(truth, scores)),
                    "validation_roc_auc": float(roc_auc_score(truth, scores)),
                    "conditional_landing_models": {
                        landing_family: {
                            "average_precision": float(
                                average_precision_score(
                                    [row.landed for row in landed_validation],
                                    score_model(landing_model, landing_scaler, landed_validation),
                                )
                            ),
                            "roc_auc": float(
                                roc_auc_score(
                                    [row.landed for row in landed_validation],
                                    score_model(landing_model, landing_scaler, landed_validation),
                                )
                            ),
                        }
                        for landing_family, (landing_model, landing_scaler) in landing_models.items()
                    },
                }
            )
            for landing_family, (landing_model, landing_scaler) in landing_models.items():
                landing_scores = score_model(landing_model, landing_scaler, validation)
                joint_scores = scores * landing_scores
                ordered = sorted(joint_scores, reverse=True)
                for prediction_target in (40, 80, 120, 200, 300, 500):
                    threshold = float(
                        ordered[min(prediction_target - 1, len(ordered) - 1)]
                    )
                    intent = intent_metrics(validation, joint_scores, threshold)
                    selected = [
                        row
                        for row, score in zip(validation, joint_scores)
                        if score >= threshold
                    ]
                    selected_scores = np.asarray(
                        [score for score in joint_scores if score >= threshold]
                    )
                    best_policy = None
                    for policy in POLICIES:
                        economics = simulate(
                            selected,
                            traces,
                            selected_scores,
                            -math.inf,
                            policy,
                            latency_ms=0,
                            decision_horizon_ms=horizon,
                        )
                        record = {
                            "horizon_ms": horizon,
                            "model_family": family,
                            "landing_model_family": landing_family,
                            "threshold": threshold,
                            "target_predictions": prediction_target,
                            "policy": asdict(policy) | {"key": policy.key},
                            "intent": intent,
                            "validation_economics": economics,
                        }
                        if best_policy is None or candidate_rank(
                            economics, intent
                        ) > candidate_rank(
                            best_policy["validation_economics"], best_policy["intent"]
                        ):
                            best_policy = record
                    if best_policy is not None:
                        frozen_options.append(best_policy)
    frozen_options.sort(
        key=lambda item: candidate_rank(item["validation_economics"], item["intent"]),
        reverse=True,
    )
    winner = frozen_options[0]
    validation_unique = [row for row in rows if row.split == "validation"]
    wallet_oracle = wallet_oracle_search(validation_unique, traces)
    # Keep the fitted population identical to the one used to calibrate the
    # frozen validation threshold.  A train+validation refit would change score
    # calibration and silently make the numeric threshold a different policy.
    train = [
        row
        for row in rows
        if row.horizon_ms == winner["horizon_ms"] and row.split == "train"
    ]
    final_models = fit_models(train)
    final_landing_models = fit_models(
        [row for row in train if row.selected], target="landed"
    )
    model, scaler = final_models[winner["model_family"]]
    landing_model, landing_scaler = final_landing_models[
        winner["landing_model_family"]
    ]
    model_path = root / ".tmp-e4-wallet-selection-model.pkl"
    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "model": model,
                "scaler": scaler,
                "landing_model": landing_model,
                "landing_scaler": landing_scaler,
            },
            handle,
            protocol=5,
        )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "source_fingerprint": base.sha256_path(Path(__file__).resolve()),
        "manifest_fingerprint": base.sha256_path(root / "artifacts/e4-v12-riskset-source-manifest.json"),
        "feature_fingerprint": base.stable_hash(FEATURE_NAMES),
        "model_family": winner["model_family"],
        "landing_model_family": winner["landing_model_family"],
        "threshold": winner["threshold"],
        "decision_horizon_ms": winner["horizon_ms"],
        "exit_policy": winner["policy"],
        "failed_fills_are_positive_labels": True,
        "failed_fills_are_separated_by_conditional_landing_model": True,
        "e4_transactions_excluded_from_features": True,
    }
    frozen = {
        "schema_version": SCHEMA_VERSION,
        "status": "EXPLORATORY_POST_HOLDOUT_REVISION",
        "experiment_id": f"e4-wallet-{base.stable_hash(identity)}",
        "identity": identity,
        "development_audit": audit,
        "horizon_reconstruction": sorted(
            horizon_results,
            key=lambda item: (item["horizon_ms"], -item["validation_average_precision"]),
        ),
        "winner": winner,
        "wallet_oracle_development": wallet_oracle,
        "holdout_rows_read_before_this_revision": 30_000,
        "scientific_warning": (
            "the final ten captures were opened by the first sealed 250ms model; "
            "this faster-horizon revision is exploratory and needs a new future epoch"
        ),
        "production_paths_changed": 0,
    }
    write_json(root / "artifacts/e4-v12-wallet-selection-development.json", frozen)
    return frozen


def certify(root: Path) -> dict[str, Any]:
    frozen_path = root / "artifacts/e4-v12-wallet-selection-development.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    with (root / ".tmp-e4-wallet-selection-model.pkl").open("rb") as handle:
        payload = pickle.load(handle)
    rows, traces, audit = extract_dataset(root, include_holdout=True, only_holdout=True)
    decision_horizon_ms = int(frozen["winner"]["horizon_ms"])
    rows = [row for row in rows if row.horizon_ms == decision_horizon_ms]
    scores = score_model(payload["model"], payload["scaler"], rows)
    landing_scores = score_model(
        payload["landing_model"], payload["landing_scaler"], rows
    )
    scores = scores * landing_scores
    threshold = finite(frozen["winner"]["threshold"])
    policy = ExitPolicy(**{key: frozen["winner"]["policy"][key] for key in ExitPolicy.__annotations__})
    intent = intent_metrics(rows, scores, threshold)
    latencies = {
        str(latency): simulate(
            rows,
            traces,
            scores,
            threshold,
            policy,
            latency_ms=latency,
            decision_horizon_ms=decision_horizon_ms,
        )
        for latency in (0, 5, 50, 100, 250, 500)
    }
    oracle_holdout = {}
    for label, oracle in frozen["wallet_oracle_development"]["best_by_label"].items():
        selected_rows = [row for row in rows if getattr(row, label)]
        selected_scores = np.ones(len(selected_rows), dtype=float)
        oracle_policy = ExitPolicy(
            **{key: oracle["policy"][key] for key in ExitPolicy.__annotations__}
        )
        oracle_holdout[label] = {
            "frozen_development_choice": oracle,
            "holdout": simulate(
                selected_rows,
                traces,
                selected_scores,
                0.5,
                oracle_policy,
                latency_ms=0,
                decision_horizon_ms=int(oracle["decision_horizon_ms"]),
            ),
            "causal_horizon_frontier": {
                str(horizon): simulate(
                    selected_rows,
                    traces,
                    selected_scores,
                    0.5,
                    oracle_policy,
                    latency_ms=0,
                    decision_horizon_ms=horizon,
                )
                for horizon in DECISION_HORIZONS_MS
            },
        }
    zero = latencies["0"]
    requirements = {
        "at_least_150_trades": zero["trades"] >= 150,
        "all_ten_holdout_windows": zero["capture_windows"] == 10,
        "win_rate_at_least_65_percent": zero["win_rate"] >= 0.65,
        "wilson_lower_bound_at_least_55_percent": zero["wilson_lower_bound"] >= 0.55,
        "net_pnl_at_least_starting_bankroll": zero["net_pnl_sol"] >= STARTING_BANKROLL_SOL,
        "profit_factor_at_least_1_50": zero["profit_factor"] >= 1.50,
        "maximum_drawdown_at_most_15_percent": zero["maximum_drawdown_fraction"] <= 0.15,
        "intent_precision_at_least_50_percent": intent["precision"] >= 0.50,
        "intent_recall_at_least_50_percent": intent["recall"] >= 0.50,
        "sub_5ms_and_500ms_positive": latencies["5"]["net_pnl_sol"] > 0
        and latencies["500"]["net_pnl_sol"] > 0,
    }
    historical_pass = all(requirements.values())
    result = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": frozen["experiment_id"],
        "holdout_audit": audit,
        "intent": intent,
        "latency_economics": latencies,
        "exact_wallet_choice_oracle": {
            "deployable": False,
            "uses_future_e4_selection_label": True,
            "results": oracle_holdout,
        },
        "execution_gate": {
            "decision_horizon_ms": decision_horizon_ms,
            "maximum_create_to_entry_price_multiple": 1.50,
            "minimum_output_retention": 0.65,
            "same_slot_sub_5ms_e4_fact_is_not_claimed_as_reproducible": True,
        },
        "golden_gate": {
            "requirements": requirements,
            "passed": historical_pass,
            "status": "HISTORICAL_CANDIDATE_ONLY" if historical_pass else "REJECTED",
            "untouched_live_required": True,
            "golden_thesis_approved": False,
            "evaluation_role": "contaminated_replay_not_certification",
        },
        "production_paths_changed": 0,
    }
    write_json(root / "artifacts/e4-v12-wallet-selection-holdout.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--certify-holdout", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    result = certify(root) if args.certify_holdout else develop(root)
    if args.certify_holdout:
        summary = {
            "experiment_id": result["experiment_id"],
            "intent": result["intent"],
            "zero_latency": {key: value for key, value in result["latency_economics"]["0"].items() if key != "ledger"},
            "golden_gate": result["golden_gate"],
        }
    else:
        summary = {
            "experiment_id": result["experiment_id"],
            "winner": result["winner"],
            "holdout_rows_read_before_this_revision": result[
                "holdout_rows_read_before_this_revision"
            ],
        }
    print(canonical_json(summary))


if __name__ == "__main__":
    main()
