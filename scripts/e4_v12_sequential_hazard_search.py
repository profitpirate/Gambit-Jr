#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import e4_v12_conclusive_entry_rerun as base


RUNTIME_FEATURES = [
    "log_seed",
    "log_outside",
    "log_fdv",
    "age_100ms",
    "prior_creator_log",
    "prior_creator_success_log",
    "prior_creator_failure_log",
    "creator_success_rate",
    "known_buyer_count",
    "max_prior_buyer_log",
    "sum_prior_buyer_log",
    "max_prior_buyer_success_log",
    "sum_prior_buyer_success_log",
    "max_pair_log",
    "seed_share",
    "first_buyer_age_100ms",
    "second_buyer_age_100ms",
    "interbuyer_100ms",
    "distinct_buy_signatures",
    "max_buys_one_signature",
    "max_buys_one_slot",
    "create_signature_buys",
    "price_multiple_clip",
    "prior_signature_shape_log",
    "outside_per_buyer",
    "buyer_graph_density",
    "buyer_success_density",
    "identity_strength",
    "slot_cluster_strength",
    "launch_velocity",
    "seed_to_fdv",
    "outside_to_fdv",
    "no_public_buyers",
    "one_public_buyer",
    "two_plus_public_buyers",
    "very_early_50ms",
    "very_early_150ms",
    "very_early_400ms",
    "fdv_core_band",
    "seed_roundness",
    "active_count_log",
    "seed_rank_inverse",
    "identity_rank_inverse",
    "velocity_rank_inverse",
    "buyers_rank_inverse",
    "current_is_seed_top",
    "current_is_identity_top",
    "current_is_velocity_top",
    "seed_gap_to_best",
    "identity_gap_to_best",
    "velocity_gap_to_best",
    "competitor_max_seed_log",
    "competitor_max_identity",
    "competitor_max_velocity_log",
    "trigger_is_creator",
    "trigger_is_known_buyer",
    "trigger_buyer_attempts_log",
    "trigger_buyer_successes_log",
    "trigger_sol_log",
    "trigger_tx_index_log",
    "trigger_event_index_log",
    "trigger_same_create_slot",
    "trigger_same_create_signature",
]


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


def log1p(value: Any) -> float:
    return math.log1p(max(0.0, finite(value)))


def tx_index(row: Mapping[str, Any]) -> int:
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
    for key in ("transaction_index", "transactionIndex", "tx_index", "txIndex"):
        if row.get(key) is not None:
            return integer(row.get(key), -1)
        if raw.get(key) is not None:
            return integer(raw.get(key), -1)
    return -1


def event_key(row: Mapping[str, Any]) -> tuple[int, int, int, int]:
    index = tx_index(row)
    return (
        integer(row.get("slot"), -1),
        index if index >= 0 else 1_000_000,
        integer(row.get("event_index"), 0),
        integer(row.get("received_ns"), 0),
    )


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


def earliest_marker(launch: base.Launch, failed: Mapping[str, list[dict[str, Any]]]):
    if failed.get(launch.mint):
        launch.failed_attempt = failed[launch.mint][0]
    return base.marker_for(launch)


def state_before_marker(launch: base.Launch, marker: tuple[str, tuple[int, int, int, int], int]) -> tuple[base.State, dict[str, Any]]:
    label, key, timestamp = marker
    state = base.State(latest_ns=launch.create_ns)
    for row in launch.events:
        if event_key(row) >= key:
            break
        base.apply_event(launch, state, row)
    snapshot = base.snapshot_dict(launch, base.state_copy(state), timestamp, label, "PRE_INTENT")
    snapshot["positive"] = True
    return state, snapshot


@dataclass
class IntentMemory:
    creator_attempts: Counter[str]
    creator_successes: Counter[str]
    creator_failures: Counter[str]
    buyer_attempts: Counter[str]
    buyer_successes: Counter[str]
    pair_attempts: Counter[str]
    signature_shapes: Counter[str]

    @classmethod
    def empty(cls) -> "IntentMemory":
        return cls(Counter(), Counter(), Counter(), Counter(), Counter(), Counter(), Counter())

    def apply(self, detail: Mapping[str, Any]) -> None:
        creator = str(detail.get("creator") or "")
        label = str(detail.get("label") or "")
        if creator:
            self.creator_attempts[creator] += 1
            self.creator_successes[creator] += int(label == "SUCCESS")
            self.creator_failures[creator] += int(label == "FAILED_ATTEMPT")
        for buyer in detail.get("first_buyers") or []:
            buyer = str(buyer or "")
            if not buyer:
                continue
            self.buyer_attempts[buyer] += 1
            self.buyer_successes[buyer] += int(label == "SUCCESS")
            if creator:
                self.pair_attempts[f"{creator}|{buyer}"] += 1
        shape = f"{integer(detail.get('max_buys_one_signature'))}|{integer(detail.get('create_signature_buys'))}"
        self.signature_shapes[shape] += 1


