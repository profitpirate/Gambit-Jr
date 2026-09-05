#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import e4_v12_golden_thesis_search_v2 as golden
import e4_v12_true_latency_replay as replay


def finite(value: Any, default: float = 0.0) -> float:
    return replay.finite(value, default)


def integer(value: Any, default: int = 0) -> int:
    return replay.integer(value, default)


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = p + z * z / (2.0 * total)
    spread = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return max(0.0, (centre - spread) / denominator)


@dataclass
class Pattern:
    seed_sol: float
    fdv_usd: float
    age_ms: float
    first_buyers: tuple[str, ...]
    signature_shape: tuple[int, int, int]
    same_slot_buys: int
    unique_buyers: int
    won: bool


@dataclass
class CreatorMemory:
    wins: int = 0
    losses: int = 0
    patterns: list[Pattern] = field(default_factory=list)
    winning_buyers: Counter[str] = field(default_factory=Counter)
    winning_prefixes: Counter[tuple[str, ...]] = field(default_factory=Counter)
    winning_shapes: Counter[tuple[int, int, int]] = field(default_factory=Counter)

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def winning_patterns(self) -> list[Pattern]:
        return [row for row in self.patterns if row.won]

    def add(self, pattern: Pattern) -> None:
        self.patterns.append(pattern)
        if pattern.won:
            self.wins += 1
            for buyer in pattern.first_buyers:
                self.winning_buyers[buyer] += 1
            for width in range(1, min(3, len(pattern.first_buyers)) + 1):
                self.winning_prefixes[pattern.first_buyers[:width]] += 1
            self.winning_shapes[pattern.signature_shape] += 1
        else:
            self.losses += 1


@dataclass(frozen=True)
class Rule:
    minimum_creator_wins: int
    maximum_creator_losses: int
    minimum_creator_win_rate: float
    minimum_creator_wilson: float
    maximum_seed_relative_distance: float
    maximum_fdv_relative_distance: float
    minimum_winning_buyer_overlap: int
    minimum_winning_buyer_frequency: int
    minimum_prefix_match: int
    require_shape_match: bool
    require_seed_or_identity_top: bool
    minimum_creator_seed_sol: float
    maximum_age_ms: float
    output_shortfall_bps: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class State:
    mint: str
    creator: str
    create_ns: int
    create_slot: int
    create_signature: str
    creator_seed_sol: float = 0.0
    outside_sol: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    buyers: list[str] = field(default_factory=list)
    buyer_set: set[str] = field(default_factory=set)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    buy_slots: Counter[int] = field(default_factory=Counter)
    fdv_usd: float = 0.0
    first_price: float = 0.0
    price: float = 0.0


def state_from_run(run: replay.RunData) -> dict[str, State]:
    output = {}
    for mint, rows in run.events_by_mint.items():
        create = next((row for row in rows if str(row.get("kind") or "").upper() == "CREATE"), None)
        if create is None:
            continue
        raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
        output[mint] = State(
            mint=mint,
            creator=str(create.get("creator") or raw.get("creator") or create.get("trader") or ""),
            create_ns=integer(create.get("received_ns")),
            create_slot=integer(create.get("slot")),
            create_signature=str(create.get("signature") or ""),
        )
    return output


def apply_event(state: State, row: Mapping[str, Any]) -> None:
    kind = str(row.get("kind") or "").upper()
    trader = str(row.get("trader") or "")
    fdv = finite(row.get("fdv_usd"))
    price = finite(row.get("price_sol"))
    if fdv > 0:
        state.fdv_usd = fdv
    if price > 0:
        state.price = price
        if state.first_price <= 0:
            state.first_price = price
    if kind in replay.BUY_KINDS:
        if trader == replay.E4_WALLET:
            return
        sol = max(0.0, finite(row.get("sol_amount")))
        state.buy_count += 1
        state.buy_signatures[str(row.get("signature") or "")] += 1
        state.buy_slots[integer(row.get("slot"))] += 1
        if trader and trader == state.creator:
            state.creator_seed_sol += sol
        elif trader:
            state.outside_sol += sol
            if trader not in state.buyer_set:
                state.buyer_set.add(trader)
                if len(state.buyers) < 8:
                    state.buyers.append(trader)
    elif kind in replay.SELL_KINDS and trader != replay.E4_WALLET:
        state.sell_count += 1


