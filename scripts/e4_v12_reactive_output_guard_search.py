#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

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


@dataclass(frozen=True)
class Rule:
    output_shortfall_bps: int
    maximum_source_sol: float
    minimum_fdv_usd: float
    maximum_fdv_usd: float
    minimum_prior_creator_wins: int
    minimum_creator_seed_sol: float
    maximum_age_ms: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Candidate:
    run_id: str
    mint: str
    creator: str
    source_buy: dict[str, Any]
    source_sells: list[dict[str, Any]]
    source_sol: float
    fdv_usd: float
    age_ms: float
    creator_seed_sol: float
    outside_sol: float
    unique_buyers: int
    buy_count: int
    same_slot_buys: int
    prior_creator_wins: int
    prior_creator_losses: int
    source_won: bool


def pre_source_features(
    run: replay.RunData,
    mint: str,
    source_buy: Mapping[str, Any],
    creator_wins: Counter[str],
    creator_losses: Counter[str],
) -> Candidate:
    rows = run.events_by_mint[mint]
    creator = ""
    create_ns = integer(rows[0].get("received_ns"))
    create_slot = integer(rows[0].get("slot"))
    creator_seed = 0.0
    outside = 0.0
    buyers: set[str] = set()
    buy_count = 0
    same_slot_buys = 0
    for row in rows:
        if (
            integer(row.get("received_ns")), integer(row.get("__sequence"), -1)
        ) >= (
            integer(source_buy.get("received_ns")), integer(source_buy.get("__sequence"), -1)
        ):
            break
        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
        if str(row.get("kind") or "").upper() == "CREATE":
            creator = str(row.get("creator") or raw.get("creator") or row.get("trader") or "")
            create_ns = integer(row.get("received_ns"))
            create_slot = integer(row.get("slot"))
        if str(row.get("kind") or "").upper() not in replay.BUY_KINDS:
            continue
        trader = str(row.get("trader") or "")
        if trader == replay.E4_WALLET:
            continue
        amount = max(0.0, finite(row.get("sol_amount")))
        buy_count += 1
        if integer(row.get("slot")) == create_slot:
            same_slot_buys += 1
        if trader and trader == creator:
            creator_seed += amount
        elif trader:
            outside += amount
            buyers.add(trader)
    position = run.e4_positions.get(mint, {})
    return Candidate(
        run_id=run.run_id,
        mint=mint,
        creator=creator,
        source_buy=dict(source_buy),
        source_sells=replay.source_events(rows)[1],
        source_sol=max(0.0, finite(source_buy.get("sol_amount"))),
        fdv_usd=max(0.0, finite(source_buy.get("fdv_usd"))),
        age_ms=max(0.0, (integer(source_buy.get("received_ns")) - create_ns) / 1e6),
        creator_seed_sol=creator_seed,
        outside_sol=outside,
        unique_buyers=len(buyers),
        buy_count=buy_count,
        same_slot_buys=same_slot_buys,
        prior_creator_wins=creator_wins[creator],
        prior_creator_losses=creator_losses[creator],
        source_won=finite(position.get("pnl_sol")) > 0,
    )


def build_candidates(ordered_runs: Sequence[replay.RunData]) -> list[Candidate]:
    creator_wins: Counter[str] = Counter()
    creator_losses: Counter[str] = Counter()
    output: list[Candidate] = []
    for run in ordered_runs:
        run_candidates: list[Candidate] = []
        for mint, rows in run.events_by_mint.items():
            source_buy, source_sells = replay.source_events(rows)
            if source_buy is None or not source_sells:
                continue
            candidate = pre_source_features(
                run,
                mint,
                source_buy,
                creator_wins,
                creator_losses,
            )
            if candidate.source_sol <= 0 or candidate.fdv_usd <= 0:
                continue
            run_candidates.append(candidate)
        run_candidates.sort(key=lambda row: integer(row.source_buy.get("received_ns")))
        output.extend(run_candidates)
        for candidate in run_candidates:
            if candidate.creator:
                (creator_wins if candidate.source_won else creator_losses)[candidate.creator] += 1
        print(json.dumps({
            "run_id": run.run_id,
            "reactive_candidates": len(run_candidates),
            "source_wins": sum(row.source_won for row in run_candidates),
        }, sort_keys=True), flush=True)
    return output


