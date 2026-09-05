#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from scripts import e4_v12_true_latency_replay as economics

E4_WALLET = economics.E4_WALLET
BUY_KINDS = economics.BUY_KINDS
SELL_KINDS = economics.SELL_KINDS

FEATURES = [
    "log_seed",
    "log_outside",
    "log_fdv",
    "age_100ms",
    "buy_count",
    "unique_buyers",
    "same_slot_buys",
    "same_slot_unique",
    "distinct_buy_signatures",
    "max_buys_one_signature",
    "create_signature_buys",
    "seed_share",
    "price_multiple_clip",
    "first_buyer_age_100ms",
    "second_buyer_age_100ms",
    "interbuyer_100ms",
    "launch_velocity",
    "outside_per_buyer",
    "prior_creator_attempts_log",
    "prior_creator_wins_log",
    "prior_creator_losses_log",
    "prior_creator_win_rate",
    "known_buyer_count",
    "max_prior_buyer_attempts_log",
    "sum_prior_buyer_attempts_log",
    "max_prior_buyer_wins_log",
    "sum_prior_buyer_wins_log",
    "max_creator_buyer_pair_log",
    "identity_strength",
    "trigger_is_creator",
    "trigger_is_known_buyer",
    "trigger_buyer_attempts_log",
    "trigger_sol_log",
    "trigger_same_create_slot",
    "trigger_same_create_signature",
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
    "fdv_core_band",
    "very_early_50ms",
    "very_early_150ms",
    "very_early_400ms",
    "seed_roundness",
]


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


def parse_pair(value: str) -> tuple[Path, Path]:
    left, right = value.split(":", 1)
    return Path(left), Path(right)


@dataclass
class LaunchState:
    creator_seed_sol: float = 0.0
    outside_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    unique_buyers: set[str] = field(default_factory=set)
    first_buyers: list[str] = field(default_factory=list)
    first_buyer_ns: list[int] = field(default_factory=list)
    same_slot_buys: int = 0
    same_slot_unique: set[str] = field(default_factory=set)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    fdv_usd: float = 0.0
    price_sol: float = 0.0
    initial_price_sol: float = 0.0
    latest_ns: int = 0


@dataclass
class Launch:
    mint: str
    run_index: int
    run_id: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    events: list[dict[str, Any]] = field(default_factory=list)
    e4_buy: dict[str, Any] | None = None
    e4_position: dict[str, Any] | None = None


@dataclass
class RunData:
    run_index: int
    run_id: str
    batch: dict[str, Any]
    grouped: dict[str, list[dict[str, Any]]]
    launches: dict[str, Launch]


@dataclass
class IntentMemory:
    creator_attempts: Counter[str] = field(default_factory=Counter)
    creator_wins: Counter[str] = field(default_factory=Counter)
    creator_losses: Counter[str] = field(default_factory=Counter)
    buyer_attempts: Counter[str] = field(default_factory=Counter)
    buyer_wins: Counter[str] = field(default_factory=Counter)
    pair_attempts: Counter[str] = field(default_factory=Counter)

    def apply_attempt(self, launch: Launch, first_buyers: Sequence[str]) -> None:
        if launch.creator:
            self.creator_attempts[launch.creator] += 1
        for buyer in first_buyers:
            if not buyer:
                continue
            self.buyer_attempts[buyer] += 1
            if launch.creator:
                self.pair_attempts[f"{launch.creator}|{buyer}"] += 1

    def apply_outcome(self, launch: Launch, first_buyers: Sequence[str]) -> None:
        if launch.e4_position is None:
            return
        won = finite(launch.e4_position.get("pnl_sol")) > 0
        if launch.creator:
            (self.creator_wins if won else self.creator_losses)[launch.creator] += 1
        if won:
            for buyer in first_buyers:
                if buyer:
                    self.buyer_wins[buyer] += 1


@dataclass(frozen=True)
class ModelSpec:
    family: str
    depth: int
    leaf: int
    estimators: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class Gate:
    threshold: float
    minimum_margin: float
    cooldown_ms: float
    require_identity_top: bool
    require_seed_or_velocity_top: bool
    require_prior_identity: bool
    maximum_predictions_per_run: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def first_buyers_before_e4(launch: Launch) -> list[str]:
    buyers: list[str] = []
    for row in launch.events:
        if launch.e4_buy is not None and economics.event_order(row) >= economics.event_order(launch.e4_buy):
            break
        if str(row.get("kind") or "").upper() not in BUY_KINDS:
            continue
        trader = str(row.get("trader") or "")
        if trader and trader not in {E4_WALLET, launch.creator} and trader not in buyers:
            buyers.append(trader)
        if len(buyers) >= 12:
            break
    return buyers


