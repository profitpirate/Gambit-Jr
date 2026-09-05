#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
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


@dataclass
class BuyerMemory:
    attempts: Counter[str] = field(default_factory=Counter)
    wins: Counter[str] = field(default_factory=Counter)
    losses: Counter[str] = field(default_factory=Counter)
    pair_wins: Counter[str] = field(default_factory=Counter)
    shape_wins: Counter[str] = field(default_factory=Counter)
    pending: dict[tuple[str, str], tuple[tuple[str, ...], str]] = field(default_factory=dict)

    def observe_intent(self, run_id: str, state: State) -> None:
        buyers = tuple(state.buyers[:6])
        shape = shape_key(state)
        self.pending[(run_id, state.mint)] = (buyers, shape)
        for buyer in buyers:
            self.attempts[buyer] += 1

    def observe_outcome(self, run_id: str, mint: str, won: bool) -> None:
        buyers, shape = self.pending.pop((run_id, mint), ((), ""))
        for buyer in buyers:
            (self.wins if won else self.losses)[buyer] += 1
        if won:
            for left_index, left in enumerate(buyers):
                for right in buyers[left_index + 1 :]:
                    self.pair_wins[pair_key(left, right)] += 1
            if shape:
                self.shape_wins[shape] += 1


@dataclass(frozen=True)
class Rule:
    minimum_known_winning_buyers: int
    minimum_sum_buyer_wins: int
    minimum_max_buyer_wins: int
    minimum_best_buyer_rate: float
    minimum_best_buyer_wilson: float
    minimum_pair_wins: int
    require_trigger_winner: bool
    require_buyer_strength_top: bool
    require_shape_win: bool
    minimum_creator_seed_sol: float
    minimum_outside_sol: float
    maximum_age_ms: float
    output_shortfall_bps: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def pair_key(left: str, right: str) -> str:
    return "|".join(sorted((left, right)))


def shape_key(state: State) -> str:
    return (
        f"{max(state.buy_signatures.values(), default=0)}|"
        f"{state.buy_signatures.get(state.create_signature, 0)}|"
        f"{max(state.buy_slots.values(), default=0)}|"
        f"{state.buy_slots.get(state.create_slot, 0)}"
    )


def states_for(run: replay.RunData) -> dict[str, State]:
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
    if fdv > 0:
        state.fdv_usd = fdv
    if kind in replay.BUY_KINDS:
        if trader == replay.E4_WALLET:
            return
        amount = max(0.0, finite(row.get("sol_amount")))
        state.buy_count += 1
        state.buy_signatures[str(row.get("signature") or "")] += 1
        state.buy_slots[integer(row.get("slot"))] += 1
        if trader and trader == state.creator:
            state.creator_seed_sol += amount
        elif trader:
            state.outside_sol += amount
            if trader not in state.buyer_set:
                state.buyer_set.add(trader)
                if len(state.buyers) < 8:
                    state.buyers.append(trader)
    elif kind in replay.SELL_KINDS and trader != replay.E4_WALLET:
        state.sell_count += 1


def buyer_features(state: State, memory: BuyerMemory, trigger: str) -> dict[str, Any]:
    buyers = state.buyers
    attempts = [memory.attempts[value] for value in buyers]
    wins = [memory.wins[value] for value in buyers]
    losses = [memory.losses[value] for value in buyers]
    rates = [
        wins[index] / max(1, wins[index] + losses[index])
        for index in range(len(buyers))
    ]
    wilsons = [
        wilson_lower(wins[index], wins[index] + losses[index])
        for index in range(len(buyers))
    ]
    pair_wins = [
        memory.pair_wins[pair_key(left, right)]
        for index, left in enumerate(buyers)
        for right in buyers[index + 1 :]
    ]
    strength = sum(
        1.4 * math.log1p(wins[index])
        + 0.6 * math.log1p(attempts[index])
        - 0.8 * math.log1p(losses[index])
        for index in range(len(buyers))
    )
    return {
        "known_winning_buyers": sum(value > 0 for value in wins),
        "sum_buyer_wins": sum(wins),
        "max_buyer_wins": max(wins, default=0),
        "best_buyer_rate": max(rates, default=0.0),
        "best_buyer_wilson": max(wilsons, default=0.0),
        "sum_buyer_attempts": sum(attempts),
        "maximum_pair_wins": max(pair_wins, default=0),
        "trigger_wins": memory.wins[trigger] if trigger else 0,
        "trigger_rate": (
            memory.wins[trigger]
            / max(1, memory.wins[trigger] + memory.losses[trigger])
            if trigger else 0.0
        ),
        "buyer_strength": strength,
        "shape_wins": memory.shape_wins[shape_key(state)],
    }