def annotate_history(row: dict[str, Any], memory: IntentMemory) -> None:
    creator = str(row.get("creator") or "")
    buyers = [str(value) for value in row.get("first_buyers") or []]
    attempts = memory.creator_attempts[creator]
    successes = memory.creator_successes[creator]
    failures = memory.creator_failures[creator]
    buyer_attempts = [memory.buyer_attempts[value] for value in buyers]
    buyer_successes = [memory.buyer_successes[value] for value in buyers]
    pair_attempts = [memory.pair_attempts[f"{creator}|{value}"] for value in buyers]
    row.update({
        "hist_wins": successes,
        "hist_losses": failures,
        "hist_trades": attempts,
        "hist_rate": successes / attempts if attempts else 0.0,
        "prior_creator_attempts": attempts,
        "prior_creator_successes": successes,
        "prior_creator_failures": failures,
        "known_buyer_count": sum(value > 0 for value in buyer_attempts),
        "max_prior_buyer_attempts": max(buyer_attempts, default=0),
        "sum_prior_buyer_attempts": sum(buyer_attempts),
        "max_prior_buyer_successes": max(buyer_successes, default=0),
        "sum_prior_buyer_successes": sum(buyer_successes),
        "max_creator_buyer_pair_attempts": max(pair_attempts, default=0),
    })


def eligible(row: Mapping[str, Any]) -> bool:
    return bool(
        not row.get("mayhem")
        and integer(row.get("sell_count")) == 0
        and 2_750.0 <= finite(row.get("fdv_usd")) <= 10_000.0
        and finite(row.get("creator_seed_sol")) >= 0.20
        and finite(row.get("age_ms")) <= 1_500.0
    )


