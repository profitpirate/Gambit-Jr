#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import e4_v12_true_latency_replay as replay


FEATURES = [
    "log_seed",
    "log_outside",
    "log_fdv",
    "age_100ms",
    "buy_count",
    "unique_buyers",
    "same_slot_buys",
    "same_slot_unique",
    "seed_share",
    "first_buyer_age_100ms",
    "second_buyer_age_100ms",
    "interbuyer_100ms",
    "distinct_buy_signatures",
    "max_buys_one_signature",
    "max_buys_one_slot",
    "create_signature_buys",
    "price_multiple_clip",
    "prior_creator_attempts_log",
    "prior_creator_wins_log",
    "prior_creator_losses_log",
    "creator_win_rate",
    "known_buyer_count",
    "sum_prior_buyer_attempts_log",
    "sum_prior_buyer_wins_log",
    "max_prior_buyer_attempts_log",
    "max_pair_attempts_log",
    "identity_strength",
    "launch_velocity",
    "slot_cluster_strength",
    "seed_to_fdv",
    "outside_to_fdv",
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
    "trigger_is_creator",
    "trigger_is_known_buyer",
    "trigger_buyer_attempts_log",
    "trigger_buyer_wins_log",
    "trigger_sol_log",
    "trigger_same_create_slot",
    "trigger_same_create_signature",
    "fdv_core_band",
    "very_early_50ms",
    "very_early_150ms",
    "very_early_400ms",
]


def finite(value: Any, default: float = 0.0) -> float:
    return replay.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return replay.integer(value, default)


def log1p(value: Any) -> float:
    return math.log1p(max(0.0, finite(value)))


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


@dataclass
class LaunchState:
    mint: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    mayhem: bool = False
    creator_seed_sol: float = 0.0
    outside_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    unique_buyers: set[str] = field(default_factory=set)
    first_buyers: list[str] = field(default_factory=list)
    first_buyer_ns: list[int] = field(default_factory=list)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    buy_slots: Counter[int] = field(default_factory=Counter)
    same_slot_unique: set[str] = field(default_factory=set)
    fdv_usd: float = 0.0
    price_sol: float = 0.0
    initial_price_sol: float = 0.0
    snapshot_count: int = 0


@dataclass
class Memory:
    creator_attempts: Counter[str] = field(default_factory=Counter)
    creator_wins: Counter[str] = field(default_factory=Counter)
    creator_losses: Counter[str] = field(default_factory=Counter)
    buyer_attempts: Counter[str] = field(default_factory=Counter)
    buyer_wins: Counter[str] = field(default_factory=Counter)
    pair_attempts: Counter[str] = field(default_factory=Counter)
    open_intents: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = field(default_factory=dict)

    def observe_intent(self, run_id: str, state: LaunchState) -> None:
        creator = state.creator
        buyers = tuple(state.first_buyers[:8])
        if creator:
            self.creator_attempts[creator] += 1
        for buyer in buyers:
            self.buyer_attempts[buyer] += 1
            if creator:
                self.pair_attempts[f"{creator}|{buyer}"] += 1
        self.open_intents[(run_id, state.mint)] = (creator, buyers)

    def observe_outcome(self, run_id: str, mint: str, won: bool) -> None:
        creator, buyers = self.open_intents.pop((run_id, mint), ("", ()))
        if creator:
            (self.creator_wins if won else self.creator_losses)[creator] += 1
        if won:
            for buyer in buyers:
                self.buyer_wins[buyer] += 1


def launch_metadata(run: replay.RunData) -> dict[str, LaunchState]:
    output: dict[str, LaunchState] = {}
    for mint, rows in run.events_by_mint.items():
        create = next(
            (row for row in rows if str(row.get("kind") or "").upper() == "CREATE"),
            rows[0] if rows else None,
        )
        if create is None:
            continue
        raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
        creator = str(
            create.get("creator")
            or raw.get("creator")
            or create.get("trader")
            or ""
        )
        output[mint] = LaunchState(
            mint=mint,
            creator=creator,
            create_ns=integer(create.get("received_ns")),
            create_slot=integer(create.get("slot")),
            create_signature=str(create.get("signature") or ""),
            mayhem=bool(raw.get("is_mayhem_mode") or raw.get("mayhem")),
        )
    return output