def apply_event(launch: Launch, state: LaunchState, row: Mapping[str, Any]) -> None:
    kind = str(row.get("kind") or "").upper()
    trader = str(row.get("trader") or "")
    timestamp = integer(row.get("received_ns"))
    state.latest_ns = max(state.latest_ns, timestamp)
    fdv = finite(row.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    if fdv > 0:
        state.fdv_usd = fdv
    if price > 0:
        state.price_sol = price
        if state.initial_price_sol <= 0:
            state.initial_price_sol = price
    if kind in BUY_KINDS:
        if trader == E4_WALLET:
            return
        amount = max(0.0, finite(row.get("sol_amount")))
        state.buy_count += 1
        signature = str(row.get("signature") or "")
        if signature:
            state.buy_signatures[signature] += 1
        slot = integer(row.get("slot"), -1)
        if slot == launch.create_slot:
            state.same_slot_buys += 1
        if trader == launch.creator:
            state.creator_seed_sol += amount
        elif trader:
            state.outside_sol += amount
            if trader not in state.unique_buyers:
                state.unique_buyers.add(trader)
                if len(state.first_buyers) < 12:
                    state.first_buyers.append(trader)
                    state.first_buyer_ns.append(timestamp)
            if slot == launch.create_slot:
                state.same_slot_unique.add(trader)
    elif kind in SELL_KINDS and trader != E4_WALLET:
        state.sell_count += 1


def load_runs(pairs: Sequence[tuple[Path, Path]]) -> list[RunData]:
    runs: list[RunData] = []
    for run_index, (batch_path, events_path) in enumerate(pairs):
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        run_id = str(
            batch.get("source_run")
            or batch.get("run_id")
            or (batch.get("capture") or {}).get("run_id")
            or batch_path.parent.parent.name
            or batch_path.stem
        )
        e4_positions = {
            str(row.get("mint") or ""): dict(row)
            for row in (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
            if row.get("mint")
        }
        grouped = economics.load_events(events_path)
        launches: dict[str, Launch] = {}
        for mint, rows in grouped.items():
            create = next((row for row in rows if str(row.get("kind") or "").upper() == "CREATE"), None)
            if create is None:
                continue
            raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
            creator = str(create.get("creator") or raw.get("creator") or create.get("trader") or "")
            e4_buy = next(
                (
                    dict(row)
                    for row in rows
                    if str(row.get("trader") or "") == E4_WALLET
                    and str(row.get("kind") or "").upper() in BUY_KINDS
                ),
                None,
            )
            launches[mint] = Launch(
                mint=mint,
                run_index=run_index,
                run_id=run_id,
                creator=creator,
                create_ns=integer(create.get("received_ns")),
                create_slot=integer(create.get("slot"), -1),
                create_signature=str(create.get("signature") or ""),
                events=[dict(row) for row in rows],
                e4_buy=e4_buy,
                e4_position=e4_positions.get(mint),
            )
        runs.append(RunData(run_index, run_id, batch, grouped, launches))
        print(json.dumps({"run_id": run_id, "launches": len(launches), "e4_positions": len(e4_positions)}), flush=True)
    return runs


def snapshot_base(
    launch: Launch,
    state: LaunchState,
    event: Mapping[str, Any],
    memory: IntentMemory,
    horizon_ms: float,
) -> dict[str, Any] | None:
    now_ns = integer(event.get("received_ns"))
    age_ms = max(0.0, (now_ns - launch.create_ns) / 1_000_000.0)
    if state.sell_count > 0 or state.fdv_usd <= 0 or state.fdv_usd > 12_000 or age_ms > 1_500:
        return None
    if state.creator_seed_sol < 0.02 and state.buy_count <= 0:
        return None

    first_age = (
        (state.first_buyer_ns[0] - launch.create_ns) / 1_000_000.0
        if state.first_buyer_ns
        else 9_999.0
    )
    second_age = (
        (state.first_buyer_ns[1] - launch.create_ns) / 1_000_000.0
        if len(state.first_buyer_ns) > 1
        else 9_999.0
    )
    interbuyer = (
        statistics.median(
            (right - left) / 1_000_000.0
            for left, right in zip(state.first_buyer_ns, state.first_buyer_ns[1:])
        )
        if len(state.first_buyer_ns) > 1
        else 9_999.0
    )
    buyers = list(state.first_buyers)
    prior_buyer_attempts = [memory.buyer_attempts[buyer] for buyer in buyers]
    prior_buyer_wins = [memory.buyer_wins[buyer] for buyer in buyers]
    pair_attempts = [memory.pair_attempts[f"{launch.creator}|{buyer}"] for buyer in buyers]
    creator_attempts = memory.creator_attempts[launch.creator]
    creator_wins = memory.creator_wins[launch.creator]
    creator_losses = memory.creator_losses[launch.creator]
    settled = creator_wins + creator_losses
    trader = str(event.get("trader") or "")
    signature = str(event.get("signature") or "")
    price_multiple = (
        state.price_sol / state.initial_price_sol
        if state.price_sol > 0 and state.initial_price_sol > 0
        else 1.0
    )
    e4_buy_ns = integer(launch.e4_buy.get("received_ns")) if launch.e4_buy is not None else 0
    lead_ms = (e4_buy_ns - now_ns) / 1_000_000.0 if e4_buy_ns > 0 else None
    e4_won = bool(launch.e4_position and finite(launch.e4_position.get("pnl_sol")) > 0)
    positive = bool(e4_won and lead_ms is not None and 0.0 < lead_ms <= horizon_ms)
    too_early_for_positive = bool(e4_won and lead_ms is not None and lead_ms > horizon_ms)

    return {
        "mint": launch.mint,
        "run_id": launch.run_id,
        "run_index": launch.run_index,
        "decision_ns": now_ns,
        "creator": launch.creator,
        "creator_seed_sol": state.creator_seed_sol,
        "outside_sol": state.outside_sol,
        "fdv_usd": state.fdv_usd,
        "age_ms": age_ms,
        "buy_count": state.buy_count,
        "sell_count": state.sell_count,
        "unique_buyers": len(state.unique_buyers),
        "same_slot_buys": state.same_slot_buys,
        "same_slot_unique": len(state.same_slot_unique),
        "distinct_buy_signatures": len(state.buy_signatures),
        "max_buys_one_signature": max(state.buy_signatures.values(), default=0),
        "create_signature_buys": state.buy_signatures.get(launch.create_signature, 0),
        "seed_share": state.creator_seed_sol / max(1e-12, state.creator_seed_sol + state.outside_sol),
        "price_multiple": price_multiple,
        "first_buyer_age_ms": first_age,
        "second_buyer_age_ms": second_age,
        "interbuyer_ms": interbuyer,
        "first_buyers": buyers,
        "prior_creator_attempts": creator_attempts,
        "prior_creator_wins": creator_wins,
        "prior_creator_losses": creator_losses,
        "prior_creator_win_rate": creator_wins / settled if settled else 0.0,
        "known_buyer_count": sum(value > 0 for value in prior_buyer_attempts),
        "max_prior_buyer_attempts": max(prior_buyer_attempts, default=0),
        "sum_prior_buyer_attempts": sum(prior_buyer_attempts),
        "max_prior_buyer_wins": max(prior_buyer_wins, default=0),
        "sum_prior_buyer_wins": sum(prior_buyer_wins),
        "max_creator_buyer_pair": max(pair_attempts, default=0),
        "trigger_is_creator": float(bool(trader and trader == launch.creator)),
        "trigger_is_known_buyer": float(bool(trader and memory.buyer_attempts[trader] > 0)),
        "trigger_buyer_attempts": memory.buyer_attempts[trader] if trader else 0,
        "trigger_sol": max(0.0, finite(event.get("sol_amount"))),
        "trigger_same_create_slot": float(integer(event.get("slot"), -2) == launch.create_slot),
        "trigger_same_create_signature": float(bool(signature and signature == launch.create_signature)),
        "positive": positive,
        "too_early_for_positive": too_early_for_positive,
        "e4_buy_ns": e4_buy_ns or None,
        "source_lead_ms": lead_ms,
        "e4_won": e4_won,
        "e4_pnl_sol": finite(launch.e4_position.get("pnl_sol")) if launch.e4_position else None,
    }


def feature_values(row: Mapping[str, Any]) -> dict[str, float]:
    seed = max(0.0, finite(row.get("creator_seed_sol")))
    outside = max(0.0, finite(row.get("outside_sol")))
    fdv = max(1.0, finite(row.get("fdv_usd")))
    buyers = max(1.0, finite(row.get("unique_buyers")))
    age_ms = max(0.0, finite(row.get("age_ms")))
    attempts = max(0.0, finite(row.get("prior_creator_attempts")))
    wins = max(0.0, finite(row.get("prior_creator_wins")))
    losses = max(0.0, finite(row.get("prior_creator_losses")))
    common_seed_distance = min(
        abs(seed - value)
        for value in (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 5.0, 6.0, 8.0)
    )
    values = {
        "log_seed": log1p(seed),
        "log_outside": log1p(outside),
        "log_fdv": log1p(fdv),
        "age_100ms": min(20.0, age_ms / 100.0),
        "buy_count": finite(row.get("buy_count")),
        "unique_buyers": finite(row.get("unique_buyers")),
        "same_slot_buys": finite(row.get("same_slot_buys")),
        "same_slot_unique": finite(row.get("same_slot_unique")),
        "distinct_buy_signatures": finite(row.get("distinct_buy_signatures")),
        "max_buys_one_signature": finite(row.get("max_buys_one_signature")),
        "create_signature_buys": finite(row.get("create_signature_buys")),
        "seed_share": finite(row.get("seed_share")),
        "price_multiple_clip": min(10.0, max(0.0, finite(row.get("price_multiple"), 1.0))),
        "first_buyer_age_100ms": min(100.0, finite(row.get("first_buyer_age_ms"), 9_999.0) / 100.0),
        "second_buyer_age_100ms": min(100.0, finite(row.get("second_buyer_age_ms"), 9_999.0) / 100.0),
        "interbuyer_100ms": min(100.0, finite(row.get("interbuyer_ms"), 9_999.0) / 100.0),
        "launch_velocity": (finite(row.get("buy_count")) + finite(row.get("unique_buyers"))) / max(0.05, age_ms / 1_000.0),
        "outside_per_buyer": outside / buyers,
        "prior_creator_attempts_log": log1p(attempts),
        "prior_creator_wins_log": log1p(wins),
        "prior_creator_losses_log": log1p(losses),
        "prior_creator_win_rate": finite(row.get("prior_creator_win_rate")),
        "known_buyer_count": finite(row.get("known_buyer_count")),
        "max_prior_buyer_attempts_log": log1p(row.get("max_prior_buyer_attempts")),
        "sum_prior_buyer_attempts_log": log1p(row.get("sum_prior_buyer_attempts")),
        "max_prior_buyer_wins_log": log1p(row.get("max_prior_buyer_wins")),
        "sum_prior_buyer_wins_log": log1p(row.get("sum_prior_buyer_wins")),
        "max_creator_buyer_pair_log": log1p(row.get("max_creator_buyer_pair")),
        "identity_strength": (
            1.5 * log1p(attempts)
            + 1.25 * log1p(wins)
            - 0.75 * log1p(losses)
            + log1p(row.get("sum_prior_buyer_attempts"))
            + 0.75 * log1p(row.get("sum_prior_buyer_wins"))
            + 0.75 * log1p(row.get("max_creator_buyer_pair"))
        ),
        "trigger_is_creator": finite(row.get("trigger_is_creator")),
        "trigger_is_known_buyer": finite(row.get("trigger_is_known_buyer")),
        "trigger_buyer_attempts_log": log1p(row.get("trigger_buyer_attempts")),
        "trigger_sol_log": log1p(row.get("trigger_sol")),
        "trigger_same_create_slot": finite(row.get("trigger_same_create_slot")),
        "trigger_same_create_signature": finite(row.get("trigger_same_create_signature")),
        "fdv_core_band": float(3_000.0 <= fdv <= 8_500.0),
        "very_early_50ms": float(age_ms <= 50.0),
        "very_early_150ms": float(age_ms <= 150.0),
        "very_early_400ms": float(age_ms <= 400.0),
        "seed_roundness": math.exp(-4.0 * common_seed_distance),
    }
    for feature in FEATURES:
        values.setdefault(feature, finite(row.get(feature)))
    return values


def add_relative_features(current: dict[str, Any], active: Sequence[dict[str, Any]]) -> None:
    current_values = feature_values(current)
    rows = [(row, feature_values(row)) for row in active]
    if not rows:
        rows = [(current, current_values)]

    def rank(name: str) -> tuple[int, float, float]:
        target = current_values[name]
        competitors = [values[name] for row, values in rows if row.get("mint") != current.get("mint")]
        rank_value = 1 + sum(value > target for value in competitors)
        best_other = max(competitors, default=0.0)
        return rank_value, target - best_other, best_other

    seed_rank, seed_gap, best_seed = rank("log_seed")
    identity_rank, identity_gap, best_identity = rank("identity_strength")
    velocity_rank, velocity_gap, best_velocity = rank("launch_velocity")
    buyer_target = finite(current.get("unique_buyers"))
    other_buyers = [finite(row.get("unique_buyers")) for row, _ in rows if row.get("mint") != current.get("mint")]
    buyer_rank = 1 + sum(value > buyer_target for value in other_buyers)
    current.update(
        {
            "active_count_log": log1p(len(rows)),
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
            "competitor_max_seed_log": best_seed,
            "competitor_max_identity": best_identity,
            "competitor_max_velocity_log": log1p(best_velocity),
        }
    )


def build_dataset(runs: Sequence[RunData], horizon_ms: float) -> list[dict[str, Any]]:
    memory = IntentMemory()
    dataset: list[dict[str, Any]] = []

    for run in runs:
        states = {mint: LaunchState(latest_ns=launch.create_ns) for mint, launch in run.launches.items()}
        events = sorted(
            ((economics.event_order(row), mint, row) for mint, launch in run.launches.items() for row in launch.events),
            key=lambda item: item[0],
        )
        markers = sorted(
            (
                integer(launch.e4_buy.get("received_ns")),
                mint,
                first_buyers_before_e4(launch),
            )
            for mint, launch in run.launches.items()
            if launch.e4_buy is not None
        )
        marker_pointer = 0
        applied_attempts: set[str] = set()
        recent: deque[str] = deque()

        for _, mint, event in events:
            now_ns = integer(event.get("received_ns"))
            while marker_pointer < len(markers) and markers[marker_pointer][0] < now_ns:
                _, marker_mint, buyers = markers[marker_pointer]
                if marker_mint not in applied_attempts:
                    memory.apply_attempt(run.launches[marker_mint], buyers)
                    applied_attempts.add(marker_mint)
                marker_pointer += 1

            launch = run.launches[mint]
            state = states[mint]
            if launch.e4_buy is not None and economics.event_order(event) >= economics.event_order(launch.e4_buy):
                continue
            if str(event.get("trader") or "") == E4_WALLET:
                continue
            apply_event(launch, state, event)
            kind = str(event.get("kind") or "").upper()
            if kind not in {"CREATE"} | BUY_KINDS:
                continue
            row = snapshot_base(launch, state, event, memory, horizon_ms)
            if row is None or row.get("too_early_for_positive"):
                continue

            recent.append(mint)
            while recent:
                oldest = run.launches.get(recent[0])
                if oldest is None or now_ns - oldest.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            active: list[dict[str, Any]] = []
            seen: set[str] = set()
            for active_mint in reversed(recent):
                if active_mint in seen:
                    continue
                seen.add(active_mint)
                active_launch = run.launches[active_mint]
                active_state = states[active_mint]
                pseudo = {
                    "received_ns": now_ns,
                    "slot": active_launch.create_slot,
                    "signature": "",
                    "trader": "",
                    "sol_amount": 0.0,
                }
                active_row = snapshot_base(active_launch, active_state, pseudo, memory, horizon_ms)
                if active_row is not None:
                    active.append(active_row)
            add_relative_features(row, active)
            dataset.append(row)

        while marker_pointer < len(markers):
            _, marker_mint, buyers = markers[marker_pointer]
            if marker_mint not in applied_attempts:
                memory.apply_attempt(run.launches[marker_mint], buyers)
                applied_attempts.add(marker_mint)
            marker_pointer += 1
        for launch in run.launches.values():
            if launch.e4_buy is not None:
                memory.apply_outcome(launch, first_buyers_before_e4(launch))

        print(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "snapshots": sum(integer(row["run_index"]) == run.run_index for row in dataset),
                    "positive_snapshots": sum(
                        integer(row["run_index"]) == run.run_index and bool(row["positive"])
                        for row in dataset
                    ),
                }
            ),
            flush=True,
        )
    return dataset


def matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([[feature_values(row)[name] for name in FEATURES] for row in rows], dtype=float)


def training_rows(rows: Sequence[dict[str, Any]], negative_ratio: int = 25) -> list[dict[str, Any]]:
    positives = [row for row in rows if row.get("positive")]
    negatives = [row for row in rows if not row.get("positive")]
    negatives.sort(
        key=lambda row: (
            feature_values(row)["identity_strength"]
            + feature_values(row)["log_seed"]
            + 0.5 * feature_values(row)["launch_velocity"]
            + feature_values(row)["same_slot_buys"]
        ),
        reverse=True,
    )
    maximum = max(3_000, len(positives) * negative_ratio)
    return positives + negatives[:maximum]


def model_specs() -> list[ModelSpec]:
    return [
        ModelSpec("logit", 0, 0, 0),
        ModelSpec("extra", 5, 3, 180),
        ModelSpec("extra", 7, 4, 220),
        ModelSpec("extra", 9, 6, 260),
        ModelSpec("forest", 6, 4, 220),
        ModelSpec("forest", 8, 6, 260),
    ]


def fit_model(rows: Sequence[dict[str, Any]], spec: ModelSpec):
    chosen = training_rows(rows)
    x = matrix(chosen)
    y = np.asarray([int(bool(row.get("positive"))) for row in chosen], dtype=int)
    if len(set(y.tolist())) < 2:
        raise RuntimeError("training data does not contain both classes")
    if spec.family == "logit":
        model = Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        C=0.25,
                        class_weight="balanced",
                        max_iter=5_000,
                        solver="liblinear",
                        random_state=712,
                    ),
                ),
            ]
        )
    elif spec.family == "extra":
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
    else:
        raise ValueError(spec.family)
    model.fit(x, y)
    return model