def row_features(row: Mapping[str, Any]) -> dict[str, float]:
    seed = finite(row.get("creator_seed_sol"))
    outside = finite(row.get("outside_sol"))
    fdv = max(1.0, finite(row.get("fdv_usd")))
    attempts = finite(row.get("prior_creator_attempts"))
    successes = finite(row.get("prior_creator_successes"))
    failures = finite(row.get("prior_creator_failures"))
    buyers = max(1.0, finite(row.get("unique_buyers")))
    first_age = finite(row.get("first_buyer_age_ms"), 9_999.0)
    second_age = finite(row.get("second_buyer_age_ms"), 9_999.0)
    interbuy = finite(row.get("median_interbuyer_ms"), 9_999.0)
    common_seed_distance = min(abs(seed - value) for value in (0.25, 0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 5.0, 6.0, 8.0))
    values = {
        "log_seed": log1p(seed),
        "log_outside": log1p(outside),
        "log_fdv": log1p(fdv),
        "age_100ms": min(20.0, finite(row.get("age_ms")) / 100.0),
        "prior_creator_log": log1p(attempts),
        "prior_creator_success_log": log1p(successes),
        "prior_creator_failure_log": log1p(failures),
        "creator_success_rate": successes / attempts if attempts else 0.0,
        "known_buyer_count": finite(row.get("known_buyer_count")),
        "max_prior_buyer_log": log1p(row.get("max_prior_buyer_attempts")),
        "sum_prior_buyer_log": log1p(row.get("sum_prior_buyer_attempts")),
        "max_prior_buyer_success_log": log1p(row.get("max_prior_buyer_successes")),
        "sum_prior_buyer_success_log": log1p(row.get("sum_prior_buyer_successes")),
        "max_pair_log": log1p(row.get("max_creator_buyer_pair_attempts")),
        "seed_share": seed / max(1e-9, seed + outside),
        "first_buyer_age_100ms": min(100.0, first_age / 100.0),
        "second_buyer_age_100ms": min(100.0, second_age / 100.0),
        "interbuyer_100ms": min(100.0, interbuy / 100.0),
        "distinct_buy_signatures": finite(row.get("distinct_buy_signatures")),
        "max_buys_one_signature": finite(row.get("max_buys_one_signature")),
        "max_buys_one_slot": finite(row.get("max_buys_one_slot")),
        "create_signature_buys": finite(row.get("create_signature_buys")),
        "price_multiple_clip": min(10.0, max(0.0, finite(row.get("price_multiple"), 1.0))),
        "prior_signature_shape_log": log1p(row.get("prior_signature_shape_attempts")),
        "outside_per_buyer": outside / buyers,
        "buyer_graph_density": finite(row.get("sum_prior_buyer_attempts")) / buyers,
        "buyer_success_density": finite(row.get("sum_prior_buyer_successes")) / buyers,
        "identity_strength": 1.5 * log1p(successes) + 1.25 * log1p(attempts) + log1p(row.get("sum_prior_buyer_attempts")),
        "slot_cluster_strength": finite(row.get("same_slot_unique")) + 0.5 * finite(row.get("same_slot_buys")) + 0.75 * finite(row.get("known_buyer_count")),
        "launch_velocity": (finite(row.get("buy_count")) + finite(row.get("unique_buyers"))) / max(0.25, finite(row.get("age_ms")) / 1_000.0),
        "seed_to_fdv": seed / fdv * 10_000.0,
        "outside_to_fdv": outside / fdv * 10_000.0,
        "no_public_buyers": float(integer(row.get("unique_buyers")) == 0),
        "one_public_buyer": float(integer(row.get("unique_buyers")) == 1),
        "two_plus_public_buyers": float(integer(row.get("unique_buyers")) >= 2),
        "very_early_50ms": float(finite(row.get("age_ms")) <= 50.0),
        "very_early_150ms": float(finite(row.get("age_ms")) <= 150.0),
        "very_early_400ms": float(finite(row.get("age_ms")) <= 400.0),
        "fdv_core_band": float(3_500.0 <= fdv <= 7_500.0),
        "seed_roundness": math.exp(-4.0 * common_seed_distance),
    }
    for name in RUNTIME_FEATURES:
        values.setdefault(name, finite(row.get(name)))
    return values


def make_snapshot(
    launch: base.Launch,
    state: base.State,
    row: Mapping[str, Any],
    memory: IntentMemory,
    marker: tuple[str, tuple[int, int, int, int], int] | None,
    horizon_ms: float,
) -> dict[str, Any]:
    now_ns = integer(row.get("received_ns"))
    label = marker[0] if marker else "IGNORED"
    snapshot = base.snapshot_dict(launch, base.state_copy(state), now_ns, label, "SEQUENTIAL_EVENT")
    annotate_history(snapshot, memory)
    first_ns = state.first_buyer_ns
    snapshot["first_buyer_age_ms"] = (first_ns[0] - launch.create_ns) / 1e6 if first_ns else 9_999.0
    snapshot["second_buyer_age_ms"] = (first_ns[1] - launch.create_ns) / 1e6 if len(first_ns) > 1 else 9_999.0
    snapshot["median_interbuyer_ms"] = statistics.median((b-a)/1e6 for a,b in zip(first_ns,first_ns[1:])) if len(first_ns) > 1 else 9_999.0
    snapshot["distinct_buy_signatures"] = len([key for key in state.buy_signatures if key])
    snapshot["max_buys_one_signature"] = max(state.buy_signatures.values(), default=0)
    snapshot["max_buys_one_slot"] = max(state.buy_slots.values(), default=0)
    snapshot["create_signature_buys"] = integer(state.buy_signatures.get(launch.create_signature, 0))
    snapshot["price_multiple"] = state.price_sol / state.initial_price_sol if state.price_sol > 0 and state.initial_price_sol > 0 else 1.0
    shape = f"{snapshot['max_buys_one_signature']}|{snapshot['create_signature_buys']}"
    snapshot["prior_signature_shape_attempts"] = memory.signature_shapes[shape]
    trader = str(row.get("trader") or "")
    snapshot["trigger_is_creator"] = float(bool(trader and trader == launch.creator))
    snapshot["trigger_is_known_buyer"] = float(memory.buyer_attempts[trader] > 0 if trader else False)
    snapshot["trigger_buyer_attempts_log"] = log1p(memory.buyer_attempts[trader] if trader else 0)
    snapshot["trigger_buyer_successes_log"] = log1p(memory.buyer_successes[trader] if trader else 0)
    snapshot["trigger_sol_log"] = log1p(row.get("sol_amount"))
    snapshot["trigger_tx_index_log"] = log1p(max(0, tx_index(row)))
    snapshot["trigger_event_index_log"] = log1p(row.get("event_index"))
    snapshot["trigger_same_create_slot"] = float(integer(row.get("slot")) == launch.create_slot)
    snapshot["trigger_same_create_signature"] = float(str(row.get("signature") or "") == launch.create_signature)
    snapshot["intent_ns"] = marker[2] if marker else None
    snapshot["intent_label"] = marker[0] if marker else None
    delta_ms = (marker[2] - now_ns) / 1e6 if marker else float("inf")
    snapshot["time_to_intent_ms"] = delta_ms if marker else None
    snapshot["positive"] = bool(marker and 0.0 < delta_ms <= horizon_ms)
    snapshot["mint_has_later_intent"] = bool(marker and delta_ms > horizon_ms)
    return snapshot