def shape(state: State) -> tuple[int, int, int]:
    return (
        max(state.buy_signatures.values(), default=0),
        state.buy_signatures.get(state.create_signature, 0),
        max(state.buy_slots.values(), default=0),
    )


def pattern(state: State, timestamp_ns: int, won: bool) -> Pattern:
    return Pattern(
        seed_sol=state.creator_seed_sol,
        fdv_usd=state.fdv_usd,
        age_ms=max(0.0, (timestamp_ns - state.create_ns) / 1e6),
        first_buyers=tuple(state.buyers[:5]),
        signature_shape=shape(state),
        same_slot_buys=state.buy_slots.get(state.create_slot, 0),
        unique_buyers=len(state.buyer_set),
        won=won,
    )


def relative_distance(value: float, reference: float) -> float:
    return abs(value - reference) / max(0.05, abs(reference))


def recurrence_features(state: State, memory: CreatorMemory) -> dict[str, Any]:
    winners = memory.winning_patterns
    if not winners:
        return {
            "seed_distance": float("inf"),
            "fdv_distance": float("inf"),
            "buyer_overlap": 0,
            "buyer_frequency": 0,
            "prefix_match": 0,
            "shape_match": False,
            "identity_strength": 0.0,
        }
    seed_distance = min(relative_distance(state.creator_seed_sol, row.seed_sol) for row in winners)
    fdv_distance = min(relative_distance(state.fdv_usd, row.fdv_usd) for row in winners if row.fdv_usd > 0)
    overlap = sum(buyer in memory.winning_buyers for buyer in state.buyers)
    frequency = sum(memory.winning_buyers[buyer] for buyer in state.buyers)
    prefix = 0
    for width in range(1, min(3, len(state.buyers)) + 1):
        if memory.winning_prefixes[tuple(state.buyers[:width])] > 0:
            prefix = width
    shape_match = memory.winning_shapes[shape(state)] > 0
    identity_strength = (
        1.8 * math.log1p(memory.wins)
        - 1.0 * math.log1p(memory.losses)
        + 1.0 * math.log1p(frequency)
        + 0.8 * prefix
        + 0.5 * int(shape_match)
        - min(2.0, seed_distance)
        - 0.5 * min(2.0, fdv_distance)
    )
    return {
        "seed_distance": seed_distance,
        "fdv_distance": fdv_distance,
        "buyer_overlap": overlap,
        "buyer_frequency": frequency,
        "prefix_match": prefix,
        "shape_match": shape_match,
        "identity_strength": identity_strength,
    }