def predict_probability(model: Any, rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    if not rows:
        return np.asarray([], dtype=float)
    return model.predict_proba(matrix(rows))[:, 1]


def select_predictions(rows: Sequence[dict[str, Any]], model: Any, gate: Gate) -> list[dict[str, Any]]:
    scored = [dict(row) for row in rows]
    probabilities = predict_probability(model, scored)
    for row, probability in zip(scored, probabilities):
        row["score"] = float(probability)
    scored.sort(key=lambda row: (integer(row.get("decision_ns")), str(row.get("mint") or "")))
    recent: deque[dict[str, Any]] = deque()
    chosen: list[dict[str, Any]] = []
    chosen_mints: set[str] = set()
    last_global_ns = -10**30
    per_run: Counter[str] = Counter()

    for row in scored:
        mint = str(row.get("mint") or "")
        if not mint or mint in chosen_mints:
            continue
        now_ns = integer(row.get("decision_ns"))
        while recent and integer(recent[0].get("decision_ns")) < now_ns - 250_000_000:
            recent.popleft()
        recent.append(row)
        probability = finite(row.get("score"))
        if probability < gate.threshold:
            continue
        if gate.require_identity_top and not bool(row.get("current_is_identity_top")):
            continue
        if gate.require_seed_or_velocity_top and not (
            bool(row.get("current_is_seed_top")) or bool(row.get("current_is_velocity_top"))
        ):
            continue
        if gate.require_prior_identity and not (
            integer(row.get("prior_creator_attempts")) > 0
            or integer(row.get("known_buyer_count")) > 0
            or integer(row.get("max_creator_buyer_pair")) > 0
        ):
            continue
        best_other = max(
            (
                finite(item.get("score"))
                for item in recent
                if str(item.get("mint") or "") != mint
            ),
            default=0.0,
        )
        margin = probability - best_other
        if margin < gate.minimum_margin:
            continue
        if now_ns - last_global_ns < int(gate.cooldown_ms * 1_000_000):
            continue
        run_id = str(row.get("run_id") or "")
        if per_run[run_id] >= gate.maximum_predictions_per_run:
            continue
        chosen_mints.add(mint)
        per_run[run_id] += 1
        last_global_ns = now_ns
        chosen.append(
            {
                "mint": mint,
                "run_id": run_id,
                "run_index": integer(row.get("run_index")),
                "decision_ns": now_ns,
                "requested_fraction": 0.0185,
                "score": probability,
                "margin": margin,
                "mode": "v12_golden_preimpact",
                "positive": bool(row.get("positive")),
                "e4_won": bool(row.get("e4_won")),
                "e4_buy_ns": row.get("e4_buy_ns"),
                "source_lead_ms": row.get("source_lead_ms"),
                "e4_pnl_sol": row.get("e4_pnl_sol"),
                "feature_values": {name: feature_values(row)[name] for name in FEATURES},
            }
        )
    return chosen


def positions_metrics(positions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(positions)
    wins = sum(finite(row.get("pnl_sol")) > 0 for row in rows)
    gains = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) > 0)
    losses = sum(finite(row.get("pnl_sol")) for row in rows if finite(row.get("pnl_sol")) < 0)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": wins / len(rows) if rows else 0.0,
        "wilson_low": wilson_lower(wins, len(rows)),
        "net_pnl_sol": sum(finite(row.get("pnl_sol")) for row in rows),
        "profit_factor": gains / abs(losses) if losses < 0 else (999.0 if gains > 0 else 0.0),
    }