def accepts(candidate: Candidate, rule: Rule) -> bool:
    return bool(
        candidate.source_sol <= rule.maximum_source_sol
        and rule.minimum_fdv_usd <= candidate.fdv_usd <= rule.maximum_fdv_usd
        and candidate.prior_creator_wins >= rule.minimum_prior_creator_wins
        and candidate.creator_seed_sol >= rule.minimum_creator_seed_sol
        and candidate.age_ms <= rule.maximum_age_ms
    )


def state_before_source(run: replay.RunData, candidate: Candidate) -> replay.ReserveState | None:
    states = run.reserves_by_mint.get(candidate.mint, [])
    sequence = integer(candidate.source_buy.get("__sequence"), -1) - 1
    timestamp = integer(candidate.source_buy.get("received_ns"))
    return replay.state_at_or_before(states, timestamp, sequence)


def simulate_candidate(
    run: replay.RunData,
    candidate: Candidate,
    rule: Rule,
    *,
    latency_ms: float,
    liquid_sol: float,
) -> tuple[dict[str, Any] | None, float]:
    states = run.reserves_by_mint.get(candidate.mint, [])
    expected_state = state_before_source(run, candidate)
    if expected_state is None:
        return None, 0.0
    source_ns = integer(candidate.source_buy.get("received_ns"))
    source_sequence = integer(candidate.source_buy.get("__sequence"), -1)
    fill_ns = source_ns + int(latency_ms * 1e6)
    fill_state = replay.state_at_or_before(
        states,
        fill_ns,
        source_sequence if latency_ms <= 0 else None,
    )
    if fill_state is None:
        return None, 0.0

    budget = min(0.30, max(0.0, liquid_sol - 0.03), liquid_sol * 0.0185)
    curve_input = replay.curve_input_for_budget(budget, 0.0125, 0.96)
    if curve_input <= 0:
        return None, 0.0
    expected_tokens = replay.buy_tokens(curve_input, expected_state)
    actual_tokens = replay.buy_tokens(curve_input, fill_state)
    if expected_tokens <= 0 or actual_tokens <= 0:
        return None, 0.0
    shortfall = max(0.0, (1.0 - actual_tokens / expected_tokens) * 10_000.0)
    if shortfall > rule.output_shortfall_bps + 1e-9:
        return None, replay.priority_failure_cost(curve_input, 0.96)

    entry_cost = curve_input * 1.0125 + replay.fee_bid(curve_input, 0.96)
    remaining = actual_tokens
    proceeds = 0.0
    source_tokens = max(1e-12, finite(candidate.source_buy.get("token_amount")))
    cumulative = 0.0
    sell_count = 0
    first_partial = None
    exit_ns = fill_ns
    for index, sell in enumerate(candidate.source_sells):
        fraction = min(
            1.0 - cumulative,
            max(0.0, finite(sell.get("token_amount")) / source_tokens),
        )
        if fraction <= 0:
            continue
        cumulative += fraction
        if first_partial is None:
            first_partial = fraction
        tokens = min(remaining, actual_tokens * fraction)
        due = integer(sell.get("received_ns")) + int(latency_ms * 1e6)
        state = replay.state_at_or_before(
            states,
            due,
            integer(sell.get("__sequence"), -1) if latency_ms <= 0 else None,
        ) or replay.state_at_or_after(states, due)
        if state is None:
            continue
        gross = replay.sell_sol(tokens, state)
        urgent = index == len(candidate.source_sells) - 1
        proceeds += max(
            0.0,
            gross * 0.9875 - replay.fee_bid(curve_input * fraction, 1.0, urgent),
        )
        remaining = max(0.0, remaining - tokens)
        sell_count += 1
        exit_ns = max(exit_ns, due)
    if remaining > actual_tokens * 1e-6:
        state = states[-1] if states else None
        if state is not None:
            gross = replay.sell_sol(remaining, state)
            proceeds += max(0.0, gross * 0.9875 - replay.fee_bid(curve_input, 1.0, True))
            sell_count += 1
            exit_ns = max(exit_ns, state.received_ns)
    return {
        "run_id": candidate.run_id,
        "mint": candidate.mint,
        "latency_ms": latency_ms,
        "entry_cost_sol": entry_cost,
        "proceeds_sol": proceeds,
        "pnl_sol": proceeds - entry_cost,
        "output_shortfall_bps": shortfall,
        "source_sol": candidate.source_sol,
        "fdv_usd": candidate.fdv_usd,
        "age_ms": candidate.age_ms,
        "prior_creator_wins": candidate.prior_creator_wins,
        "creator_seed_sol": candidate.creator_seed_sol,
        "source_won": candidate.source_won,
        "first_partial_fraction": first_partial,
        "sell_count": sell_count,
        "fill_ns": fill_ns,
        "exit_ns": exit_ns,
    }, 0.0