def build_snapshots(ordered_runs: Sequence[replay.RunData]) -> list[dict[str, Any]]:
    memories: defaultdict[str, CreatorMemory] = defaultdict(CreatorMemory)
    snapshots: list[dict[str, Any]] = []
    for run_index, run in enumerate(ordered_runs):
        states = state_from_run(run)
        sources = {mint: replay.source_events(rows) for mint, rows in run.events_by_mint.items()}
        last_source_sell = {
            mint: integer(sells[-1].get("__sequence"), -1)
            for mint, (_, sells) in sources.items() if sells
        }
        global_events = sorted(
            ((mint, row) for mint, rows in run.events_by_mint.items() for row in rows),
            key=lambda item: replay.event_sort_key(item[1]),
        )
        recent: deque[str] = deque()
        emitted: set[str] = set()
        pending_pattern: dict[str, Pattern] = {}
        for mint, row in global_events:
            state = states.get(mint)
            if state is None:
                continue
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            source_buy, source_sells = sources.get(mint, (None, []))
            source_position = run.e4_positions.get(mint, {})
            source_won = finite(source_position.get("pnl_sol")) > 0

            if trader == replay.E4_WALLET:
                if kind in replay.BUY_KINDS:
                    pending_pattern[mint] = pattern(state, integer(row.get("received_ns")), source_won)
                elif kind in replay.SELL_KINDS and integer(row.get("__sequence"), -2) == last_source_sell.get(mint, -1):
                    row_pattern = pending_pattern.pop(mint, None)
                    if row_pattern is not None and state.creator:
                        memories[state.creator].add(row_pattern)
                continue

            apply_event(state, row)
            if kind not in {"CREATE", *replay.BUY_KINDS} or mint in emitted:
                continue
            if source_buy is not None and (
                integer(row.get("received_ns")), integer(row.get("__sequence"), -1)
            ) >= (
                integer(source_buy.get("received_ns")), integer(source_buy.get("__sequence"), -1)
            ):
                continue
            age_ms = max(0.0, (integer(row.get("received_ns")) - state.create_ns) / 1e6)
            if (
                not state.creator
                or state.sell_count > 0
                or state.creator_seed_sol < 0.02
                or not 2_750.0 <= state.fdv_usd <= 10_000.0
                or age_ms > 1_500.0
            ):
                continue
            memory = memories[state.creator]
            recurrence = recurrence_features(state, memory)
            recent.append(mint)
            now_ns = integer(row.get("received_ns"))
            while recent:
                first = states.get(recent[0])
                if first is None or now_ns - first.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            active = []
            seen = set()
            for active_mint in reversed(recent):
                if active_mint in seen:
                    continue
                seen.add(active_mint)
                active_state = states.get(active_mint)
                if active_state is None or active_state.sell_count > 0:
                    continue
                active_recurrence = recurrence_features(active_state, memories[active_state.creator])
                active.append((active_state, active_recurrence))
            seed_rank = 1 + sum(other.creator_seed_sol > state.creator_seed_sol for other, _ in active if other.mint != mint)
            identity_rank = 1 + sum(
                finite(values["identity_strength"]) > finite(recurrence["identity_strength"])
                for other, values in active if other.mint != mint
            )
            source_ns = integer(source_buy.get("received_ns")) if source_buy else 0
            source_sequence = integer(source_buy.get("__sequence"), -1) if source_buy else -1
            current_sequence = integer(row.get("__sequence"), -1)
            precedes = bool(
                source_buy
                and (now_ns, current_sequence) < (source_ns, source_sequence)
            )
            lead_ms = (source_ns - now_ns) / 1e6 if precedes else None
            snapshots.append({
                "run_id": run.run_id,
                "run_index": run_index,
                "mint": mint,
                "creator": state.creator,
                "decision_ns": now_ns,
                "decision_sequence": current_sequence,
                "decision_event_id": row.get("event_id"),
                "decision_signature": str(row.get("signature") or ""),
                "decision_event_index": integer(row.get("event_index")),
                "creator_wins": memory.wins,
                "creator_losses": memory.losses,
                "creator_trades": memory.trades,
                "creator_win_rate": memory.win_rate,
                "creator_wilson": wilson_lower(memory.wins, memory.trades),
                "creator_seed_sol": state.creator_seed_sol,
                "fdv_usd": state.fdv_usd,
                "age_ms": age_ms,
                "seed_rank": seed_rank,
                "identity_rank": identity_rank,
                "source_won": source_won,
                "source_intent": bool(source_buy),
                "lead_ms": lead_ms,
                **recurrence,
            })
        for mint, row_pattern in pending_pattern.items():
            state = states.get(mint)
            if state is not None and state.creator:
                memories[state.creator].add(row_pattern)
        print(json.dumps({
            "run_id": run.run_id,
            "snapshots": sum(integer(row["run_index"]) == run_index for row in snapshots),
            "known_creators": len(memories),
        }, sort_keys=True), flush=True)
    return snapshots