def aggregate_economics(
    runs: Sequence[RunData],
    predictions: Sequence[Mapping[str, Any]],
    latencies: Sequence[float],
    *,
    starting_balance_sol: float,
    max_output_shortfall_bps: int,
) -> dict[str, Any]:
    by_run: defaultdict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in predictions:
        by_run[integer(row.get("run_index"))].append(row)
    output: dict[str, Any] = {}
    for latency in latencies:
        all_positions: list[dict[str, Any]] = []
        rejected: Counter[str] = Counter()
        ending_balances: list[float] = []
        for run in runs:
            rows = by_run.get(run.run_index, [])
            converted = [
                economics.Prediction(
                    mint=str(row.get("mint") or ""),
                    decision_ns=integer(row.get("decision_ns")),
                    requested_fraction=finite(row.get("requested_fraction"), 0.0185),
                    score=finite(row.get("score"), 0.96),
                    mode=str(row.get("mode") or "v12_golden_preimpact"),
                    metadata=dict(row),
                )
                for row in rows
            ]
            e4_positions = economics.same_window_e4_positions(run.batch)
            result = economics.replay_latency(
                converted,
                run.grouped,
                e4_positions,
                starting_balance_sol=starting_balance_sol,
                latency_ms=latency,
                entry_fraction_default=0.0185,
                reserve_sol=0.03,
                fee_bps=125,
                max_output_shortfall_bps=max_output_shortfall_bps,
                confirmation_ms=1500.0,
                max_concurrent=2,
            )
            all_positions.extend(result["all_predictions"]["positions"])
            rejected.update(result.get("rejected") or {})
            ending_balances.append(finite(result.get("ending_balance_sol"), starting_balance_sol))
        output[str(int(latency) if float(latency).is_integer() else latency)] = {
            **positions_metrics(all_positions),
            "positions": all_positions,
            "rejected": dict(sorted(rejected.items())),
            "mean_ending_balance_sol": statistics.fmean(ending_balances) if ending_balances else starting_balance_sol,
        }
    return output