def apply_event(state: LaunchState, row: Mapping[str, Any]) -> None:
    kind = str(row.get("kind") or "").upper()
    trader = str(row.get("trader") or "")
    fdv = finite(row.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    if fdv > 0:
        state.fdv_usd = fdv
    if price > 0:
        state.price_sol = price
        if state.initial_price_sol <= 0:
            state.initial_price_sol = price
    if kind in replay.BUY_KINDS:
        if trader == replay.E4_WALLET:
            return
        amount = max(0.0, finite(row.get("sol_amount")))
        state.buy_count += 1
        signature = str(row.get("signature") or "")
        slot = integer(row.get("slot"))
        state.buy_signatures[signature] += 1
        state.buy_slots[slot] += 1
        if trader == state.creator:
            state.creator_seed_sol += amount
        elif trader:
            state.outside_sol += amount
            if trader not in state.unique_buyers:
                state.unique_buyers.add(trader)
                if len(state.first_buyers) < 12:
                    state.first_buyers.append(trader)
                    state.first_buyer_ns.append(integer(row.get("received_ns")))
            if slot == state.create_slot:
                state.same_slot_unique.add(trader)
    elif kind in replay.SELL_KINDS and trader != replay.E4_WALLET:
        state.sell_count += 1


def historical_values(state: LaunchState, memory: Memory) -> dict[str, float]:
    creator = state.creator
    attempts = memory.creator_attempts[creator]
    wins = memory.creator_wins[creator]
    losses = memory.creator_losses[creator]
    buyers = state.first_buyers
    buyer_attempts = [memory.buyer_attempts[value] for value in buyers]
    buyer_wins = [memory.buyer_wins[value] for value in buyers]
    pairs = [memory.pair_attempts[f"{creator}|{value}"] for value in buyers]
    total_known = sum(value > 0 for value in buyer_attempts)
    identity = (
        1.6 * log1p(wins)
        + 1.1 * log1p(attempts)
        - 0.9 * log1p(losses)
        + 0.9 * log1p(sum(buyer_attempts))
        + 0.8 * log1p(sum(buyer_wins))
        + 0.7 * log1p(max(pairs, default=0))
    )
    return {
        "attempts": float(attempts),
        "wins": float(wins),
        "losses": float(losses),
        "win_rate": wins / max(1, wins + losses),
        "known_buyer_count": float(total_known),
        "sum_buyer_attempts": float(sum(buyer_attempts)),
        "sum_buyer_wins": float(sum(buyer_wins)),
        "max_buyer_attempts": float(max(buyer_attempts, default=0)),
        "max_pair_attempts": float(max(pairs, default=0)),
        "identity_strength": identity,
    }


def base_snapshot(
    run_id: str,
    state: LaunchState,
    row: Mapping[str, Any],
    memory: Memory,
    source_buy: Mapping[str, Any] | None,
    source_won: bool,
    horizon_ms: float,
    minimum_lead_ms: float,
) -> dict[str, Any]:
    now_ns = integer(row.get("received_ns"))
    age_ms = max(0.0, (now_ns - state.create_ns) / 1e6)
    history = historical_values(state, memory)
    first_age = (
        (state.first_buyer_ns[0] - state.create_ns) / 1e6
        if state.first_buyer_ns else 9_999.0
    )
    second_age = (
        (state.first_buyer_ns[1] - state.create_ns) / 1e6
        if len(state.first_buyer_ns) > 1 else 9_999.0
    )
    interbuyer = (
        statistics.median(
            (right - left) / 1e6
            for left, right in zip(state.first_buyer_ns, state.first_buyer_ns[1:])
        )
        if len(state.first_buyer_ns) > 1 else 9_999.0
    )
    source_ns = integer(source_buy.get("received_ns")) if source_buy else 0
    source_sequence = integer(source_buy.get("__sequence"), -1) if source_buy else -1
    current_sequence = integer(row.get("__sequence"), -1)
    source_after = bool(
        source_buy
        and (now_ns, current_sequence) < (source_ns, source_sequence)
    )
    lead_ms = (source_ns - now_ns) / 1e6 if source_after else None
    target_intent = bool(
        source_after
        and lead_ms is not None
        and minimum_lead_ms <= lead_ms <= horizon_ms
    )
    target = bool(target_intent and source_won)
    seed = state.creator_seed_sol
    outside = state.outside_sol
    fdv = max(1.0, state.fdv_usd)
    buyers = max(1, len(state.unique_buyers))
    price_multiple = (
        state.price_sol / state.initial_price_sol
        if state.price_sol > 0 and state.initial_price_sol > 0 else 1.0
    )
    signature_counts = list(state.buy_signatures.values())
    trigger = str(row.get("trader") or "")
    trigger_attempts = memory.buyer_attempts[trigger] if trigger else 0
    trigger_wins = memory.buyer_wins[trigger] if trigger else 0
    values = {
        "log_seed": log1p(seed),
        "log_outside": log1p(outside),
        "log_fdv": log1p(fdv),
        "age_100ms": min(20.0, age_ms / 100.0),
        "buy_count": float(state.buy_count),
        "unique_buyers": float(len(state.unique_buyers)),
        "same_slot_buys": float(state.buy_slots.get(state.create_slot, 0)),
        "same_slot_unique": float(len(state.same_slot_unique)),
        "seed_share": seed / max(1e-9, seed + outside),
        "first_buyer_age_100ms": min(100.0, first_age / 100.0),
        "second_buyer_age_100ms": min(100.0, second_age / 100.0),
        "interbuyer_100ms": min(100.0, interbuyer / 100.0),
        "distinct_buy_signatures": float(sum(bool(key) for key in state.buy_signatures)),
        "max_buys_one_signature": float(max(signature_counts, default=0)),
        "max_buys_one_slot": float(max(state.buy_slots.values(), default=0)),
        "create_signature_buys": float(state.buy_signatures.get(state.create_signature, 0)),
        "price_multiple_clip": min(10.0, max(0.0, price_multiple)),
        "prior_creator_attempts_log": log1p(history["attempts"]),
        "prior_creator_wins_log": log1p(history["wins"]),
        "prior_creator_losses_log": log1p(history["losses"]),
        "creator_win_rate": history["win_rate"],
        "known_buyer_count": history["known_buyer_count"],
        "sum_prior_buyer_attempts_log": log1p(history["sum_buyer_attempts"]),
        "sum_prior_buyer_wins_log": log1p(history["sum_buyer_wins"]),
        "max_prior_buyer_attempts_log": log1p(history["max_buyer_attempts"]),
        "max_pair_attempts_log": log1p(history["max_pair_attempts"]),
        "identity_strength": history["identity_strength"],
        "launch_velocity": (state.buy_count + len(state.unique_buyers)) / max(0.025, age_ms / 1_000.0),
        "slot_cluster_strength": float(state.buy_slots.get(state.create_slot, 0)) + 0.75 * len(state.same_slot_unique),
        "seed_to_fdv": seed / fdv * 10_000.0,
        "outside_to_fdv": outside / fdv * 10_000.0,
        "trigger_is_creator": float(bool(trigger and trigger == state.creator)),
        "trigger_is_known_buyer": float(trigger_attempts > 0),
        "trigger_buyer_attempts_log": log1p(trigger_attempts),
        "trigger_buyer_wins_log": log1p(trigger_wins),
        "trigger_sol_log": log1p(row.get("sol_amount")),
        "trigger_same_create_slot": float(integer(row.get("slot")) == state.create_slot),
        "trigger_same_create_signature": float(str(row.get("signature") or "") == state.create_signature),
        "fdv_core_band": float(3_200.0 <= fdv <= 7_500.0),
        "very_early_50ms": float(age_ms <= 50.0),
        "very_early_150ms": float(age_ms <= 150.0),
        "very_early_400ms": float(age_ms <= 400.0),
    }
    return {
        "run_id": run_id,
        "mint": state.mint,
        "creator": state.creator,
        "decision_ns": now_ns,
        "decision_sequence": current_sequence,
        "decision_event_id": row.get("event_id"),
        "decision_signature": str(row.get("signature") or ""),
        "decision_event_index": integer(row.get("event_index")),
        "age_ms": age_ms,
        "creator_seed_sol": seed,
        "outside_sol": outside,
        "fdv_usd": fdv,
        "sell_count": state.sell_count,
        "lead_ms": lead_ms,
        "source_won": source_won,
        "target_intent": target_intent,
        "target": target,
        **values,
    }


def eligible(row: Mapping[str, Any]) -> bool:
    return bool(
        integer(row.get("sell_count")) == 0
        and 2_750.0 <= finite(row.get("fdv_usd")) <= 10_000.0
        and finite(row.get("creator_seed_sol")) >= 0.02
        and finite(row.get("age_ms")) <= 1_500.0
    )


def add_relative_features(
    current: dict[str, Any],
    active: Sequence[tuple[LaunchState, dict[str, float]]],
) -> None:
    if not active:
        active = []
    count = max(1, len(active))
    current_values = {
        "seed": finite(current.get("log_seed")),
        "identity": finite(current.get("identity_strength")),
        "velocity": finite(current.get("launch_velocity")),
        "buyers": finite(current.get("unique_buyers")),
    }
    rows = [
        {
            "mint": state.mint,
            "seed": log1p(state.creator_seed_sol),
            "identity": values["identity_strength"],
            "velocity": (state.buy_count + len(state.unique_buyers)) / max(0.025, finite(values.get("age_ms"), 1.0) / 1_000.0),
            "buyers": float(len(state.unique_buyers)),
        }
        for state, values in active
    ]
    if not any(row["mint"] == current["mint"] for row in rows):
        rows.append({"mint": current["mint"], **current_values})
    def rank(name: str) -> tuple[int, float]:
        target = current_values[name]
        other = [row[name] for row in rows if row["mint"] != current["mint"]]
        return 1 + sum(value > target for value in other), target - max(other, default=0.0)
    seed_rank, seed_gap = rank("seed")
    identity_rank, identity_gap = rank("identity")
    velocity_rank, velocity_gap = rank("velocity")
    buyers_rank, _ = rank("buyers")
    current.update({
        "active_count_log": log1p(count),
        "seed_rank_inverse": 1.0 / seed_rank,
        "identity_rank_inverse": 1.0 / identity_rank,
        "velocity_rank_inverse": 1.0 / velocity_rank,
        "buyers_rank_inverse": 1.0 / buyers_rank,
        "current_is_seed_top": float(seed_rank == 1),
        "current_is_identity_top": float(identity_rank == 1),
        "current_is_velocity_top": float(velocity_rank == 1),
        "seed_gap_to_best": seed_gap,
        "identity_gap_to_best": identity_gap,
        "velocity_gap_to_best": velocity_gap,
    })


def build_dataset(
    ordered_runs: Sequence[replay.RunData],
    *,
    horizon_ms: float,
    minimum_lead_ms: float,
) -> list[dict[str, Any]]:
    memory = Memory()
    dataset: list[dict[str, Any]] = []
    for run_index, run in enumerate(ordered_runs):
        states = launch_metadata(run)
        sources = {
            mint: replay.source_events(rows)
            for mint, rows in run.events_by_mint.items()
        }
        final_sell_sequence = {
            mint: integer(sells[-1].get("__sequence"), -1)
            for mint, (_, sells) in sources.items() if sells
        }
        global_events = sorted(
            (
                (mint, row)
                for mint, rows in run.events_by_mint.items()
                for row in rows
            ),
            key=lambda item: replay.event_sort_key(item[1]),
        )
        recent: deque[str] = deque()
        for mint, row in global_events:
            state = states.get(mint)
            if state is None:
                continue
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            source_buy, source_sells = sources.get(mint, (None, []))
            position = run.e4_positions.get(mint, {})
            source_won = finite(position.get("pnl_sol")) > 0

            if trader == replay.E4_WALLET:
                if kind in replay.BUY_KINDS:
                    memory.observe_intent(run.run_id, state)
                elif kind in replay.SELL_KINDS and integer(row.get("__sequence"), -2) == final_sell_sequence.get(mint, -1):
                    memory.observe_outcome(run.run_id, mint, source_won)
                continue

            apply_event(state, row)
            if kind not in {"CREATE", *replay.BUY_KINDS}:
                continue
            if state.snapshot_count >= 6:
                continue
            # Do not manufacture a pre-entry sample after the source decision.
            if source_buy is not None and (
                integer(row.get("received_ns")), integer(row.get("__sequence"), -1)
            ) >= (
                integer(source_buy.get("received_ns")), integer(source_buy.get("__sequence"), -1)
            ):
                continue

            snapshot = base_snapshot(
                run.run_id,
                state,
                row,
                memory,
                source_buy,
                source_won,
                horizon_ms,
                minimum_lead_ms,
            )
            if not eligible(snapshot):
                continue
            state.snapshot_count += 1
            recent.append(mint)
            now_ns = integer(row.get("received_ns"))
            while recent:
                first = states.get(recent[0])
                if first is None or now_ns - first.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            active: list[tuple[LaunchState, dict[str, float]]] = []
            seen: set[str] = set()
            for active_mint in reversed(recent):
                if active_mint in seen:
                    continue
                seen.add(active_mint)
                active_state = states.get(active_mint)
                if active_state is None or active_state.sell_count > 0:
                    continue
                history = historical_values(active_state, memory)
                active.append((active_state, {**history, "age_ms": (now_ns - active_state.create_ns) / 1e6}))
            add_relative_features(snapshot, active)
            snapshot["run_index"] = run_index
            dataset.append(snapshot)

        # Closed positions missing a final sell event are made historical only
        # after the whole run, never before an earlier decision in that run.
        for mint, position in run.e4_positions.items():
            if (run.run_id, mint) in memory.open_intents:
                memory.observe_outcome(run.run_id, mint, finite(position.get("pnl_sol")) > 0)
        print(json.dumps({
            "run_id": run.run_id,
            "snapshots": sum(integer(row["run_index"]) == run_index for row in dataset),
            "positive_winning_intent_snapshots": sum(integer(row["run_index"]) == run_index and row["target"] for row in dataset),
        }, sort_keys=True), flush=True)
    return dataset


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([[finite(row.get(name)) for name in FEATURES] for row in rows], dtype=float)


@dataclass(frozen=True)
class Spec:
    family: str
    depth: int
    leaf: int
    estimators: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def specs() -> list[Spec]:
    return [
        Spec("logit", 0, 0, 0),
        Spec("extra", 5, 3, 320),
        Spec("extra", 7, 4, 400),
        Spec("extra", 9, 6, 480),
        Spec("forest", 6, 3, 360),
        Spec("forest", 8, 5, 440),
    ]


def training_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positives = [row for row in rows if row["target"]]
    negatives = [row for row in rows if not row["target"]]
    negatives.sort(
        key=lambda row: (
            finite(row.get("identity_strength"))
            + 0.8 * finite(row.get("slot_cluster_strength"))
            + 0.10 * finite(row.get("launch_velocity"))
            + 0.50 * finite(row.get("log_seed"))
        ),
        reverse=True,
    )
    hard = negatives[: max(2_000, len(positives) * 25)]
    remaining = negatives[len(hard):]
    random.Random(712).shuffle(remaining)
    return positives + hard + remaining[: max(1_000, len(positives) * 10)]


def fit_model(rows: list[dict[str, Any]], spec: Spec):
    chosen = training_rows(rows)
    x = matrix(chosen)
    y = np.asarray([int(row["target"]) for row in chosen], dtype=int)
    if y.sum() < 2:
        raise RuntimeError("insufficient positive training samples")
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
    elif spec.family == "forest":
        model = RandomForestClassifier(
            n_estimators=spec.estimators,
            max_depth=spec.depth,
            min_samples_leaf=spec.leaf,
            max_features="sqrt",
            class_weight="balanced_subsample",
            random_state=712,
            n_jobs=-1,
        )
    elif spec.family == "logit":
        model = Pipeline([
            ("scale", StandardScaler()),
            ("model", LogisticRegression(
                C=0.35,
                class_weight="balanced",
                max_iter=5_000,
                solver="liblinear",
                random_state=712,
            )),
        ])
    else:
        raise ValueError(spec.family)
    model.fit(x, y)
    return model


def probabilities(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    if not rows:
        return np.asarray([], dtype=float)
    return model.predict_proba(matrix(rows))[:, 1]


@dataclass(frozen=True)
class Gate:
    threshold: float
    minimum_margin: float
    cooldown_ms: float
    maximum_age_ms: float
    require_identity_top: bool
    require_seed_or_velocity_top: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def select_predictions(
    rows: Sequence[dict[str, Any]],
    model: Any,
    gate: Gate,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scored = [dict(row) for row in rows]
    for row, probability in zip(scored, probabilities(model, scored)):
        row["probability"] = float(probability)
    scored.sort(key=lambda row: (integer(row["decision_ns"]), str(row["mint"])))
    recent: deque[dict[str, Any]] = deque()
    predicted_mints: set[tuple[str, str]] = set()
    predictions: list[dict[str, Any]] = []
    last_global_by_run: dict[str, int] = defaultdict(lambda: -10**30)
    for row in scored:
        run_id = str(row["run_id"])
        now_ns = integer(row["decision_ns"])
        while recent and (
            str(recent[0]["run_id"]) != run_id
            or integer(recent[0]["decision_ns"]) < now_ns - 250_000_000
        ):
            recent.popleft()
        recent.append(row)
        key = (run_id, str(row["mint"]))
        if key in predicted_mints:
            continue
        probability = finite(row.get("probability"))
        if probability < gate.threshold or finite(row.get("age_ms")) > gate.maximum_age_ms:
            continue
        if gate.require_identity_top and not bool(row.get("current_is_identity_top")):
            continue
        if gate.require_seed_or_velocity_top and not (
            bool(row.get("current_is_seed_top"))
            or bool(row.get("current_is_velocity_top"))
        ):
            continue
        best_other = max(
            (
                finite(item.get("probability"))
                for item in recent
                if item["run_id"] == run_id and item["mint"] != row["mint"]
            ),
            default=0.0,
        )
        margin = probability - best_other
        if margin < gate.minimum_margin:
            continue
        if now_ns - last_global_by_run[run_id] < int(gate.cooldown_ms * 1e6):
            continue
        prediction = {
            key: row.get(key)
            for key in (
                "run_id", "mint", "decision_ns", "decision_sequence",
                "decision_event_id", "decision_signature", "decision_event_index",
                "lead_ms", "source_won", "target_intent", "target",
            )
        }
        prediction.update({
            "score": probability,
            "family": "v12_golden_profitable_intent",
            "probability": probability,
            "margin": margin,
            "entry_fraction": 0.0185,
        })
        predictions.append(prediction)
        predicted_mints.add(key)
        last_global_by_run[run_id] = now_ns
    true = sum(bool(row.get("target")) for row in predictions)
    intent = sum(bool(row.get("target_intent")) for row in predictions)
    return predictions, {
        "predictions": len(predictions),
        "winning_e4_true": true,
        "e4_intent_true": intent,
        "winning_precision": true / len(predictions) if predictions else 0.0,
        "intent_precision": intent / len(predictions) if predictions else 0.0,
        "winning_precision_wilson_low": wilson_lower(true, len(predictions)),
        "median_lead_ms": statistics.median(
            finite(row.get("lead_ms")) for row in predictions if row.get("target")
        ) if true else None,
    }


def aggregate_economics(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    positions = [row for result in results for row in result.get("positions") or []]
    rejections = [row for result in results for row in result.get("rejections") or []]
    wins = sum(finite(row.get("pnl_sol")) > 0 for row in positions)
    losses = len(positions) - wins
    gross_positive = sum(finite(row.get("pnl_sol")) for row in positions if finite(row.get("pnl_sol")) > 0)
    gross_negative = sum(finite(row.get("pnl_sol")) for row in positions if finite(row.get("pnl_sol")) < 0)
    rejection_fees = sum(finite(row.get("fee_sol")) for row in rejections)
    pnl = sum(finite(row.get("pnl_sol")) for row in positions) - rejection_fees
    return {
        "closed": len(positions),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(positions) if positions else 0.0,
        "wilson_low": wilson_lower(wins, len(positions)),
        "net_pnl_sol": pnl,
        "profit_factor": (
            gross_positive / abs(gross_negative)
            if gross_negative < 0 else (999.0 if gross_positive > 0 else 0.0)
        ),
        "output_rejections": sum(
            str(row.get("reason") or "").startswith("BuyExactSolIn") for row in rejections
        ),
        "rejection_fees_sol": rejection_fees,
        "positions": positions,
        "rejections": rejections,
    }


def economic_grid(
    run_map: Mapping[str, replay.RunData],
    predictions: Sequence[Mapping[str, Any]],
    *,
    floor_bps: int,
    latencies: Sequence[float],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in predictions:
        by_run[str(row.get("run_id") or "")].append(row)
    for latency in latencies:
        results = []
        for run_id, rows in by_run.items():
            if run_id not in run_map:
                continue
            results.append(replay.portfolio(
                {run_id: run_map[run_id]},
                rows,
                latency_ms=latency,
                output_shortfall_bps=floor_bps,
                starting_balance_sol=3.0,
                entry_fraction=0.0185,
                maximum_position_sol=0.30,
                reserve_sol=0.03,
                pump_fee_bps=125,
                confirmation_ms=1_500.0,
                unconfirmed_timeout_ms=1_500.0,
                max_concurrency=2,
            ))
        output[str(latency)] = aggregate_economics(results)
    return output


def economics_pass(grid: Mapping[str, Mapping[str, Any]], minimum_trades: int) -> bool:
    return bool(grid) and all(
        integer(row.get("closed")) >= minimum_trades
        and finite(row.get("win_rate")) >= 0.65
        and finite(row.get("wilson_low")) >= 0.30
        and finite(row.get("net_pnl_sol")) > 0
        and finite(row.get("profit_factor")) >= 1.25
        for row in grid.values()
    )


def tune(
    train: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    run_map: Mapping[str, replay.RunData],
    latencies: Sequence[float],
):
    best = None
    for spec in specs():
        model = fit_model(train, spec)
        scores = probabilities(model, validation)
        if not len(scores):
            continue
        thresholds = sorted(set(float(np.quantile(scores, q)) for q in (
            0.90, 0.93, 0.95, 0.97, 0.98, 0.985, 0.99, 0.995, 0.997, 0.999
        )))
        for threshold in thresholds:
            for margin in (0.0, 0.025, 0.05, 0.075, 0.10, 0.15, 0.20):
                for cooldown in (0.0, 50.0, 100.0, 250.0, 500.0):
                    for max_age in (50.0, 150.0, 400.0, 1_000.0):
                        for identity_top in (False, True):
                            for seed_velocity_top in (False, True):
                                gate = Gate(
                                    threshold,
                                    margin,
                                    cooldown,
                                    max_age,
                                    identity_top,
                                    seed_velocity_top,
                                )
                                predictions, selection = select_predictions(validation, model, gate)
                                if selection["predictions"] < 4:
                                    continue
                                # Do not spend reserve replay time on obviously
                                # indiscriminate gates.
                                if selection["winning_precision"] < 0.40 and selection["intent_precision"] < 0.55:
                                    continue
                                for floor in (200, 400, 600, 800, 1_000):
                                    grid = economic_grid(
                                        run_map,
                                        predictions,
                                        floor_bps=floor,
                                        latencies=latencies,
                                    )
                                    passed = economics_pass(grid, 4)
                                    worst_wr = min(finite(row.get("win_rate")) for row in grid.values())
                                    worst_pf = min(finite(row.get("profit_factor")) for row in grid.values())
                                    total_pnl = sum(finite(row.get("net_pnl_sol")) for row in grid.values())
                                    objective = (
                                        int(passed),
                                        worst_wr,
                                        selection["winning_precision_wilson_low"],
                                        worst_pf,
                                        total_pnl,
                                        selection["winning_e4_true"],
                                        -selection["predictions"],
                                    )
                                    if best is None or objective > best[0]:
                                        best = (objective, spec, model, gate, floor, selection, grid)
        print(json.dumps({"spec": spec.as_dict(), "best": list(best[0]) if best else None}), flush=True)
    if best is None:
        raise RuntimeError("no candidate gate produced enough validation trades")
    return best


def compact_grid(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        latency: {
            key: value
            for key, value in row.items()
            if key not in {"positions", "rejections"}
        }
        for latency, row in grid.items()
    }


def search_mode(args: argparse.Namespace) -> int:
    pairs = [replay.parse_pair(value) for value in args.pair]
    ordered_runs = [replay.load_run(*value) for value in pairs]
    run_map = {run.run_id: run for run in ordered_runs}
    dataset = build_dataset(
        ordered_runs,
        horizon_ms=args.horizon_ms,
        minimum_lead_ms=args.minimum_lead_ms,
    )
    count = len(ordered_runs)
    if count < 8:
        raise SystemExit("at least eight chronological runs are required")
    train_end = count - 4
    validation_end = count - 2
    train = [row for row in dataset if integer(row["run_index"]) < train_end]
    validation = [
        row for row in dataset
        if train_end <= integer(row["run_index"]) < validation_end
    ]
    holdout = [row for row in dataset if integer(row["run_index"]) >= validation_end]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    objective, spec, seed_model, gate, floor, validation_selection, validation_grid = tune(
        train,
        validation,
        run_map,
        latencies,
    )
    combined = train + validation
    model = fit_model(combined, spec)
    holdout_predictions, holdout_selection = select_predictions(holdout, model, gate)
    holdout_grid = economic_grid(
        run_map,
        holdout_predictions,
        floor_bps=floor,
        latencies=latencies,
    )
    validation_ok = economics_pass(validation_grid, 4)
    holdout_ok = economics_pass(holdout_grid, 4)
    status = "HISTORICAL_GOLDEN_CONFIRMED" if validation_ok and holdout_ok else "NOT_CONCLUSIVE"
    bundle = {
        "version": "e4-v12-golden-profitable-intent-v2",
        "status": status,
        "features": FEATURES,
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "output_shortfall_bps": floor,
        "horizon_ms": args.horizon_ms,
        "minimum_lead_ms": args.minimum_lead_ms,
        "latencies_ms": latencies,
        "training_runs": [run.run_id for run in ordered_runs[:validation_end]],
        "historical_holdout_runs": [run.run_id for run in ordered_runs[validation_end:]],
        "model": model,
    }
    args.model_output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.model_output)
    report = {
        "version": "e4-v12-golden-thesis-search-v2",
        "status": status,
        "objective": list(objective),
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "output_shortfall_bps": floor,
        "horizon_ms": args.horizon_ms,
        "minimum_lead_ms": args.minimum_lead_ms,
        "coverage": {
            "runs": [run.run_id for run in ordered_runs],
            "snapshots": len(dataset),
            "positive_snapshots": sum(bool(row["target"]) for row in dataset),
        },
        "validation_selection": validation_selection,
        "validation_economics": compact_grid(validation_grid),
        "historical_holdout_selection": holdout_selection,
        "historical_holdout_economics": compact_grid(holdout_grid),
        "predictions": holdout_predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status", "spec", "gate", "output_shortfall_bps",
        "validation_selection", "historical_holdout_selection",
    )}, indent=2, sort_keys=True), flush=True)
    return 0 if status == "HISTORICAL_GOLDEN_CONFIRMED" else 1


def apply_mode(args: argparse.Namespace) -> int:
    bundle = joblib.load(args.model_input)
    if bundle.get("status") != "HISTORICAL_GOLDEN_CONFIRMED":
        raise SystemExit("model did not pass historical freeze")
    pairs = [replay.parse_pair(value) for value in args.pair]
    ordered_runs = [replay.load_run(*value) for value in pairs]
    run_map = {run.run_id: run for run in ordered_runs}
    dataset = build_dataset(
        ordered_runs,
        horizon_ms=finite(bundle["horizon_ms"]),
        minimum_lead_ms=finite(bundle["minimum_lead_ms"]),
    )
    live_index = len(ordered_runs) - 1
    live_rows = [row for row in dataset if integer(row["run_index"]) == live_index]
    gate = Gate(**bundle["gate"])
    predictions, selection = select_predictions(live_rows, bundle["model"], gate)
    latencies = [finite(value) for value in bundle["latencies_ms"]]
    grid = economic_grid(
        run_map,
        predictions,
        floor_bps=integer(bundle["output_shortfall_bps"]),
        latencies=latencies,
    )
    passed = economics_pass(grid, args.minimum_live_trades)
    status = "FRESH_LIVE_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-golden-thesis-live-v2",
        "status": status,
        "live_run": ordered_runs[-1].run_id,
        "selection": selection,
        "economics": compact_grid(grid),
        "predictions": predictions,
        "output_shortfall_bps": bundle["output_shortfall_bps"],
        "gate": bundle["gate"],
        "spec": bundle["spec"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status", "live_run", "selection", "economics"
    )}, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Find and freeze a profitable pre-E4 V12 entry thesis")
    parser.add_argument("--mode", choices=("search", "apply"), required=True)
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--horizon-ms", type=float, default=1_500.0)
    parser.add_argument("--minimum-lead-ms", type=float, default=12.0)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--minimum-live-trades", type=int, default=3)
    parser.add_argument("--model-output", type=Path)
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "search":
        if args.model_output is None:
            parser.error("--model-output is required in search mode")
        return search_mode(args)
    if args.model_input is None:
        parser.error("--model-input is required in apply mode")
    return apply_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