def add_relative_features(current: dict[str, Any], active: Sequence[dict[str, Any]]) -> None:
    current_values = row_features(current)
    rows = [(row, row_features(row)) for row in active]
    if not rows:
        rows = [(current, current_values)]
    def ranked(name: str) -> tuple[int, float, float]:
        values = sorted((features[name], str(row.get("mint") or "")) for row, features in rows)
        values.reverse()
        target = current_values[name]
        rank = 1 + sum(value > target for value, _ in values)
        best_other = max((value for value, mint in values if mint != current["mint"]), default=0.0)
        return rank, target - best_other, best_other
    seed_rank, seed_gap, max_seed = ranked("log_seed")
    identity_rank, identity_gap, max_identity = ranked("identity_strength")
    velocity_rank, velocity_gap, max_velocity = ranked("launch_velocity")
    buyers = sorted(((finite(row.get("unique_buyers")), str(row.get("mint") or "")) for row, _ in rows), reverse=True)
    buyer_value = finite(current.get("unique_buyers"))
    buyer_rank = 1 + sum(value > buyer_value for value, _ in buyers)
    count = max(1, len(rows))
    current.update({
        "active_count_log": log1p(count),
        "seed_rank_inverse": 1.0 / seed_rank,
        "identity_rank_inverse": 1.0 / identity_rank,
        "velocity_rank_inverse": 1.0 / velocity_rank,
        "buyers_rank_inverse": 1.0 / buyer_rank,
        "current_is_seed_top": float(seed_rank == 1),
        "current_is_identity_top": float(identity_rank == 1),
        "current_is_velocity_top": float(velocity_rank == 1),
        "seed_gap_to_best": seed_gap,
        "identity_gap_to_best": identity_gap,
        "velocity_gap_to_best": velocity_gap,
        "competitor_max_seed_log": max_seed,
        "competitor_max_identity": max_identity,
        "competitor_max_velocity_log": log1p(max_velocity),
    })