def passes_economics(metrics: Mapping[str, Any], minimum_trades: int, minimum_wr: float, minimum_pf: float) -> bool:
    return bool(
        integer(metrics.get("trades")) >= minimum_trades
        and finite(metrics.get("win_rate")) >= minimum_wr
        and finite(metrics.get("wilson_low")) >= 0.30
        and finite(metrics.get("net_pnl_sol")) > 0
        and finite(metrics.get("profit_factor")) >= minimum_pf
    )


def candidate_gates(probabilities: np.ndarray) -> Iterable[Gate]:
    quantiles = (0.90, 0.94, 0.96, 0.975, 0.985, 0.99, 0.995, 0.9975, 0.999)
    thresholds = sorted(
        set(float(np.quantile(probabilities, quantile)) for quantile in quantiles)
    ) if len(probabilities) else [1.0]
    for threshold in thresholds:
        for margin in (0.0, 0.025, 0.05, 0.10, 0.15):
            for cooldown in (0.0, 50.0, 100.0, 250.0, 500.0):
                for identity_top, seed_velocity_top, prior_identity in (
                    (False, False, False),
                    (True, False, False),
                    (False, True, False),
                    (True, True, False),
                    (False, False, True),
                    (True, False, True),
                    (False, True, True),
                    (True, True, True),
                ):
                    yield Gate(
                        threshold=threshold,
                        minimum_margin=margin,
                        cooldown_ms=cooldown,
                        require_identity_top=identity_top,
                        require_seed_or_velocity_top=seed_velocity_top,
                        require_prior_identity=prior_identity,
                        maximum_predictions_per_run=20,
                    )