def accepts(row: Mapping[str, Any], rule: Rule) -> bool:
    if integer(row.get("creator_wins")) < rule.minimum_creator_wins:
        return False
    if integer(row.get("creator_losses")) > rule.maximum_creator_losses:
        return False
    if finite(row.get("creator_win_rate")) < rule.minimum_creator_win_rate:
        return False
    if finite(row.get("creator_wilson")) < rule.minimum_creator_wilson:
        return False
    if finite(row.get("seed_distance"), float("inf")) > rule.maximum_seed_relative_distance:
        return False
    if finite(row.get("fdv_distance"), float("inf")) > rule.maximum_fdv_relative_distance:
        return False
    if integer(row.get("buyer_overlap")) < rule.minimum_winning_buyer_overlap:
        return False
    if integer(row.get("buyer_frequency")) < rule.minimum_winning_buyer_frequency:
        return False
    if integer(row.get("prefix_match")) < rule.minimum_prefix_match:
        return False
    if rule.require_shape_match and not bool(row.get("shape_match")):
        return False
    if rule.require_seed_or_identity_top and not (
        integer(row.get("seed_rank")) == 1 or integer(row.get("identity_rank")) == 1
    ):
        return False
    return bool(
        finite(row.get("creator_seed_sol")) >= rule.minimum_creator_seed_sol
        and finite(row.get("age_ms")) <= rule.maximum_age_ms
    )


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    output = []
    touched = set()
    for row in sorted(rows, key=lambda item: (integer(item.get("decision_ns")), str(item.get("mint") or ""))):
        key = (str(row.get("run_id") or ""), str(row.get("mint") or ""))
        if key in touched or not accepts(row, rule):
            continue
        touched.add(key)
        output.append({
            "run_id": row["run_id"],
            "mint": row["mint"],
            "decision_ns": row["decision_ns"],
            "decision_sequence": row["decision_sequence"],
            "decision_event_id": row["decision_event_id"],
            "decision_signature": row["decision_signature"],
            "decision_event_index": row["decision_event_index"],
            "score": 0.97,
            "family": "v12_creator_buyer_shape_recurrence",
            "entry_fraction": 0.0185,
            "lead_ms": row.get("lead_ms"),
            "source_won": row.get("source_won"),
        })
    return output


def grid(
    run_map: Mapping[str, replay.RunData],
    predictions: Sequence[Mapping[str, Any]],
    rule: Rule,
    latencies: Sequence[float],
) -> dict[str, Any]:
    return golden.economic_grid(
        run_map,
        predictions,
        floor_bps=rule.output_shortfall_bps,
        latencies=latencies,
    )


def passes(economics: Mapping[str, Mapping[str, Any]], minimum_trades: int) -> bool:
    return golden.economics_pass(economics, minimum_trades)


