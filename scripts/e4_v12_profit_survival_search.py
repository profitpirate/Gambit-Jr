#!/usr/bin/env python3
"""Search for a causal, net-profitable launch-survival thesis.

This research pipeline deliberately predicts independent trade expectancy instead
of copying E4's observed choices.  Development search reads only the immutable
train and validation capture windows.  The chronological holdout is inaccessible
unless ``--certify-holdout`` is supplied after a development candidate is frozen.

The module never imports or writes production V12 paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import e4_v12_canonical_choice_sets as choice_sets
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

SCHEMA_VERSION = "e4-v12-profit-survival-v1"
THESIS_FAMILY = "causal-profit-survival-hazard-v1"
HORIZONS_MS = (250, 1_000, 3_000)
LATENCIES_MS = (0, 1, 2, 5, 10)
STOPS = (0.70, 0.80, 0.85, 0.90)
TAKES = (1.10, 1.20, 1.35, 1.60, 2.00)
HOLDS_MS = (30_000, 60_000)
PRIORITY_FEES_SOL = (0.001, 0.005, 0.020)
STARTING_BANKROLL_SOL = 3.0
POSITION_FRACTION = 0.0185
MAX_CONCURRENT_POSITIONS = 2
PROTOCOL_AND_CREATOR_FEE_BPS = 175.0
TIP_SOL = 0.001
BASE_TRANSACTION_FEE_SOL = 0.000005
RANDOM_SEED = 12_091

FEATURE_NAMES = (
    "horizon_ms",
    "creator_seed_sol",
    "public_buy_sol",
    "public_sell_sol",
    "buy_count",
    "sell_count",
    "unique_buyers",
    "unique_sellers",
    "distinct_signatures",
    "same_slot_buyers",
    "same_transaction_buyers",
    "maximum_buy_sol",
    "median_buy_sol",
    "buyer_sol_concentration",
    "price_multiple_from_create",
    "prefix_max_price_multiple",
    "prefix_drawdown",
    "log_return_volatility",
    "buy_sol_last_250ms",
    "buy_sol_last_1000ms",
    "buyers_last_250ms",
    "buyers_last_1000ms",
    "buy_acceleration",
    "virtual_sol_reserve",
    "real_token_reserve",
    "estimated_price_impact_bps",
    "creator_prior_launch_count",
    "mayhem_mode",
    "cashback_enabled",
    "metadata_content_addressed",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class CaptureSpec:
    run_id: str
    split: str
    start_ns: int
    end_ns: int
    events_path: Path
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class Point:
    timestamp_ns: int
    kind: str
    trader: str
    signature: str
    slot: int
    sol_amount: float
    price_sol: float
    virtual_sol: float
    virtual_tokens: float
    real_tokens: float
    complete: bool


@dataclass(slots=True)
class Trace:
    run_id: str
    split: str
    mint: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    mayhem_mode: bool
    cashback_enabled: bool
    metadata_content_addressed: bool
    creator_prior_launch_count: int
    points: list[Point] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Outcome:
    gross_multiple: float
    exit_offset_ms: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class ResearchRow:
    run_id: str
    split: str
    mint: str
    decision_ns: int
    horizon_ms: int
    features: tuple[float, ...]
    outcomes: tuple[Outcome, ...]
    latency_outcomes: tuple[Outcome, ...] = ()


@dataclass(frozen=True, slots=True)
class Policy:
    stop: float
    take: float
    hold_ms: int

    @property
    def key(self) -> str:
        return f"stop={self.stop:.2f}|take={self.take:.2f}|hold={self.hold_ms}"


POLICIES = tuple(Policy(stop, take, hold) for stop in STOPS for take in TAKES for hold in HOLDS_MS)


def content_addressed(uri: str) -> bool:
    lowered = uri.lower()
    return "ipfs" in lowered or "arweave" in lowered


def capture_specs(repo_root: Path, *, include_holdout: bool) -> list[CaptureSpec]:
    manifest_path = repo_root / "artifacts/e4-v12-riskset-source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capture_root = repo_root / ".tmp-choice-set-source/runs"
    output = []
    for row in manifest["captures"]:
        split = str(row["split"])
        if split == "holdout" and not include_holdout:
            continue
        files = {item["filename"]: item for item in row["files"]}
        event = files["e4-v12-forward-batch-live-events.jsonl"]
        run_id = str(row["run_id"])
        output.append(
            CaptureSpec(
                run_id=run_id,
                split=split,
                start_ns=integer(row["capture_start_ns"]),
                end_ns=integer(row["capture_end_ns"]),
                events_path=capture_root
                / run_id
                / "artifacts/e4-v12-forward-batch-live-events.jsonl",
                expected_sha256=str(event["sha256"]),
            )
        )
    return output


def point_from_event(row: Mapping[str, Any]) -> Point:
    virtual_sol, virtual_tokens, real_tokens = choice_sets.reserve_values(row)
    return Point(
        timestamp_ns=integer(row.get("received_ns")),
        kind=str(row.get("kind") or "").upper(),
        trader=str(row.get("trader") or ""),
        signature=str(row.get("signature") or ""),
        slot=integer(row.get("slot"), -1),
        sol_amount=finite(row.get("sol_amount")),
        price_sol=finite(row.get("price_sol")),
        virtual_sol=finite(virtual_sol),
        virtual_tokens=finite(virtual_tokens),
        real_tokens=finite(real_tokens),
        complete=bool(row.get("complete"))
        or str(row.get("kind") or "").upper() == "MIGRATION",
    )


def load_capture(spec: CaptureSpec, creator_counts: Counter[str]) -> tuple[list[Trace], int]:
    traces: dict[str, Trace] = {}
    parse_errors = 0
    with spec.events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            mint = str(row.get("mint") or "")
            if not mint:
                continue
            kind = str(row.get("kind") or "").upper()
            if kind == "CREATE" and mint not in traces:
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                creator = str(
                    row.get("creator") or raw.get("creator") or row.get("trader") or ""
                )
                traces[mint] = Trace(
                    run_id=spec.run_id,
                    split=spec.split,
                    mint=mint,
                    creator=creator,
                    create_ns=integer(row.get("received_ns")),
                    create_slot=integer(row.get("slot"), -1),
                    create_signature=str(row.get("signature") or ""),
                    mayhem_mode=bool(raw.get("is_mayhem_mode")),
                    cashback_enabled=bool(raw.get("is_cashback_enabled")),
                    metadata_content_addressed=content_addressed(str(raw.get("uri") or "")),
                    creator_prior_launch_count=creator_counts[creator],
                )
                creator_counts[creator] += 1
            trace = traces.get(mint)
            if trace is not None:
                trace.points.append(point_from_event(row))
    return list(traces.values()), parse_errors


def latest_point(points: Sequence[Point], timestamp_ns: int) -> Point | None:
    latest = None
    for point in points:
        if point.timestamp_ns > timestamp_ns:
            break
        if point.price_sol > 0 and point.virtual_sol > 0 and point.virtual_tokens > 0:
            latest = point
    return latest


def snapshot(trace: Trace, horizon_ms: int) -> tuple[float, ...] | None:
    decision_ns = trace.create_ns + horizon_ms * 1_000_000
    observed = [point for point in trace.points if point.timestamp_ns <= decision_ns]
    valid = [point for point in observed if point.price_sol > 0]
    reserve = latest_point(observed, decision_ns)
    if not valid or reserve is None or reserve.complete:
        return None
    buys = [point for point in observed if point.kind in choice_sets.BUY_KINDS]
    sells = [point for point in observed if point.kind in choice_sets.SELL_KINDS]
    creator_buys = [point for point in buys if point.trader == trace.creator]
    public_buys = [
        point
        for point in buys
        if point.trader and point.trader not in {trace.creator, choice_sets.E4_WALLET}
    ]
    prices = [point.price_sol for point in valid]
    log_returns = [
        math.log(right / left)
        for left, right in pairwise(prices)
        if left > 0 and right > 0
    ]
    buy_amounts = [point.sol_amount for point in public_buys if point.sol_amount > 0]
    total_buy = sum(buy_amounts)
    by_buyer = Counter()
    for point in public_buys:
        by_buyer[point.trader] += point.sol_amount

    def recent(window_ms: int) -> tuple[float, int]:
        lower = decision_ns - window_ms * 1_000_000
        rows = [point for point in public_buys if point.timestamp_ns >= lower]
        return sum(point.sol_amount for point in rows), len({point.trader for point in rows})

    buy_250, buyers_250 = recent(250)
    buy_1000, buyers_1000 = recent(1_000)
    prior_250 = sum(
        point.sol_amount
        for point in public_buys
        if decision_ns - 500_000_000 <= point.timestamp_ns < decision_ns - 250_000_000
    )
    create_price = prices[0]
    current_price = prices[-1]
    maximum = max(prices)
    return (
        float(horizon_ms),
        sum(point.sol_amount for point in creator_buys),
        total_buy,
        sum(point.sol_amount for point in sells),
        float(len(buys)),
        float(len(sells)),
        float(len({point.trader for point in public_buys})),
        float(len({point.trader for point in sells if point.trader})),
        float(len({point.signature for point in observed if point.signature})),
        float(
            len(
                {
                    point.trader
                    for point in public_buys
                    if point.slot == trace.create_slot and point.trader
                }
            )
        ),
        float(
            len(
                {
                    point.trader
                    for point in public_buys
                    if point.signature == trace.create_signature and point.trader
                }
            )
        ),
        max(buy_amounts, default=0.0),
        statistics.median(buy_amounts) if buy_amounts else 0.0,
        max(by_buyer.values(), default=0.0) / max(total_buy, 1e-12),
        current_price / create_price,
        maximum / create_price,
        1 - current_price / maximum,
        statistics.pstdev(log_returns) if len(log_returns) > 1 else 0.0,
        buy_250,
        buy_1000,
        float(buyers_250),
        float(buyers_1000),
        buy_250 - prior_250,
        reserve.virtual_sol,
        reserve.real_tokens,
        0.1 / (reserve.virtual_sol + 0.1) * 10_000,
        float(trace.creator_prior_launch_count),
        float(trace.mayhem_mode),
        float(trace.cashback_enabled),
        float(trace.metadata_content_addressed),
    )


def trade_outcome(
    trace: Trace, horizon_ms: int, policy: Policy, *, latency_ms: int = 0
) -> Outcome | None:
    decision_ns = trace.create_ns + horizon_ms * 1_000_000
    entry_ns = decision_ns + latency_ms * 1_000_000
    entry = latest_point(trace.points, entry_ns)
    if entry is None or entry.complete:
        return None
    entry_price = entry.price_sol or entry.virtual_sol / max(entry.virtual_tokens, 1e-12)
    exit_point = entry
    exit_reason = "MAXIMUM_HOLD"
    deadline = entry_ns + policy.hold_ms * 1_000_000
    for point in trace.points:
        if point.timestamp_ns <= entry_ns:
            continue
        if point.timestamp_ns > deadline:
            break
        if point.price_sol <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
            continue
        exit_point = point
        multiple = point.price_sol / max(entry_price, 1e-18)
        if point.complete:
            exit_reason = "LIQUIDITY_EMERGENCY"
            break
        if multiple <= policy.stop:
            exit_reason = "INITIAL_STOP"
            break
        if multiple >= policy.take:
            exit_reason = "TAKE_PROFIT"
            break
    position_sol = STARTING_BANKROLL_SOL * POSITION_FRACTION
    tokens = choice_sets.buy_tokens(position_sol, entry.virtual_sol, entry.virtual_tokens)
    if entry.real_tokens > 0:
        tokens = min(tokens, entry.real_tokens)
    gross = tokens * exit_point.virtual_sol / max(exit_point.virtual_tokens + tokens, 1e-12)
    return Outcome(
        gross_multiple=gross / position_sol,
        exit_offset_ms=max(0.0, (exit_point.timestamp_ns - decision_ns) / 1_000_000),
        exit_reason=exit_reason,
    )


def extract_rows(
    repo_root: Path,
    *,
    include_holdout: bool,
    verify_hashes: bool,
    only_split: str | None = None,
    horizons: Sequence[int] = HORIZONS_MS,
    certification_policy_index: int | None = None,
) -> tuple[list[ResearchRow], dict[str, Any]]:
    rows = []
    creator_counts: Counter[str] = Counter()
    audit_runs = []
    for spec in capture_specs(repo_root, include_holdout=include_holdout):
        if only_split is not None and spec.split != only_split:
            continue
        print(f"extract {spec.split} capture {spec.run_id}", flush=True)
        actual_sha = sha256_path(spec.events_path) if verify_hashes else spec.expected_sha256
        if actual_sha != spec.expected_sha256:
            raise ValueError(f"capture hash changed: {spec.run_id}")
        traces, parse_errors = load_capture(spec, creator_counts)
        included = Counter()
        for trace in traces:
            for horizon_ms in horizons:
                decision_ns = trace.create_ns + horizon_ms * 1_000_000
                if decision_ns + max(HOLDS_MS) * 1_000_000 > spec.end_ns:
                    included["insufficient_tail"] += 1
                    continue
                features = snapshot(trace, horizon_ms)
                if features is None:
                    included["invalid_snapshot"] += 1
                    continue
                outcomes = tuple(trade_outcome(trace, horizon_ms, policy) for policy in POLICIES)
                if any(outcome is None for outcome in outcomes):
                    included["invalid_outcome"] += 1
                    continue
                latency_outcomes = (
                    tuple(
                        trade_outcome(
                            trace,
                            horizon_ms,
                            POLICIES[certification_policy_index],
                            latency_ms=latency,
                        )
                        for latency in LATENCIES_MS
                    )
                    if certification_policy_index is not None
                    else ()
                )
                if any(outcome is None for outcome in latency_outcomes):
                    included["invalid_latency_outcome"] += 1
                    continue
                rows.append(
                    ResearchRow(
                        run_id=trace.run_id,
                        split=trace.split,
                        mint=trace.mint,
                        decision_ns=decision_ns,
                        horizon_ms=horizon_ms,
                        features=features,
                        outcomes=outcomes,  # type: ignore[arg-type]
                        latency_outcomes=latency_outcomes,  # type: ignore[arg-type]
                    )
                )
                included["rows"] += 1
        audit_runs.append(
            {
                "run_id": spec.run_id,
                "split": spec.split,
                "launches": len(traces),
                "parse_errors": parse_errors,
                "sha256": actual_sha,
                "expected_sha256": spec.expected_sha256,
                "hash_match": actual_sha == spec.expected_sha256,
                **dict(included),
            }
        )
    audit = {
        "version": SCHEMA_VERSION,
        "include_holdout": include_holdout,
        "only_split": only_split,
        "runs": audit_runs,
        "rows": len(rows),
        "rows_by_split": dict(Counter(row.split for row in rows)),
        "rows_by_horizon": dict(Counter(str(row.horizon_ms) for row in rows)),
        "certification_policy_index": certification_policy_index,
        "feature_names": list(FEATURE_NAMES),
        "policies": [asdict(policy) | {"key": policy.key} for policy in POLICIES],
        "future_values_in_features": False,
        "production_paths_changed": 0,
    }
    return rows, audit


def net_pnl(outcome: Outcome, priority_fee_sol: float, position_sol: float) -> float:
    variable_fee = position_sol * PROTOCOL_AND_CREATOR_FEE_BPS / 10_000
    fixed_fee = priority_fee_sol + TIP_SOL + BASE_TRANSACTION_FEE_SOL
    entry_cost = position_sol + variable_fee + fixed_fee
    proceeds = outcome.gross_multiple * position_sol * (
        1 - PROTOCOL_AND_CREATOR_FEE_BPS / 10_000
    )
    return proceeds - entry_cost


def wilson_lower_bound(wins: int, trades: int, z: float = 1.96) -> float:
    if trades <= 0:
        return 0.0
    proportion = wins / trades
    denominator = 1 + z**2 / trades
    centre = proportion + z**2 / (2 * trades)
    spread = z * math.sqrt(
        (proportion * (1 - proportion) + z**2 / (4 * trades)) / trades
    )
    return (centre - spread) / denominator


def opportunity_census(rows: Sequence[ResearchRow]) -> list[dict[str, Any]]:
    output = []
    nominal_position = STARTING_BANKROLL_SOL * POSITION_FRACTION
    for horizon_ms in HORIZONS_MS:
        for policy_index, policy in enumerate(POLICIES):
            for priority_fee in PRIORITY_FEES_SOL:
                record = {
                    "horizon_ms": horizon_ms,
                    "policy_index": policy_index,
                    "policy": policy.key,
                    "priority_fee_sol": priority_fee,
                }
                for split in ("train", "validation"):
                    pnls = [
                        net_pnl(row.outcomes[policy_index], priority_fee, nominal_position)
                        for row in rows
                        if row.split == split and row.horizon_ms == horizon_ms
                    ]
                    profits = [value for value in pnls if value > 0]
                    losses = [value for value in pnls if value <= 0]
                    record[split] = {
                        "launches": len(pnls),
                        "profitable": len(profits),
                        "profitable_percent": len(profits) / max(len(pnls), 1) * 100,
                        "oracle_pnl_sol": sum(pnls),
                        "oracle_profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
                    }
                output.append(record)
    return output


def feature_matrix(rows: Sequence[ResearchRow]) -> np.ndarray:
    matrix = np.asarray([row.features for row in rows], dtype=float)
    if not np.isfinite(matrix).all():
        raise ValueError("feature matrix contains non-finite values")
    return matrix


def simulate_predictions(
    rows: Sequence[ResearchRow],
    scores: np.ndarray,
    threshold: float,
    *,
    policy_index: int,
    priority_fee_sol: float,
    latency_index: int | None = None,
) -> dict[str, Any]:
    candidates = sorted(
        (
            (row.decision_ns, row.run_id, row.mint, row, float(score))
            for row, score in zip(rows, scores)
            if score >= threshold
        ),
        key=lambda item: (integer(item[1]), item[0], item[2]),
    )
    bankroll = STARTING_BANKROLL_SOL
    active: list[tuple[int, str]] = []
    closed = []
    rejected_concurrency = 0
    for _, run_id, mint, row, score in candidates:
        active = [item for item in active if item[0] > row.decision_ns]
        if len(active) >= MAX_CONCURRENT_POSITIONS:
            rejected_concurrency += 1
            continue
        position = bankroll * POSITION_FRACTION
        outcome = (
            row.outcomes[policy_index]
            if latency_index is None
            else row.latency_outcomes[latency_index]
        )
        pnl = net_pnl(outcome, priority_fee_sol, position)
        bankroll += pnl
        exit_ns = row.decision_ns + int(outcome.exit_offset_ms * 1_000_000)
        active.append((exit_ns, mint))
        closed.append(
            {
                "run_id": run_id,
                "mint": mint,
                "decision_ns": row.decision_ns,
                "score": score,
                "pnl_sol": pnl,
                "win": pnl > 0,
                "exit_reason": outcome.exit_reason,
            }
        )
    profits = [row["pnl_sol"] for row in closed if row["pnl_sol"] > 0]
    losses = [row["pnl_sol"] for row in closed if row["pnl_sol"] <= 0]
    equity = STARTING_BANKROLL_SOL
    peak = equity
    drawdown = 0.0
    for row in closed:
        equity += row["pnl_sol"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "threshold": threshold,
        "trades": len(closed),
        "wins": len(profits),
        "win_rate": len(profits) / max(len(closed), 1),
        "wilson_95_lower_bound": wilson_lower_bound(len(profits), len(closed)),
        "net_pnl_sol": bankroll - STARTING_BANKROLL_SOL,
        "profit_factor": sum(profits) / max(abs(sum(losses)), 1e-12),
        "maximum_drawdown_fraction": drawdown / STARTING_BANKROLL_SOL,
        "capture_windows": len({row["run_id"] for row in closed}),
        "largest_winner_contribution": max(profits, default=0.0) / max(sum(profits), 1e-12),
        "rejected_concurrency": rejected_concurrency,
        "ledger_hash": stable_hash(closed),
        "by_capture_window": {
            run_id: {
                "trades": sum(row["run_id"] == run_id for row in closed),
                "wins": sum(row["run_id"] == run_id and row["win"] for row in closed),
                "pnl_sol": sum(
                    row["pnl_sol"] for row in closed if row["run_id"] == run_id
                ),
            }
            for run_id in sorted({row["run_id"] for row in closed}, key=int)
        },
        "exit_reasons": dict(Counter(row["exit_reason"] for row in closed)),
        "ledger": closed,
    }


def candidate_rank(metrics: Mapping[str, Any]) -> tuple[Any, ...]:
    requirements = (
        metrics["trades"] >= 8,
        metrics["capture_windows"] >= 2,
        metrics["win_rate"] >= 0.65,
        metrics["net_pnl_sol"] > 0,
        metrics["profit_factor"] >= 1.25,
        metrics["maximum_drawdown_fraction"] <= 0.15,
        metrics["largest_winner_contribution"] <= 0.50,
    )
    return (sum(requirements), *requirements, metrics["net_pnl_sol"], metrics["profit_factor"])


def fit_model(
    train_rows: Sequence[ResearchRow],
    policy_index: int,
    priority_fee_sol: float,
    model_family: str,
) -> Any:
    position = STARTING_BANKROLL_SOL * POSITION_FRACTION
    labels = np.asarray(
        [net_pnl(row.outcomes[policy_index], priority_fee_sol, position) > 0 for row in train_rows],
        dtype=int,
    )
    positives = max(int(labels.sum()), 1)
    weights = np.where(labels == 1, (len(labels) - positives) / positives, 1.0)
    matrix = feature_matrix(train_rows)
    if model_family == "logistic":
        means = matrix.mean(axis=0)
        scales = matrix.std(axis=0)
        scales[scales < 1e-9] = 1.0
        model = LogisticRegression(C=0.1, max_iter=1_000, solver="liblinear")
        model.fit((matrix - means) / scales, labels, sample_weight=weights)
        model.feature_means_ = means
        model.feature_scales_ = scales
        return model
    model = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=120,
        max_leaf_nodes=15,
        min_samples_leaf=30,
        l2_regularization=5.0,
        random_state=RANDOM_SEED,
    )
    model.fit(matrix, labels, sample_weight=weights)
    return model


def score_model(model: Any, rows: Sequence[ResearchRow]) -> np.ndarray:
    matrix = feature_matrix(rows)
    if hasattr(model, "feature_means_"):
        matrix = (matrix - model.feature_means_) / model.feature_scales_
    return model.predict_proba(matrix)[:, 1]


def development_search(
    rows: Sequence[ResearchRow], census: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    eligible = [
        row
        for row in census
        if row["train"]["profitable"] >= 80 and row["validation"]["profitable"] >= 30
    ]
    eligible.sort(
        key=lambda row: (
            row["validation"]["profitable_percent"],
            row["validation"]["oracle_profit_factor"],
        ),
        reverse=True,
    )
    # The opportunity census narrows only compute; it never uses holdout evidence.
    shortlisted = eligible[:24]
    results = []
    for index, config in enumerate(shortlisted, 1):
        print(f"fit development candidate {index}/{len(shortlisted)}", flush=True)
        horizon = integer(config["horizon_ms"])
        policy_index = integer(config["policy_index"])
        priority_fee = finite(config["priority_fee_sol"])
        train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
        validation = [
            row for row in rows if row.split == "validation" and row.horizon_ms == horizon
        ]
        for family in ("hist_gradient_boosting", "logistic"):
            model = fit_model(train, policy_index, priority_fee, family)
            scores = score_model(model, validation)
            ordered = sorted(scores, reverse=True)
            thresholds = {
                float(ordered[min(target - 1, len(ordered) - 1)])
                for target in (8, 12, 20, 30, 50, 80, 120, 200)
            }
            best = None
            for threshold in sorted(thresholds, reverse=True):
                metrics = simulate_predictions(
                    validation,
                    scores,
                    threshold,
                    policy_index=policy_index,
                    priority_fee_sol=priority_fee,
                )
                if best is None or candidate_rank(metrics) > candidate_rank(best):
                    best = metrics
            results.append(
                {
                    "horizon_ms": horizon,
                    "policy_index": policy_index,
                    "policy": POLICIES[policy_index].key,
                    "priority_fee_sol": priority_fee,
                    "model_family": family,
                    "validation": best,
                    "train_positive_labels": sum(
                        net_pnl(
                            row.outcomes[policy_index],
                            priority_fee,
                            STARTING_BANKROLL_SOL * POSITION_FRACTION,
                        )
                        > 0
                        for row in train
                    ),
                    "validation_positive_labels": sum(
                        net_pnl(
                            row.outcomes[policy_index],
                            priority_fee,
                            STARTING_BANKROLL_SOL * POSITION_FRACTION,
                        )
                        > 0
                        for row in validation
                    ),
                }
            )
    results.sort(key=lambda row: candidate_rank(row["validation"]), reverse=True)
    winner = results[0] if results else None
    return {
        "version": SCHEMA_VERSION,
        "thesis_family": THESIS_FAMILY,
        "development_only": True,
        "holdout_rows_read": 0,
        "candidate_configurations_in_census": len(census),
        "candidate_configurations_modelled": len(shortlisted) * 2,
        "selection_rule": (
            "validation gate count, then PnL and profit factor; all models and labels fitted "
            "on chronological training windows only"
        ),
        "winner": winner,
        "top_candidates": results[:20],
        "frozen_candidate_ready": bool(
            winner and candidate_rank(winner["validation"])[0] == 7
        ),
        "production_paths_changed": 0,
    }


def frozen_candidate(
    search: Mapping[str, Any], audit: Mapping[str, Any], script_path: Path, root: Path
) -> dict[str, Any]:
    winner = search.get("winner")
    if not winner or not search.get("frozen_candidate_ready"):
        raise ValueError("development search did not produce a gate-complete candidate")
    identity = {
        "thesis_family_identifier": THESIS_FAMILY,
        "source_code_fingerprint": sha256_path(script_path),
        "dataset_source_manifest_fingerprint": sha256_path(
            root / "artifacts/e4-v12-riskset-source-manifest.json"
        ),
        "evidence_epoch": [
            {"run_id": row["run_id"], "sha256": row["sha256"]} for row in audit["runs"]
        ],
        "feature_set_fingerprint": stable_hash(FEATURE_NAMES),
        "model_family": winner["model_family"],
        "full_parameters": {
            "random_seed": RANDOM_SEED,
            "learning_rate": 0.05,
            "max_iter": 120,
            "max_leaf_nodes": 15,
            "min_samples_leaf": 30,
            "l2_regularization": 5.0,
            "threshold": winner["validation"]["threshold"],
        },
        "causal_horizon_ms": winner["horizon_ms"],
        "candidate_risk_set_policy": "one independent launch at each causal confirmation horizon",
        "chronological_split": {
            "train": [row["run_id"] for row in audit["runs"] if row["split"] == "train"],
            "validation": [
                row["run_id"] for row in audit["runs"] if row["split"] == "validation"
            ],
            "holdout": "sealed until explicit certification",
        },
        "bankroll": STARTING_BANKROLL_SOL,
        "position_sizing": {
            "fraction": POSITION_FRACTION,
            "maximum_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        },
        "fee_model": {
            "priority_fee_sol": winner["priority_fee_sol"],
            "tip_sol": TIP_SOL,
            "base_transaction_fee_sol": BASE_TRANSACTION_FEE_SOL,
            "protocol_and_creator_fee_bps": PROTOCOL_AND_CREATOR_FEE_BPS,
        },
        "output_guard": "reserve-valid exact constant-product quote",
        "latency_assumptions_ms": list(LATENCIES_MS),
        "execution_policy": "paper replay; one entry per launch; no re-entry",
        "exit_policy": {
            **asdict(POLICIES[integer(winner["policy_index"])]),
            "uses_e4_future_sells": False,
        },
    }
    return {
        "version": SCHEMA_VERSION,
        "status": "FROZEN_FOR_ONE_TIME_HISTORICAL_HOLDOUT",
        "identity": identity,
        "experiment_id": f"e4x-{stable_hash(identity)}",
        "candidate": winner,
        "development_ledger_hash": winner["validation"]["ledger_hash"],
        "holdout_rows_read_before_freeze": 0,
        "live_evidence_authorised": False,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def scalar_baseline(
    validation: Sequence[ResearchRow],
    holdout: Sequence[ResearchRow],
    *,
    target_trades: int,
    policy_index: int,
    priority_fee_sol: float,
) -> dict[str, Any]:
    candidates = []
    for feature_index, name in enumerate(FEATURE_NAMES):
        for direction in (1.0, -1.0):
            validation_scores = np.asarray(
                [direction * row.features[feature_index] for row in validation], dtype=float
            )
            ordered = sorted(validation_scores, reverse=True)
            threshold = float(ordered[min(target_trades - 1, len(ordered) - 1)])
            metrics = simulate_predictions(
                validation,
                validation_scores,
                threshold,
                policy_index=policy_index,
                priority_fee_sol=priority_fee_sol,
            )
            candidates.append(
                {
                    "feature": name,
                    "direction": "high" if direction > 0 else "low",
                    "threshold": threshold,
                    "validation": metrics,
                    "direction_value": direction,
                    "feature_index": feature_index,
                }
            )
    candidates.sort(key=lambda row: candidate_rank(row["validation"]), reverse=True)
    winner = candidates[0]
    holdout_scores = np.asarray(
        [
            winner["direction_value"] * row.features[winner["feature_index"]]
            for row in holdout
        ],
        dtype=float,
    )
    holdout_metrics = simulate_predictions(
        holdout,
        holdout_scores,
        finite(winner["threshold"]),
        policy_index=policy_index,
        priority_fee_sol=priority_fee_sol,
        latency_index=0,
    )
    return {
        "feature": winner["feature"],
        "direction": winner["direction"],
        "threshold": winner["threshold"],
        "validation": winner["validation"],
        "holdout": holdout_metrics,
        "selection_used_holdout": False,
    }


def historical_gate(
    economics: Mapping[str, Mapping[str, Any]], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    zero = economics["0"]
    requirements = {
        "at_least_20_closed_trades": zero["trades"] >= 20,
        "at_least_two_capture_windows": zero["capture_windows"] >= 2,
        "win_rate_at_least_65_percent": zero["win_rate"] >= 0.65,
        "wilson_lower_bound_at_least_45_percent": zero["wilson_95_lower_bound"] >= 0.45,
        "net_pnl_positive": zero["net_pnl_sol"] > 0,
        "profit_factor_at_least_1_25": zero["profit_factor"] >= 1.25,
        "maximum_drawdown_at_most_15_percent": zero["maximum_drawdown_fraction"] <= 0.15,
        "largest_winner_at_most_50_percent": zero["largest_winner_contribution"] <= 0.50,
        "every_latency_pnl_positive": all(
            row["net_pnl_sol"] > 0 for row in economics.values()
        ),
        "every_latency_profit_factor_at_least_1_25": all(
            row["profit_factor"] >= 1.25 for row in economics.values()
        ),
        "every_latency_win_rate_at_least_65_percent": all(
            row["win_rate"] >= 0.65 for row in economics.values()
        ),
        "beats_frozen_scalar_baseline_pnl": zero["net_pnl_sol"]
        > baseline["holdout"]["net_pnl_sol"],
    }
    passed = all(requirements.values())
    if passed:
        failure = None
    elif zero["trades"] < 20:
        failure = "INSUFFICIENT_LABELS"
    elif zero["win_rate"] < 0.65:
        failure = "FALSE_POSITIVE_OVERLOAD"
    elif zero["net_pnl_sol"] <= 0:
        failure = "NEGATIVE_EXPECTANCY"
    elif zero["profit_factor"] < 1.25:
        failure = "INADEQUATE_PROFIT_FACTOR"
    else:
        failure = "HOLDOUT_COLLAPSE"
    return {
        "status": "HISTORICAL_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "historical_qualification_passed": passed,
        "requirements": requirements,
        "failed_requirements": [name for name, value in requirements.items() if not value],
        "failure_classification": failure,
        "live_confirmation_authorised": passed,
        "production_promotion_authorised": False,
        "production_paths_changed": 0,
    }


def certify_holdout(root: Path, cache: Path, frozen_path: Path) -> dict[str, Any]:
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    with cache.open("rb") as handle:
        development = pickle.load(handle)
    rows: list[ResearchRow] = development["rows"]
    candidate = frozen["candidate"]
    horizon = integer(candidate["horizon_ms"])
    policy_index = integer(candidate["policy_index"])
    priority_fee = finite(candidate["priority_fee_sol"])
    train = [row for row in rows if row.split == "train" and row.horizon_ms == horizon]
    validation = [
        row for row in rows if row.split == "validation" and row.horizon_ms == horizon
    ]
    model = fit_model(train, policy_index, priority_fee, str(candidate["model_family"]))
    validation_scores = score_model(model, validation)
    validation_repeat = simulate_predictions(
        validation,
        validation_scores,
        finite(candidate["validation"]["threshold"]),
        policy_index=policy_index,
        priority_fee_sol=priority_fee,
    )
    if validation_repeat["ledger_hash"] != frozen["development_ledger_hash"]:
        raise ValueError("frozen development candidate is not reproducible")
    holdout, holdout_audit = extract_rows(
        root,
        include_holdout=True,
        verify_hashes=True,
        only_split="holdout",
        horizons=(horizon,),
        certification_policy_index=policy_index,
    )
    holdout_scores = score_model(model, holdout)
    threshold = finite(candidate["validation"]["threshold"])
    economics = {
        str(latency): simulate_predictions(
            holdout,
            holdout_scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=priority_fee,
            latency_index=index,
        )
        for index, latency in enumerate(LATENCIES_MS)
    }
    baseline = scalar_baseline(
        validation,
        holdout,
        target_trades=validation_repeat["trades"],
        policy_index=policy_index,
        priority_fee_sol=priority_fee,
    )
    gate = historical_gate(economics, baseline)
    repeat = {
        str(latency): simulate_predictions(
            holdout,
            holdout_scores,
            threshold,
            policy_index=policy_index,
            priority_fee_sol=priority_fee,
            latency_index=index,
        )["ledger_hash"]
        for index, latency in enumerate(LATENCIES_MS)
    }
    if any(economics[key]["ledger_hash"] != repeat[key] for key in economics):
        raise ValueError("economic replay is not deterministic")
    return {
        "version": SCHEMA_VERSION,
        "experiment_id": frozen["experiment_id"],
        "candidate": candidate,
        "development_reproduction": validation_repeat,
        "holdout_data_audit": holdout_audit,
        "holdout_rows": len(holdout),
        "latencies": economics,
        "strongest_scalar_baseline": baseline,
        "deterministic_replay": True,
        "verdict": gate,
        "production_paths_changed": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--cache", type=Path, default=Path(".tmp-profit-survival-development.pkl")
    )
    parser.add_argument("--reuse-cache", action="store_true")
    parser.add_argument("--skip-source-hashes", action="store_true")
    parser.add_argument("--certify-holdout", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    cache = (root / args.cache).resolve()
    output = root / "artifacts"
    frozen_path = output / "e4-v12-profit-survival-frozen-candidate.json"
    if args.certify_holdout:
        if not frozen_path.exists():
            raise ValueError("development candidate must be frozen before holdout certification")
        certification = certify_holdout(root, cache, frozen_path)
        write_json(output / "e4-v12-profit-survival-historical-economics.json", certification)
        write_json(
            output / "e4-v12-profit-survival-historical-verdict.json",
            certification["verdict"]
            | {"experiment_id": certification["experiment_id"]},
        )
        print(
            canonical_json(
                {
                    "experiment_id": certification["experiment_id"],
                    "status": certification["verdict"]["status"],
                    "trades_0ms": certification["latencies"]["0"]["trades"],
                    "pnl_0ms": certification["latencies"]["0"]["net_pnl_sol"],
                }
            )
        )
        return
    if args.reuse_cache and cache.exists():
        with cache.open("rb") as handle:
            payload = pickle.load(handle)
        rows = payload["rows"]
        audit = payload["audit"]
    else:
        rows, audit = extract_rows(
            root,
            include_holdout=False,
            verify_hashes=not args.skip_source_hashes,
        )
        with cache.open("wb") as handle:
            pickle.dump({"rows": rows, "audit": audit}, handle, protocol=5)
    census = opportunity_census(rows)
    search = development_search(rows, census)
    write_json(output / "e4-v12-profit-survival-data-audit.json", audit)
    write_json(output / "e4-v12-profit-survival-opportunity-census.json", census)
    write_json(output / "e4-v12-profit-survival-development-search.json", search)
    if search["frozen_candidate_ready"]:
        write_json(
            frozen_path,
            frozen_candidate(search, audit, Path(__file__).resolve(), root),
        )
    print(canonical_json({"rows": len(rows), "winner": search["winner"]}))


if __name__ == "__main__":
    main()