def tune(
    train_rows: Sequence[dict[str, Any]],
    validation_rows: Sequence[dict[str, Any]],
    validation_runs: Sequence[RunData],
    *,
    latencies: Sequence[float],
    starting_balance_sol: float,
    max_output_shortfall_bps: int,
    minimum_wr: float,
    minimum_pf: float,
) -> tuple[ModelSpec, Any, Gate, dict[str, Any], list[dict[str, Any]]] | None:
    best: tuple[Any, ...] | None = None
    diagnostics: list[dict[str, Any]] = []
    for spec in model_specs():
        model = fit_model(train_rows, spec)
        probabilities = predict_probability(model, validation_rows)
        spec_best: dict[str, Any] | None = None
        for gate in candidate_gates(probabilities):
            predictions = select_predictions(validation_rows, model, gate)
            if not 6 <= len(predictions) <= 60:
                continue
            target_true = sum(bool(row.get("positive")) for row in predictions)
            selection_precision = target_true / len(predictions)
            if selection_precision < 0.20:
                continue
            economics_by_latency = aggregate_economics(
                validation_runs,
                predictions,
                latencies,
                starting_balance_sol=starting_balance_sol,
                max_output_shortfall_bps=max_output_shortfall_bps,
            )
            if not all(
                passes_economics(block, 6, minimum_wr, minimum_pf)
                for block in economics_by_latency.values()
            ):
                continue
            worst_wr = min(finite(block.get("win_rate")) for block in economics_by_latency.values())
            worst_pf = min(finite(block.get("profit_factor")) for block in economics_by_latency.values())
            worst_wilson = min(finite(block.get("wilson_low")) for block in economics_by_latency.values())
            total_pnl = sum(finite(block.get("net_pnl_sol")) for block in economics_by_latency.values())
            objective = (
                worst_wr,
                worst_wilson,
                worst_pf,
                selection_precision,
                total_pnl,
                target_true,
                -len(predictions),
            )
            candidate = (
                objective,
                spec,
                model,
                gate,
                {
                    "selection_predictions": len(predictions),
                    "selection_true": target_true,
                    "selection_precision": selection_precision,
                    "economics": economics_by_latency,
                },
                predictions,
            )
            if best is None or objective > best[0]:
                best = candidate
            if spec_best is None or objective > tuple(spec_best["objective"]):
                spec_best = {"objective": list(objective), "gate": gate.as_dict()}
        diagnostics.append({"spec": spec.as_dict(), "best": spec_best})
        print(json.dumps(diagnostics[-1], sort_keys=True), flush=True)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4], best[5]


def feature_importance(model: Any, limit: int = 20) -> list[dict[str, Any]]:
    if hasattr(model, "feature_importances_"):
        values = model.feature_importances_
    elif isinstance(model, Pipeline):
        values = np.abs(model.named_steps["model"].coef_[0])
    else:
        return []
    pairs = sorted(zip(FEATURES, values), key=lambda pair: float(pair[1]), reverse=True)
    return [{"feature": name, "importance": float(value)} for name, value in pairs[:limit]]