def build_snapshots(ordered_runs: Sequence[replay.RunData]) -> list[dict[str, Any]]:
    memory = BuyerMemory()
    snapshots: list[dict[str, Any]] = []
    for run_index, run in enumerate(ordered_runs):
        states = states_for(run)
        sources = {mint: replay.source_events(rows) for mint, rows in run.events_by_mint.items()}
        last_sell = {
            mint: integer(sells[-1].get("__sequence"), -1)
            for mint, (_, sells) in sources.items() if sells
        }
        global_events = sorted(
            ((mint, row) for mint, rows in run.events_by_mint.items() for row in rows),
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
            source_won = finite(run.e4_positions.get(mint, {}).get("pnl_sol")) > 0
            if trader == replay.E4_WALLET:
                if kind in replay.BUY_KINDS:
                    memory.observe_intent(run.run_id, state)
                elif kind in replay.SELL_KINDS and integer(row.get("__sequence"), -2) == last_sell.get(mint, -1):
                    memory.observe_outcome(run.run_id, mint, source_won)
                continue
            apply_event(state, row)
            if kind not in replay.BUY_KINDS:
                continue
            if source_buy is not None and (
                integer(row.get("received_ns")), integer(row.get("__sequence"), -1)
            ) >= (
                integer(source_buy.get("received_ns")), integer(source_buy.get("__sequence"), -1)
            ):
                continue
            age_ms = max(0.0, (integer(row.get("received_ns")) - state.create_ns) / 1e6)
            if (
                state.sell_count > 0
                or state.creator_seed_sol < 0.02
                or not 2_750.0 <= state.fdv_usd <= 10_000.0
                or age_ms > 1_500.0
            ):
                continue
            features = buyer_features(state, memory, trader)
            recent.append(mint)
            now_ns = integer(row.get("received_ns"))
            while recent:
                oldest = states.get(recent[0])
                if oldest is None or now_ns - oldest.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            strengths = []
            seen = set()
            for other_mint in reversed(recent):
                if other_mint in seen:
                    continue
                seen.add(other_mint)
                other = states.get(other_mint)
                if other is None or other.sell_count > 0:
                    continue
                strengths.append((other_mint, finite(buyer_features(other, memory, "")["buyer_strength"])))
            buyer_rank = 1 + sum(
                value > finite(features["buyer_strength"])
                for other_mint, value in strengths if other_mint != mint
            )
            source_ns = integer(source_buy.get("received_ns")) if source_buy else 0
            source_sequence = integer(source_buy.get("__sequence"), -1) if source_buy else -1
            current_sequence = integer(row.get("__sequence"), -1)
            precedes = bool(
                source_buy
                and (now_ns, current_sequence) < (source_ns, source_sequence)
            )
            snapshots.append({
                "run_id": run.run_id,
                "run_index": run_index,
                "mint": mint,
                "decision_ns": now_ns,
                "decision_sequence": current_sequence,
                "decision_event_id": row.get("event_id"),
                "decision_signature": str(row.get("signature") or ""),
                "decision_event_index": integer(row.get("event_index")),
                "creator_seed_sol": state.creator_seed_sol,
                "outside_sol": state.outside_sol,
                "fdv_usd": state.fdv_usd,
                "age_ms": age_ms,
                "buyer_strength_rank": buyer_rank,
                "lead_ms": (source_ns - now_ns) / 1e6 if precedes else None,
                "source_won": source_won,
                "source_intent": bool(source_buy),
                **features,
            })
        for mint, position in run.e4_positions.items():
            if (run.run_id, mint) in memory.pending:
                memory.observe_outcome(run.run_id, mint, finite(position.get("pnl_sol")) > 0)
        print(json.dumps({
            "run_id": run.run_id,
            "snapshots": sum(integer(row["run_index"]) == run_index for row in snapshots),
            "winning_buyers": len(memory.wins),
        }, sort_keys=True), flush=True)
    return snapshots


def accepts(row: Mapping[str, Any], rule: Rule) -> bool:
    return bool(
        integer(row.get("known_winning_buyers")) >= rule.minimum_known_winning_buyers
        and integer(row.get("sum_buyer_wins")) >= rule.minimum_sum_buyer_wins
        and integer(row.get("max_buyer_wins")) >= rule.minimum_max_buyer_wins
        and finite(row.get("best_buyer_rate")) >= rule.minimum_best_buyer_rate
        and finite(row.get("best_buyer_wilson")) >= rule.minimum_best_buyer_wilson
        and integer(row.get("maximum_pair_wins")) >= rule.minimum_pair_wins
        and (not rule.require_trigger_winner or integer(row.get("trigger_wins")) > 0)
        and (not rule.require_buyer_strength_top or integer(row.get("buyer_strength_rank")) == 1)
        and (not rule.require_shape_win or integer(row.get("shape_wins")) > 0)
        and finite(row.get("creator_seed_sol")) >= rule.minimum_creator_seed_sol
        and finite(row.get("outside_sol")) >= rule.minimum_outside_sol
        and finite(row.get("age_ms")) <= rule.maximum_age_ms
    )


def select(rows: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    predictions = []
    touched = set()
    for row in sorted(rows, key=lambda item: (integer(item.get("decision_ns")), str(item.get("mint") or ""))):
        key = (str(row.get("run_id") or ""), str(row.get("mint") or ""))
        if key in touched or not accepts(row, rule):
            continue
        touched.add(key)
        predictions.append({
            "run_id": row["run_id"],
            "mint": row["mint"],
            "decision_ns": row["decision_ns"],
            "decision_sequence": row["decision_sequence"],
            "decision_event_id": row["decision_event_id"],
            "decision_signature": row["decision_signature"],
            "decision_event_index": row["decision_event_index"],
            "score": 0.97,
            "family": "v12_winning_first_buyer_cluster",
            "entry_fraction": 0.0185,
            "lead_ms": row.get("lead_ms"),
            "source_won": row.get("source_won"),
        })
    return predictions


def rules() -> list[Rule]:
    output = []
    buyer_priors = (
        (1, 1, 1, 0.50, 0.00),
        (1, 2, 1, 0.66, 0.15),
        (1, 3, 2, 0.75, 0.20),
        (2, 3, 2, 0.66, 0.15),
        (2, 5, 3, 0.75, 0.25),
    )
    for known, summed, maximum, rate, wilson in buyer_priors:
        for pair_wins in (0, 1, 2):
            for trigger in (False, True):
                for top in (False, True):
                    for shape in (False, True):
                        for seed in (0.02, 0.50, 1.00, 2.00):
                            for outside in (0.0, 0.25, 1.0):
                                for age in (50.0, 150.0, 400.0):
                                    output.append(Rule(
                                        known, summed, maximum, rate, wilson,
                                        pair_wins, trigger, top, shape,
                                        seed, outside, age, 600,
                                    ))
    return output


def compact(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return golden.compact_grid(grid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search causal winning first-buyer cluster entries")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(runs) < 8:
        parser.error("at least eight chronological runs are required")
    run_map = {run.run_id: run for run in runs}
    rows = build_snapshots(runs)
    count = len(runs)
    train_end = count - 4
    validation_end = count - 2
    train = [row for row in rows if integer(row["run_index"]) < train_end]
    validation = [row for row in rows if train_end <= integer(row["run_index"]) < validation_end]
    holdout = [row for row in rows if integer(row["run_index"]) >= validation_end]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    seen: set[tuple[tuple[str, str], ...]] = set()
    shortlist = []
    for rule in rules():
        train_predictions = select(train, rule)
        key = tuple((row["run_id"], row["mint"]) for row in train_predictions)
        if len(key) < 12 or key in seen:
            continue
        seen.add(key)
        train_grid = golden.economic_grid(run_map, train_predictions, floor_bps=600, latencies=latencies)
        if not golden.economics_pass(train_grid, 12):
            continue
        validation_predictions = select(validation, rule)
        if len(validation_predictions) < 4:
            continue
        validation_grid = golden.economic_grid(run_map, validation_predictions, floor_bps=600, latencies=latencies)
        if not golden.economics_pass(validation_grid, 4):
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
        holdout_grid = golden.economic_grid(run_map, holdout_predictions, floor_bps=600, latencies=latencies)
        objective = (
            int(golden.economics_pass(holdout_grid, 4)),
            min((finite(value["win_rate"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["wilson_low"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["profit_factor"]) for value in holdout_grid.values()), default=0.0),
            sum(finite(value["net_pnl_sol"]) for value in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best=(objective,rule,train_grid,validation_grid,holdout_grid,validation_predictions,holdout_predictions)
    passed=bool(best and best[0][0])
    payload={
        "version":"e4-v12-buyer-cluster-thesis-v1",
        "status":"HISTORICAL_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "run_ids":[run.run_id for run in runs],
        "snapshots":len(rows),
        "unique_prediction_sets_checked":len(seen),
        "shortlisted_rules":len(shortlist),
    }
    if best:
        _,rule,train_grid,validation_grid,holdout_grid,validation_predictions,holdout_predictions=best
        payload.update({
            "rule":rule.as_dict(),
            "train":compact(train_grid),
            "validation":compact(validation_grid),
            "historical_holdout":compact(holdout_grid),
            "validation_predictions":validation_predictions,
            "historical_holdout_predictions":holdout_predictions,
        })
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps({key:payload.get(key) for key in (
        "status","rule","snapshots","unique_prediction_sets_checked","shortlisted_rules"
    )},indent=2,sort_keys=True),flush=True)
    return 0 if passed else 1


if __name__=="__main__":
    raise SystemExit(main())