def evaluate(
    run_map: Mapping[str, replay.RunData],
    candidates: Sequence[Candidate],
    rule: Rule,
    latency_ms: float,
) -> dict[str, Any]:
    by_run: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if accepts(candidate, rule):
            by_run.setdefault(candidate.run_id, []).append(candidate)
    positions: list[dict[str, Any]] = []
    rejection_fees = 0.0
    rejected = 0
    for run_id, rows in by_run.items():
        liquid = 3.0
        active: list[dict[str, Any]] = []
        touched: set[str] = set()
        for candidate in sorted(rows, key=lambda row: integer(row.source_buy.get("received_ns"))):
            now_ns = integer(candidate.source_buy.get("received_ns"))
            remaining = []
            for trade in active:
                if integer(trade["exit_ns"]) <= now_ns:
                    liquid += finite(trade["proceeds_sol"])
                    positions.append(trade)
                else:
                    remaining.append(trade)
            active = remaining
            if candidate.mint in touched or len(active) >= 2:
                continue
            touched.add(candidate.mint)
            trade, failed_fee = simulate_candidate(
                run_map[run_id],
                candidate,
                rule,
                latency_ms=latency_ms,
                liquid_sol=liquid,
            )
            if trade is None:
                if failed_fee > 0:
                    liquid = max(0.0, liquid - failed_fee)
                    rejection_fees += failed_fee
                    rejected += 1
                continue
            if liquid - finite(trade["entry_cost_sol"]) < 0.03:
                continue
            liquid -= finite(trade["entry_cost_sol"])
            active.append(trade)
        for trade in active:
            liquid += finite(trade["proceeds_sol"])
            positions.append(trade)
    wins = sum(finite(row.get("pnl_sol")) > 0 for row in positions)
    positive = sum(finite(row.get("pnl_sol")) for row in positions if finite(row.get("pnl_sol")) > 0)
    negative = sum(finite(row.get("pnl_sol")) for row in positions if finite(row.get("pnl_sol")) < 0)
    return {
        "closed": len(positions),
        "wins": wins,
        "losses": len(positions) - wins,
        "win_rate": wins / len(positions) if positions else 0.0,
        "wilson_low": wilson_lower(wins, len(positions)),
        "net_pnl_sol": sum(finite(row.get("pnl_sol")) for row in positions) - rejection_fees,
        "profit_factor": positive / abs(negative) if negative < 0 else (999.0 if positive > 0 else 0.0),
        "output_rejections": rejected,
        "rejection_fees_sol": rejection_fees,
        "positions": positions,
    }