def compact(economics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return golden.compact_grid(economics)


def rules() -> list[Rule]:
    output = []
    for wins in (1, 2, 3, 5):
        for losses in (0, 1, 2):
            for rate in (0.60, 0.75, 0.85, 0.90):
                for wilson in (0.0, 0.20, 0.30, 0.40):
                    for seed_distance in (0.05, 0.10, 0.20, 0.40, 1.00):
                        for fdv_distance in (0.10, 0.20, 0.40, 1.00):
                            for overlap, frequency, prefix in (
                                (0, 0, 0),
                                (1, 1, 0),
                                (1, 2, 0),
                                (1, 1, 1),
                                (2, 2, 1),
                            ):
                                for shape_match in (False, True):
                                    for top in (False, True):
                                        for seed in (0.02, 0.25, 0.50, 1.0, 2.0):
                                            for age in (50.0, 150.0, 400.0):
                                                for floor in (200, 400, 600, 800, 1_000):
                                                    output.append(Rule(
                                                        wins, losses, rate, wilson,
                                                        seed_distance, fdv_distance,
                                                        overlap, frequency, prefix,
                                                        shape_match, top, seed, age, floor,
                                                    ))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Search exact winning creator-buyer launch recurrence")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ordered_runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(ordered_runs) < 8:
        parser.error("at least eight chronological runs are required")
    run_map = {run.run_id: run for run in ordered_runs}
    rows = build_snapshots(ordered_runs)
    count = len(ordered_runs)
    train_end = count - 4
    validation_end = count - 2
    train = [row for row in rows if integer(row["run_index"]) < train_end]
    validation = [row for row in rows if train_end <= integer(row["run_index"]) < validation_end]
    holdout = [row for row in rows if integer(row["run_index"]) >= validation_end]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    # Collapse millions of syntactic combinations to unique prediction sets,
    # then evaluate economics only once per set.
    seen_sets: set[tuple[tuple[str, str], ...]] = set()
    shortlist = []
    for rule in rules():
        train_predictions = select(train, rule)
        train_keys = tuple((row["run_id"], row["mint"]) for row in train_predictions)
        if len(train_keys) < 12 or train_keys in seen_sets:
            continue
        seen_sets.add(train_keys)
        train_grid = grid(run_map, train_predictions, rule, latencies)
        if not passes(train_grid, 12):
            continue
        validation_predictions = select(validation, rule)
        if len(validation_predictions) < 4:
            continue
        validation_grid = grid(run_map, validation_predictions, rule, latencies)
        if not passes(validation_grid, 4):
            continue
        score = (
            min(finite(value["win_rate"]) for value in validation_grid.values()),
            min(finite(value["wilson_low"]) for value in validation_grid.values()),
            min(finite(value["profit_factor"]) for value in validation_grid.values()),
            sum(finite(value["net_pnl_sol"]) for value in validation_grid.values()),
            -len(validation_predictions),
        )
        shortlist.append((score, rule, train_grid, validation_grid, validation_predictions))
    shortlist.sort(key=lambda item: item[0], reverse=True)

    best = None
    for score, rule, train_grid, validation_grid, validation_predictions in shortlist[:50]:
        holdout_predictions = select(holdout, rule)
        holdout_grid = grid(run_map, holdout_predictions, rule, latencies)
        objective = (
            int(passes(holdout_grid, 4)),
            min((finite(value["win_rate"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["wilson_low"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["profit_factor"]) for value in holdout_grid.values()), default=0.0),
            sum(finite(value["net_pnl_sol"]) for value in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best = (
                objective, rule, train_grid, validation_grid,
                holdout_grid, validation_predictions, holdout_predictions,
            )
    confirmed = bool(best and best[0][0])
    payload = {
        "version": "e4-v12-recurrence-shape-thesis-v1",
        "status": "HISTORICAL_GOLDEN_CONFIRMED" if confirmed else "NOT_CONCLUSIVE",
        "run_ids": [run.run_id for run in ordered_runs],
        "snapshots": len(rows),
        "unique_prediction_sets_checked": len(seen_sets),
        "shortlisted_rules": len(shortlist),
    }
    if best:
        _, rule, train_grid, validation_grid, holdout_grid, validation_predictions, holdout_predictions = best
        payload.update({
            "rule": rule.as_dict(),
            "train": compact(train_grid),
            "validation": compact(validation_grid),
            "historical_holdout": compact(holdout_grid),
            "validation_predictions": validation_predictions,
            "historical_holdout_predictions": holdout_predictions,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: payload.get(key) for key in (
        "status", "rule", "snapshots", "unique_prediction_sets_checked", "shortlisted_rules"
    )}, indent=2, sort_keys=True), flush=True)
    return 0 if confirmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