def build_dataset(
    launches: Mapping[str, base.Launch],
    failed: Mapping[str, list[dict[str, Any]]],
    run_ids: Sequence[str],
    horizon_ms: float,
) -> list[dict[str, Any]]:
    markers = {mint: earliest_marker(launch, failed) for mint, launch in launches.items()}
    marker_details: dict[str, dict[str, Any]] = {}
    for mint, marker in markers.items():
        if marker is not None:
            _, detail = state_before_marker(launches[mint], marker)
            marker_details[mint] = detail

    memory = IntentMemory.empty()
    dataset: list[dict[str, Any]] = []
    by_run: dict[int, list[base.Launch]] = defaultdict(list)
    for launch in launches.values():
        by_run[launch.run_index].append(launch)

    for run_index in range(len(run_ids)):
        run_launches = by_run[run_index]
        events = sorted((row for launch in run_launches for row in launch.events), key=event_key)
        states = {launch.mint: base.State(latest_ns=launch.create_ns) for launch in run_launches}
        launch_by_mint = {launch.mint: launch for launch in run_launches}
        run_markers = sorted(
            ((marker[2], mint) for mint, marker in markers.items() if marker is not None and launches[mint].run_index == run_index),
            key=lambda value: (value[0], value[1]),
        )
        marker_pointer = 0
        recent: deque[str] = deque()

        for event in events:
            now_ns = integer(event.get("received_ns"))
            while marker_pointer < len(run_markers) and run_markers[marker_pointer][0] < now_ns:
                memory.apply(marker_details[run_markers[marker_pointer][1]])
                marker_pointer += 1
            mint = str(event.get("mint") or "")
            launch = launch_by_mint.get(mint)
            state = states.get(mint)
            if launch is None or state is None:
                continue
            kind = str(event.get("kind") or "").upper()
            trader = str(event.get("trader") or "")
            marker = markers.get(mint)
            if marker is not None and event_key(event) >= marker[1]:
                continue
            if trader == base.E4_WALLET:
                continue
            base.apply_event(launch, state, event)
            if kind not in {"CREATE", "BUY", "PUMPSWAP_BUY"}:
                continue
            snapshot = make_snapshot(launch, state, event, memory, marker, horizon_ms)
            if not eligible(snapshot):
                continue
            recent.append(mint)
            while recent:
                oldest = launch_by_mint.get(recent[0])
                if oldest is None or now_ns - oldest.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            active_rows = []
            seen = set()
            for active_mint in reversed(recent):
                if active_mint in seen:
                    continue
                seen.add(active_mint)
                active_launch = launch_by_mint[active_mint]
                active_state = states[active_mint]
                pseudo = {
                    "received_ns": now_ns,
                    "trader": "",
                    "sol_amount": 0.0,
                    "slot": active_state.buy_slots.most_common(1)[0][0] if active_state.buy_slots else active_launch.create_slot,
                    "signature": "",
                    "event_index": 0,
                }
                active_marker = markers.get(active_mint)
                active_row = make_snapshot(active_launch, active_state, pseudo, memory, active_marker, horizon_ms)
                if eligible(active_row):
                    active_rows.append(active_row)
            add_relative_features(snapshot, active_rows)
            snapshot["run_id"] = run_ids[run_index]
            snapshot["run_index"] = run_index
            dataset.append(snapshot)

        while marker_pointer < len(run_markers):
            memory.apply(marker_details[run_markers[marker_pointer][1]])
            marker_pointer += 1
        print(json.dumps({
            "run": run_ids[run_index],
            "snapshots": sum(integer(row["run_index"]) == run_index for row in dataset),
            "positive_snapshots": sum(integer(row["run_index"]) == run_index and row["positive"] for row in dataset),
        }), flush=True)
    return dataset


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([[row_features(row)[name] for name in RUNTIME_FEATURES] for row in rows], dtype=float)


@dataclass(frozen=True)
class Spec:
    family: str
    depth: int
    leaf: int
    estimators: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def training_rows(rows: list[dict[str, Any]], negative_ratio: int = 15) -> list[dict[str, Any]]:
    positives = [row for row in rows if row["positive"]]
    negatives = [row for row in rows if not row["positive"]]
    negatives.sort(key=lambda row: (
        row_features(row)["identity_strength"]
        + row_features(row)["slot_cluster_strength"]
        + 0.20 * row_features(row)["launch_velocity"]
        + 0.50 * row_features(row)["log_seed"]
    ), reverse=True)
    return positives + negatives[: max(1_500, len(positives) * negative_ratio)]


def fit(rows: list[dict[str, Any]], spec: Spec):
    chosen = training_rows(rows)
    x = matrix(chosen)
    y = np.asarray([int(row["positive"]) for row in chosen])
    if spec.family == "extra":
        model = ExtraTreesClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "forest":
        model = RandomForestClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
        model.fit(x, y)
        return model
    if spec.family == "hist":
        positives = max(1, int(y.sum()))
        weights = np.where(y == 1, max(1.0, (len(y) - positives) / positives), 1.0)
        model = HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            l2_regularization=2.0,
            random_state=712,
        )
        model.fit(x, y, sample_weight=weights)
        return model
    if spec.family == "logit":
        model = Pipeline([
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C=0.4, class_weight="balanced", max_iter=5_000, solver="liblinear")),
        ])
        model.fit(x, y)
        return model
    raise ValueError(spec.family)