def passes(grid: Mapping[str, Mapping[str, Any]], minimum_trades: int) -> bool:
    return bool(grid) and all(
        integer(row.get("closed")) >= minimum_trades
        and finite(row.get("win_rate")) >= 0.65
        and finite(row.get("wilson_low")) >= 0.30
        and finite(row.get("net_pnl_sol")) > 0
        and finite(row.get("profit_factor")) >= 1.25
        for row in grid.values()
    )


def compact(grid: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        key: {name: value for name, value in row.items() if name != "positions"}
        for key, row in grid.items()
    }


def rules() -> list[Rule]:
    output = []
    for floor in (200, 400, 600, 800, 1_000, 1_250, 1_500, 2_000):
        for max_sol in (0.75, 1.5, 3.0, 5.0, 10.0, 100.0):
            for fdv_min, fdv_max in (
                (2_750.0, 5_000.0),
                (3_200.0, 7_500.0),
                (4_000.0, 8_500.0),
                (2_750.0, 10_000.0),
            ):
                for prior_wins in (0, 1, 2):
                    for seed in (0.0, 0.5, 1.5, 3.0):
                        for age in (50.0, 150.0, 400.0, 1_500.0):
                            output.append(Rule(floor, max_sol, fdv_min, fdv_max, prior_wins, seed, age))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Find an E4-confirmed tight-output V12 entry rule")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--latencies-ms", default="0,1,2,5,10")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ordered_runs = [replay.load_run(*replay.parse_pair(value)) for value in args.pair]
    if len(ordered_runs) < 8:
        parser.error("at least eight chronological runs are required")
    run_map = {run.run_id: run for run in ordered_runs}
    candidates = build_candidates(ordered_runs)
    run_ids = [run.run_id for run in ordered_runs]
    train_ids = set(run_ids[:-4])
    validation_ids = set(run_ids[-4:-2])
    holdout_ids = set(run_ids[-2:])
    train = [row for row in candidates if row.run_id in train_ids]
    validation = [row for row in candidates if row.run_id in validation_ids]
    holdout = [row for row in candidates if row.run_id in holdout_ids]
    latencies = [finite(value) for value in args.latencies_ms.split(",") if value.strip()]

    shortlist = []
    for rule in rules():
        train_grid = {str(latency): evaluate(run_map, train, rule, latency) for latency in latencies}
        if not passes(train_grid, 12):
            continue
        validation_grid = {str(latency): evaluate(run_map, validation, rule, latency) for latency in latencies}
        if not passes(validation_grid, 4):
            continue
        score = (
            min(finite(row["win_rate"]) for row in validation_grid.values()),
            min(finite(row["wilson_low"]) for row in validation_grid.values()),
            min(finite(row["profit_factor"]) for row in validation_grid.values()),
            sum(finite(row["net_pnl_sol"]) for row in validation_grid.values()),
            -rule.output_shortfall_bps,
        )
        shortlist.append((score, rule, train_grid, validation_grid))
    shortlist.sort(key=lambda item: item[0], reverse=True)

    best = None
    for score, rule, train_grid, validation_grid in shortlist[:50]:
        holdout_grid = {str(latency): evaluate(run_map, holdout, rule, latency) for latency in latencies}
        objective = (
            int(passes(holdout_grid, 4)),
            min(finite(row["win_rate"]) for row in holdout_grid.values()),
            min(finite(row["wilson_low"]) for row in holdout_grid.values()),
            min(finite(row["profit_factor"]) for row in holdout_grid.values()),
            sum(finite(row["net_pnl_sol"]) for row in holdout_grid.values()),
            *score,
        )
        if best is None or objective > best[0]:
            best = (objective, rule, train_grid, validation_grid, holdout_grid)
    passed = bool(best and best[0][0])
    payload = {
        "version": "e4-v12-reactive-output-guard-thesis-v1",
        "status": "HISTORICAL_GOLDEN_CONFIRMED" if passed else "NOT_CONCLUSIVE",
        "run_ids": run_ids,
        "candidate_count": len(candidates),
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
