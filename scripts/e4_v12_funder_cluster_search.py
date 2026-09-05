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
    buyers: set[str] = field(default_factory=set)
    buy_slots: Counter[int] = field(default_factory=Counter)
    buy_signatures: Counter[str] = field(default_factory=Counter)
    fdv_usd: float = 0.0


@dataclass(frozen=True)
class Rule:
    minimum_funder_wins: int
    maximum_funder_losses: int
    minimum_funder_rate: float
    minimum_funder_wilson: float
    minimum_funder_creator_count: int
    minimum_creator_seed_sol: float
    minimum_outside_sol: float
    minimum_unique_buyers: int
    minimum_same_slot_buys: int
    minimum_create_signature_buys: int
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    maximum_age_ms: float
    require_funder_strength_top: bool
    require_seed_top: bool
    output_shortfall_bps: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_relations(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    output = {}
    for creator, relation in (payload.get("creator_funders") or {}).items():
        if not isinstance(relation, Mapping):
            continue
        funder = str(relation.get("funder") or "")
        if not funder:
            continue
        output[str(creator)] = {
            "funder": funder,
            "observed_ns": integer(relation.get("observed_ns")),
        }
    return output


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
        state.buy_slots[integer(row.get("slot"))] += 1
        state.buy_signatures[str(row.get("signature") or "")] += 1
        if trader and trader == state.creator:
            state.creator_seed_sol += amount
        elif trader:
            state.outside_sol += amount
            state.buyers.add(trader)
    elif kind in replay.SELL_KINDS and trader != replay.E4_WALLET:
        state.sell_count += 1


def build_snapshots(
    runs: Sequence[replay.RunData],
    relations: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    funder_wins: Counter[str] = Counter()
    funder_losses: Counter[str] = Counter()
    funder_creators: defaultdict[str, set[str]] = defaultdict(set)
    pending: dict[tuple[str, str], tuple[str, str]] = {}
    snapshots: list[dict[str, Any]] = []

    for run_index, run in enumerate(runs):
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
            relation = relations.get(state.creator, {})
            funder = str(relation.get("funder") or "")
            relation_ns = integer(relation.get("observed_ns"))
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            source_buy, source_sells = sources.get(mint, (None, []))
            source_won = finite(run.e4_positions.get(mint, {}).get("pnl_sol")) > 0

            if trader == replay.E4_WALLET:
                if kind in replay.BUY_KINDS and funder and relation_ns and relation_ns < integer(row.get("received_ns")):
                    pending[(run.run_id, mint)] = (funder, state.creator)
                elif kind in replay.SELL_KINDS and integer(row.get("__sequence"), -2) == last_sell.get(mint, -1):
                    known_funder, creator = pending.pop((run.run_id, mint), ("", ""))
                    if known_funder:
                        (funder_wins if source_won else funder_losses)[known_funder] += 1
                        funder_creators[known_funder].add(creator)
                continue

            apply_event(state, row)
            if kind not in {"CREATE", *replay.BUY_KINDS}:
                continue
            now_ns = integer(row.get("received_ns"))
            if not funder or not relation_ns or relation_ns >= now_ns:
                continue
            if source_buy is not None and (
                now_ns, integer(row.get("__sequence"), -1)
            ) >= (
                integer(source_buy.get("received_ns")), integer(source_buy.get("__sequence"), -1)
            ):
                continue
            age_ms = max(0.0, (now_ns - state.create_ns) / 1e6)
            if state.sell_count > 0 or not 2_500.0 <= state.fdv_usd <= 12_000.0 or age_ms > 1_500.0:
                continue
            wins = funder_wins[funder]
            losses = funder_losses[funder]
            trades = wins + losses
            if wins <= 0:
                continue
            recent.append(mint)
            while recent:
                oldest = states.get(recent[0])
                if oldest is None or now_ns - oldest.create_ns > 1_500_000_000:
                    recent.popleft()
                else:
                    break
            active = []
            seen = set()
            for other_mint in reversed(recent):
                if other_mint in seen:
                    continue
                seen.add(other_mint)
                other = states.get(other_mint)
                if other is None or other.sell_count > 0:
                    continue
                other_relation = relations.get(other.creator, {})
                other_funder = str(other_relation.get("funder") or "")
                if not other_funder:
                    continue
                other_wins = funder_wins[other_funder]
                other_losses = funder_losses[other_funder]
                strength = 1.5 * math.log1p(other_wins) - math.log1p(other_losses)
                active.append((other_mint, strength, other.creator_seed_sol))
            strength = 1.5 * math.log1p(wins) - math.log1p(losses)
            funder_rank = 1 + sum(value > strength for other_mint, value, _ in active if other_mint != mint)
            seed_rank = 1 + sum(value > state.creator_seed_sol for other_mint, _, value in active if other_mint != mint)
            source_ns = integer(source_buy.get("received_ns")) if source_buy else 0
            source_sequence = integer(source_buy.get("__sequence"), -1) if source_buy else -1
            sequence = integer(row.get("__sequence"), -1)
            precedes = bool(source_buy and (now_ns, sequence) < (source_ns, source_sequence))
            snapshots.append({
                "run_id": run.run_id,
                "run_index": run_index,
                "mint": mint,
                "creator": state.creator,
                "funder": funder,
                "decision_ns": now_ns,
                "decision_sequence": sequence,
                "decision_event_id": row.get("event_id"),
                "decision_signature": str(row.get("signature") or ""),
                "decision_event_index": integer(row.get("event_index")),
                "funder_wins": wins,
                "funder_losses": losses,
                "funder_trades": trades,
                "funder_rate": wins / trades if trades else 0.0,
                "funder_wilson": wilson_lower(wins, trades),
                "funder_creator_count": len(funder_creators[funder]),
                "creator_seed_sol": state.creator_seed_sol,
                "outside_sol": state.outside_sol,
                "unique_buyers": len(state.buyers),
                "same_slot_buys": state.buy_slots.get(state.create_slot, 0),
                "create_signature_buys": state.buy_signatures.get(state.create_signature, 0),
                "fdv_usd": state.fdv_usd,
                "age_ms": age_ms,
                "funder_strength_rank": funder_rank,
                "seed_rank": seed_rank,
                "source_won": source_won,
                "source_intent": bool(source_buy),
                "lead_ms": (source_ns - now_ns) / 1e6 if precedes else None,
            })
        for mint, position in run.e4_positions.items():
            if (run.run_id, mint) in pending:
                known_funder, creator = pending.pop((run.run_id, mint))
                won = finite(position.get("pnl_sol")) > 0
                (funder_wins if won else funder_losses)[known_funder] += 1
                funder_creators[known_funder].add(creator)
        print(json.dumps({
            "run_id": run.run_id,
            "funder_snapshots": sum(integer(row["run_index"]) == run_index for row in snapshots),
            "known_winning_funders": len(funder_wins),
        }), flush=True)
    return snapshots


def accepts(row: Mapping[str, Any], rule: Rule) -> bool:
    return bool(
        integer(row.get("funder_wins")) >= rule.minimum_funder_wins
        and integer(row.get("funder_losses")) <= rule.maximum_funder_losses
        and finite(row.get("funder_rate")) >= rule.minimum_funder_rate
        and finite(row.get("funder_wilson")) >= rule.minimum_funder_wilson
        and integer(row.get("funder_creator_count")) >= rule.minimum_funder_creator_count
        and finite(row.get("creator_seed_sol")) >= rule.minimum_creator_seed_sol
        and finite(row.get("outside_sol")) >= rule.minimum_outside_sol
        and integer(row.get("unique_buyers")) >= rule.minimum_unique_buyers
        and integer(row.get("same_slot_buys")) >= rule.minimum_same_slot_buys
        and integer(row.get("create_signature_buys")) >= rule.minimum_create_signature_buys
        and rule.minimum_fdv_usd <= finite(row.get("fdv_usd")) <= rule.maximum_fdv_usd
        and finite(row.get("age_ms")) <= rule.maximum_age_ms
        and (not rule.require_funder_strength_top or integer(row.get("funder_strength_rank")) == 1)
        and (not rule.require_seed_top or integer(row.get("seed_rank")) == 1)
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
            "score": 0.98,
            "family": "v12_causal_funder_cluster",
            "entry_fraction": 0.0185,
            "lead_ms": row.get("lead_ms"),
            "source_won": row.get("source_won"),
        })
    return output


def rules() -> list[Rule]:
    output = []
    for wins, losses, rate, wilson, creators in (
        (1, 0, 0.60, 0.00, 1),
        (2, 0, 0.75, 0.20, 1),
        (2, 1, 0.66, 0.20, 2),
        (3, 1, 0.75, 0.25, 2),
        (5, 1, 0.80, 0.35, 3),
    ):
        for seed, outside, buyers, same_slot, create_sig in (
            (0.02, 0.0, 0, 0, 0),
            (0.50, 0.0, 0, 1, 0),
            (0.50, 0.25, 1, 1, 0),
            (1.50, 1.0, 1, 2, 0),
            (0.25, 0.0, 0, 1, 1),
        ):
            for fdv_min, fdv_max in (
                (2_750.0, 5_000.0),
                (3_200.0, 7_500.0),
                (2_750.0, 10_000.0),
            ):
                for age in (50.0, 150.0, 400.0):
                    for funder_top, seed_top in (
                        (False, False), (True, False), (False, True), (True, True)
                    ):
                        for floor in (200, 400, 600, 800, 1_000):
                            output.append(Rule(
                                wins, losses, rate, wilson, creators,
                                seed, outside, buyers, same_slot, create_sig,
                                fdv_min, fdv_max, age,
                                funder_top, seed_top, floor,
                            ))
    return output


def compact(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return golden.compact_grid(grid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search causal upstream-funder launch clusters")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--intelligence-cache", type=Path, required=True)
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(runs) < 8:
        parser.error("at least eight chronological runs are required")
    relations = load_relations(args.intelligence_cache)
    run_map = {run.run_id: run for run in runs}
    snapshots = build_snapshots(runs, relations)
    count = len(runs)
    train_end = count - 4
    validation_end = count - 2
    train = [row for row in snapshots if integer(row["run_index"]) < train_end]
    validation = [row for row in snapshots if train_end <= integer(row["run_index"]) < validation_end]
    holdout = [row for row in snapshots if integer(row["run_index"]) >= validation_end]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    seen: set[tuple[tuple[str, str], ...]] = set()
    shortlist = []
    for rule in rules():
        train_predictions = select(train, rule)
        key = tuple((row["run_id"], row["mint"]) for row in train_predictions)
        if len(key) < 8 or key in seen:
            continue
        seen.add(key)
        train_grid = golden.economic_grid(run_map, train_predictions, floor_bps=rule.output_shortfall_bps, latencies=latencies)
        if not golden.economics_pass(train_grid, 8):
            continue
        validation_predictions = select(validation, rule)
        if len(validation_predictions) < 3:
            continue
        validation_grid = golden.economic_grid(run_map, validation_predictions, floor_bps=rule.output_shortfall_bps, latencies=latencies)
        if not golden.economics_pass(validation_grid, 3):
            continue
        score = (
            min(finite(value["win_rate"]) for value in validation_grid.values()),
            min(finite(value["wilson_low"]) for value in validation_grid.values()),
            min(finite(value["profit_factor"]) for value in validation_grid.values()),
            sum(finite(value["net_pnl_sol"]) for value in validation_grid.values()),
            -len(validation_predictions),
        )
        shortlist.append((score, rule, train_grid, validation_grid))
    shortlist.sort(key=lambda item: item[0], reverse=True)
    best = None
    for score, rule, train_grid, validation_grid in shortlist[:50]:
        holdout_predictions = select(holdout, rule)
        holdout_grid = golden.economic_grid(run_map, holdout_predictions, floor_bps=rule.output_shortfall_bps, latencies=latencies)
        objective = (
            int(golden.economics_pass(holdout_grid, 3)),
            min((finite(value["win_rate"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["wilson_low"]) for value in holdout_grid.values()), default=0.0),
            min((finite(value["profit_factor"]) for value in holdout_grid.values()), default=0.0),
            sum(finite(value["net_pnl_sol"]) for value in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best = (objective, rule, train_grid, validation_grid, holdout_grid)
    passed = bool(best and best[0][0])
    payload = {
        "version": "e4-v12-funder-cluster-thesis-v1",
        "status": "HISTORICAL_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "run_ids": [run.run_id for run in runs],
        "relation_count": len(relations),
        "snapshots": len(snapshots),
        "unique_prediction_sets_checked": len(seen),
        "shortlisted_rules": len(shortlist),
    }
    if best:
        _, rule, train_grid, validation_grid, holdout_grid = best
        payload.update({
            "rule": rule.as_dict(),
            "train": compact(train_grid),
            "validation": compact(validation_grid),
            "historical_holdout": compact(holdout_grid),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