def predict_probability(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    if not rows:
        return np.asarray([], dtype=float)
    return model.predict_proba(matrix(rows))[:, 1]


@dataclass(frozen=True)
class Gate:
    threshold: float
    minimum_probability_margin: float
    cooldown_ms: float
    require_identity_top: bool
    require_seed_or_velocity_top: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evaluate(
    rows: list[dict[str, Any]],
    model: Any,
    gate: Gate,
    horizon_ms: float,
) -> dict[str, Any]:
    scored = [dict(row) for row in rows]
    probabilities = predict_probability(model, scored)
    for row, probability in zip(scored, probabilities):
        row["probability"] = float(probability)
    scored.sort(key=lambda row: (integer(row["timestamp_ns"]), str(row["mint"])))
    positive_mints = {str(row["mint"]) for row in scored if row["positive"]}
    successful_mints = {str(row["mint"]) for row in scored if row["positive"] and row.get("intent_label") == "SUCCESS"}
    failed_mints = positive_mints - successful_mints
    decisions: dict[str, dict[str, Any]] = {}
    recent_candidates: deque[dict[str, Any]] = deque()
    cooldown_ns = int(gate.cooldown_ms * 1e6)
    last_global_ns = -10**30

    for row in scored:
        now_ns = integer(row["timestamp_ns"])
        mint = str(row["mint"])
        while recent_candidates and integer(recent_candidates[0]["timestamp_ns"]) < now_ns - 250_000_000:
            recent_candidates.popleft()
        recent_candidates.append(row)
        if mint in decisions:
            continue
        probability = finite(row.get("probability"))
        if probability < gate.threshold:
            continue
        if gate.require_identity_top and not bool(row.get("current_is_identity_top")):
            continue
        if gate.require_seed_or_velocity_top and not (
            bool(row.get("current_is_seed_top")) or bool(row.get("current_is_velocity_top"))
        ):
            continue
        best_other = max((finite(item.get("probability")) for item in recent_candidates if item["mint"] != mint), default=0.0)
        margin = probability - best_other
        if margin < gate.minimum_probability_margin:
            continue
        if now_ns - last_global_ns < cooldown_ns:
            continue
        intent_ns = integer(row.get("intent_ns"), 0)
        lead_ms = (intent_ns - now_ns) / 1e6 if intent_ns else None
        decision = {
            "mint": mint,
            "run_id": row.get("run_id"),
            "decision_ns": now_ns,
            "probability": probability,
            "margin": margin,
            "intent_ns": intent_ns or None,
            "intent_label": row.get("intent_label"),
            "lead_ms": lead_ms,
            "true": bool(intent_ns and 0.0 < lead_ms <= horizon_ms),
            "feature_values": {name: row_features(row)[name] for name in RUNTIME_FEATURES},
        }
        decisions[mint] = decision
        last_global_ns = now_ns

    predicted = set(decisions)
    true = {mint for mint, row in decisions.items() if row["true"]}
    false = predicted - true
    pre_intent = {mint for mint in true if finite(decisions[mint].get("lead_ms"), -1.0) > 0}
    return {
        "positive_mints": len(positive_mints),
        "successful_intent_mints": len(successful_mints),
        "failed_intent_mints": len(failed_mints),
        "predictions": len(predicted),
        "true": len(true),
        "false_positives": len(false),
        "precision": len(true) / len(predicted) if predicted else 0.0,
        "precision_wilson_low": wilson_lower(len(true), len(predicted)),
        "recall": len(true) / len(positive_mints) if positive_mints else 0.0,
        "success_recall": len(true & successful_mints) / len(successful_mints) if successful_mints else 0.0,
        "failed_attempt_recall": len(true & failed_mints) / len(failed_mints) if failed_mints else 0.0,
        "all_true_pre_intent": len(pre_intent) == len(true),
        "median_lead_ms": statistics.median(decisions[mint]["lead_ms"] for mint in true) if true else None,
        "decisions": list(sorted(decisions.values(), key=lambda row: integer(row["decision_ns"]))),
    }


def specs() -> list[Spec]:
    return [
        Spec("logit", 0, 0, 0),
        Spec("extra", 5, 3, 160),
        Spec("extra", 7, 4, 200),
        Spec("extra", 9, 6, 240),
        Spec("forest", 6, 4, 180),
        Spec("hist", 5, 12, 0),
    ]


def tune(train: list[dict[str, Any]], validation: list[dict[str, Any]], horizon_ms: float):
    best = None
    for spec in specs():
        model = fit(train, spec)
        probabilities = predict_probability(model, validation)
        thresholds = sorted(set(float(np.quantile(probabilities, quantile)) for quantile in (0.70,0.80,0.86,0.90,0.93,0.95,0.97,0.98,0.99,0.995))) if len(probabilities) else [1.0]
        for threshold in thresholds:
            for margin in (0.0, 0.025, 0.05, 0.075, 0.10, 0.15):
                for cooldown in (0.0, 50.0, 100.0, 250.0, 500.0):
                    for identity_top in (False, True):
                        for seed_or_velocity_top in (False, True):
                            gate = Gate(threshold, margin, cooldown, identity_top, seed_or_velocity_top)
                            result = evaluate(validation, model, gate, horizon_ms)
                            if result["true"] < 5 or result["recall"] < 0.10:
                                continue
                            valid = result["precision"] >= 0.55 and result["precision_wilson_low"] >= 0.30
                            objective = (
                                int(valid),
                                result["precision_wilson_low"],
                                result["precision"],
                                result["recall"],
                                result["median_lead_ms"] or 0.0,
                                result["true"],
                                -result["false_positives"],
                            )
                            if best is None or objective > best[0]:
                                best = (objective, spec, model, gate, result)
        print(json.dumps({"spec": spec.as_dict(), "best": best[0] if best else None}), flush=True)
    if best is None:
        raise RuntimeError("no sequential hazard rule produced five validation true positives")
    return best[1], best[2], best[3], best[4]


def export_tree(tree: Any) -> dict[str, Any]:
    value = tree.tree_
    def node(index: int) -> dict[str, Any]:
        left = int(value.children_left[index])
        right = int(value.children_right[index])
        distribution = value.value[index][0]
        probability = float(distribution[1] / max(1e-12, distribution.sum())) if len(distribution) > 1 else 0.0
        if left < 0 or right < 0:
            return {"leaf": True, "probability": probability}
        return {
            "leaf": False,
            "feature_index": int(value.feature[index]),
            "threshold": float(value.threshold[index]),
            "left": node(left),
            "right": node(right),
        }
    return node(0)


def export_model(model: Any, spec: Spec) -> dict[str, Any]:
    if spec.family in {"extra", "forest"}:
        return {
            "kind": "tree_ensemble",
            "features": RUNTIME_FEATURES,
            "trees": [export_tree(tree) for tree in model.estimators_],
        }
    if spec.family == "logit":
        scaler = model.named_steps["scale"]
        classifier = model.named_steps["model"]
        return {
            "kind": "logistic",
            "features": RUNTIME_FEATURES,
            "mean": [float(value) for value in scaler.mean_],
            "scale": [float(value) for value in scaler.scale_],
            "coefficient": [float(value) for value in classifier.coef_[0]],
            "intercept": float(classifier.intercept_[0]),
        }
    raise RuntimeError(f"model family {spec.family} is not runtime-exportable")


def feature_importance(model: Any, spec: Spec, limit: int = 15) -> list[dict[str, Any]]:
    if hasattr(model, "feature_importances_"):
        values = model.feature_importances_
    elif spec.family == "logit":
        values = np.abs(model.named_steps["model"].coef_[0])
    else:
        return []
    pairs = sorted(zip(RUNTIME_FEATURES, values), key=lambda pair: float(pair[1]), reverse=True)
    return [{"feature": name, "importance": float(value)} for name, value in pairs[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential causal E4 entry-hazard search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--attempts", action="append", default=[], type=Path)
    parser.add_argument("--horizon-ms", type=float, default=500.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, required=True)
    args = parser.parse_args()
    pairs = [base.parse_pair(value) for value in args.pair]
    if len(pairs) < 6:
        parser.error("at least six chronological live samples are required")
    launches, run_ids = base.load_launches(pairs)
    failed = base.load_failed_attempts(args.attempts)
    dataset = build_dataset(launches, failed, run_ids, args.horizon_ms)

    live_index = len(run_ids) - 1
    validation_start = max(3, live_index - 3)
    train = [row for row in dataset if integer(row["run_index"]) < validation_start]
    validation = [row for row in dataset if validation_start <= integer(row["run_index"]) < live_index]
    pre_live = [row for row in dataset if integer(row["run_index"]) < live_index]
    live = [row for row in dataset if integer(row["run_index"]) == live_index]

    spec, seed_model, gate, validation_result = tune(train, validation, args.horizon_ms)
    model = fit(pre_live, spec)
    live_result = evaluate(live, model, gate, args.horizon_ms)

    folds = []
    aggregate_true = aggregate_predictions = aggregate_positive = 0
    for fold in range(max(3, validation_start), len(run_ids)):
        fold_train = [row for row in dataset if integer(row["run_index"]) < fold]
        fold_rows = [row for row in dataset if integer(row["run_index"]) == fold]
        fold_model = fit(fold_train, spec)
        result = evaluate(fold_rows, fold_model, gate, args.horizon_ms)
        result["run_id"] = run_ids[fold]
        result.pop("decisions", None)
        folds.append(result)
        aggregate_true += integer(result["true"])
        aggregate_predictions += integer(result["predictions"])
        aggregate_positive += integer(result["positive_mints"])
    walk = {
        "folds": folds,
        "true": aggregate_true,
        "predictions": aggregate_predictions,
        "positive_mints": aggregate_positive,
        "precision": aggregate_true / aggregate_predictions if aggregate_predictions else 0.0,
        "precision_wilson_low": wilson_lower(aggregate_true, aggregate_predictions),
        "recall": aggregate_true / aggregate_positive if aggregate_positive else 0.0,
    }

    exportable = spec.family in {"extra", "forest", "logit"}
    passed = bool(
        exportable
        and validation_result["precision"] >= 0.55
        and validation_result["recall"] >= 0.10
        and validation_result["true"] >= 5
        and walk["precision"] >= 0.50
        and walk["precision_wilson_low"] >= 0.25
        and walk["true"] >= 10
        and live_result["precision"] >= 0.50
        and live_result["recall"] >= 0.10
        and live_result["true"] >= 2
        and live_result["all_true_pre_intent"]
    )
    status = "LIVE_HOLDOUT_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    thesis = (
        "E4 enters when a launch reaches the highest short-horizon entry hazard among active unsold low-FDV mints: "
        "the hazard is driven jointly by creator/first-buyer intent history, creator seed, first-slot transaction topology, "
        "relative rank versus simultaneous launches and launch velocity."
    )
    model_payload = {
        "version": "e4-v12-sequential-hazard-v1",
        "status": status,
        "thesis": thesis,
        "horizon_ms": args.horizon_ms,
        "guardrails": {
            "minimum_creator_seed_sol": 0.20,
            "minimum_fdv_usd": 2_750.0,
            "maximum_fdv_usd": 10_000.0,
            "maximum_age_ms": 1_500.0,
            "pre_entry_sell_count": 0,
            "mayhem_allowed": False,
        },
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "model": export_model(model, spec) if exportable else None,
        "top_drivers": feature_importance(model, spec),
        "training_runs": run_ids[:live_index],
        "live_run": run_ids[live_index],
        "validation": {key:value for key,value in validation_result.items() if key != "decisions"},
        "walk_forward": walk,
        "live_holdout": {key:value for key,value in live_result.items() if key != "decisions"},
    }
    report = {
        "version": "e4-v12-sequential-hazard-report-v1",
        "status": status,
        "coverage": {
            "runs": run_ids,
            "launches": len(launches),
            "snapshots": len(dataset),
            "positive_snapshots": sum(row["positive"] for row in dataset),
            "positive_mints": len({row["mint"] for row in dataset if row["positive"]}),
            "mapped_failed_intent_mints": len({row["mint"] for row in dataset if row["positive"] and row.get("intent_label") == "FAILED_ATTEMPT"}),
        },
        "causality": {
            "features": "only state available after each non-E4 CREATE/BUY event",
            "label": f"E4 successful or failed BUY intent occurs in the next {args.horizon_ms:.0f}ms",
            "history": "only E4 intent markers strictly earlier than the snapshot",
            "competition": "active unique launches reconstructed globally at the same timestamp",
            "live_holdout": run_ids[live_index],
        },
        "thesis": thesis,
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "top_drivers": model_payload["top_drivers"],
        "validation": validation_result,
        "walk_forward": walk,
        "live_holdout": live_result,
        "safe_to_implement": passed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.model_output.write_text(json.dumps(model_payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": status,
        "coverage": report["coverage"],
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "top_drivers": model_payload["top_drivers"],
        "validation": {key:validation_result[key] for key in ("positive_mints","predictions","true","precision","precision_wilson_low","recall","median_lead_ms")},
        "walk_forward": walk,
        "live_holdout": {key:live_result[key] for key in ("positive_mints","predictions","true","precision","recall","success_recall","failed_attempt_recall","median_lead_ms")},
    }, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 7


if __name__ == "__main__":
    raise SystemExit(main())