def search_mode(args: argparse.Namespace) -> int:
    pairs = [parse_pair(value) for value in args.pair]
    if len(pairs) < 8:
        raise SystemExit("at least eight chronological live-capture pairs are required")
    runs = load_runs(pairs)
    dataset = build_dataset(runs, args.horizon_ms)
    n = len(runs)
    holdout_start = n - 2
    validation_start = max(4, holdout_start - 2)
    train_rows = [row for row in dataset if integer(row.get("run_index")) < validation_start]
    validation_rows = [
        row
        for row in dataset
        if validation_start <= integer(row.get("run_index")) < holdout_start
    ]
    holdout_rows = [row for row in dataset if integer(row.get("run_index")) >= holdout_start]
    validation_runs = runs[validation_start:holdout_start]
    holdout_runs = runs[holdout_start:]
    latencies = economics.parse_latencies(args.latencies)

    tuned = tune(
        train_rows,
        validation_rows,
        validation_runs,
        latencies=latencies,
        starting_balance_sol=args.starting_balance_sol,
        max_output_shortfall_bps=args.max_output_shortfall_bps,
        minimum_wr=args.minimum_win_rate,
        minimum_pf=args.minimum_profit_factor,
    )
    if tuned is None:
        report = {
            "version": "e4-v12-golden-thesis-search-v1",
            "status": "NOT_CONCLUSIVE",
            "reason": "no causal rule passed validation economics at every requested latency",
            "coverage": {
                "runs": [run.run_id for run in runs],
                "snapshots": len(dataset),
                "positive_snapshots": sum(bool(row.get("positive")) for row in dataset),
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    spec, model, gate, validation_result, validation_predictions = tuned
    holdout_predictions = select_predictions(holdout_rows, model, gate)
    holdout_economics = aggregate_economics(
        holdout_runs,
        holdout_predictions,
        latencies,
        starting_balance_sol=args.starting_balance_sol,
        max_output_shortfall_bps=args.max_output_shortfall_bps,
    )
    holdout_true = sum(bool(row.get("positive")) for row in holdout_predictions)
    holdout_precision = holdout_true / len(holdout_predictions) if holdout_predictions else 0.0
    holdout_pass = bool(
        len(holdout_predictions) >= 4
        and holdout_precision >= 0.25
        and all(
            passes_economics(block, 4, args.minimum_win_rate, args.minimum_profit_factor)
            for block in holdout_economics.values()
        )
    )
    status = "HISTORICAL_HOLDOUT_CONFIRMED" if holdout_pass else "NOT_CONCLUSIVE"
    report = {
        "version": "e4-v12-golden-thesis-search-v1",
        "status": status,
        "thesis": (
            "Enter only when a causal creator/first-buyer identity cluster and the current first-slot "
            "launch geometry jointly dominate simultaneous launches; sign a tightly protected "
            "BuyExactSolIn-style order and allow late quotes to fail."
        ),
        "spec": spec.as_dict(),
        "gate": gate.as_dict(),
        "features": FEATURES,
        "horizon_ms": args.horizon_ms,
        "latencies_ms": latencies,
        "starting_balance_sol": args.starting_balance_sol,
        "max_output_shortfall_bps": args.max_output_shortfall_bps,
        "minimum_win_rate": args.minimum_win_rate,
        "minimum_profit_factor": args.minimum_profit_factor,
        "train_runs": [run.run_id for run in runs[:validation_start]],
        "validation_runs": [run.run_id for run in validation_runs],
        "holdout_runs": [run.run_id for run in holdout_runs],
        "validation": validation_result,
        "holdout": {
            "predictions": len(holdout_predictions),
            "target_true": holdout_true,
            "target_precision": holdout_precision,
            "economics": holdout_economics,
        },
        "top_drivers": feature_importance(model),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    args.predictions_output.write_text(
        json.dumps({"predictions": holdout_predictions}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    joblib.dump(
        {
            "version": "e4-v12-golden-thesis-model-v1",
            "model": model,
            "spec": spec.as_dict(),
            "gate": gate.as_dict(),
            "features": FEATURES,
            "horizon_ms": args.horizon_ms,
            "history_run_ids": [run.run_id for run in runs],
        },
        args.model_output,
    )
    print(json.dumps({"status": status, "holdout": report["holdout"]}, indent=2, sort_keys=True))
    return 0 if holdout_pass else 3


def apply_mode(args: argparse.Namespace) -> int:
    bundle = joblib.load(args.model_input)
    model = bundle["model"]
    gate = Gate(**bundle["gate"])
    pairs = [parse_pair(value) for value in args.pair]
    runs = load_runs(pairs)
    dataset = build_dataset(runs, finite(bundle.get("horizon_ms"), args.horizon_ms))
    live_index = len(runs) - 1
    live_rows = [row for row in dataset if integer(row.get("run_index")) == live_index]
    predictions = select_predictions(live_rows, model, gate)
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    args.predictions_output.write_text(
        json.dumps(
            {
                "version": "e4-v12-golden-live-predictions-v1",
                "model_version": bundle.get("version"),
                "live_run_id": runs[-1].run_id,
                "predictions": predictions,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"live_run_id": runs[-1].run_id, "predictions": len(predictions)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search and freeze a causal, profitable V12 entry thesis")
    parser.add_argument("--mode", choices=("search", "apply"), default="search")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("artifacts/e4-v12-golden-thesis.json"))
    parser.add_argument("--model-output", type=Path, default=Path("artifacts/e4-v12-golden-thesis.joblib"))
    parser.add_argument("--model-input", type=Path)
    parser.add_argument("--predictions-output", type=Path, default=Path("artifacts/e4-v12-golden-predictions.json"))
    parser.add_argument("--horizon-ms", type=float, default=750.0)
    parser.add_argument("--latencies", default="0,1,2,5,10")
    parser.add_argument("--starting-balance-sol", type=float, default=3.0)
    parser.add_argument("--max-output-shortfall-bps", type=int, default=800)
    parser.add_argument("--minimum-win-rate", type=float, default=0.65)
    parser.add_argument("--minimum-profit-factor", type=float, default=1.25)
    args = parser.parse_args()
    if args.mode == "apply":
        if args.model_input is None:
            parser.error("--model-input is required in apply mode")
        return apply_mode(args)
    return search_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())
