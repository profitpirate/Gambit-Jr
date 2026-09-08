#!/usr/bin/env python3
"""Strict economic overrides for the all-out launch-hazard search.

The discovery implementation intentionally exposed every intermediate trade.
This module makes promotion-grade chronology explicit: stake is removed at
entry, proceeds return only at exit, open capital is locked, and drawdown is
measured on equity rather than treating locked principal as a loss.
"""
from __future__ import annotations

import heapq
import statistics
from typing import Any, Sequence

import numpy as np

from scripts import e4_v12_allout_profit_hazard as base


def chronological_economics_strict(
    indices: Sequence[int],
    scores: np.ndarray,
    corpus: base.Corpus,
    policy: str,
    latency_column: int,
) -> dict[str, Any]:
    ordered = sorted(
        {int(index) for index in indices},
        key=lambda index: (int(corpus.create_ns[index]), str(corpus.mints[index])),
    )
    cash = base.STARTING_BANKROLL_SOL
    realised_pnl = 0.0
    open_positions: list[tuple[int, int, float, float, dict[str, Any]]] = []
    sequence = 0
    peak_equity = base.STARTING_BANKROLL_SOL
    maximum_drawdown = 0.0
    closed: list[dict[str, Any]] = []
    skipped_concurrency = 0
    static_pnl = corpus.pnl[policy][:, latency_column]
    exit_delays = corpus.exit_delay_ms[policy][:, latency_column]

    def equity() -> float:
        # At an unresolved instant the safest non-oracle accounting value for an
        # open position is its locked principal.  No future PnL is recognised.
        return cash + sum(item[2] for item in open_positions)

    def mark_drawdown() -> None:
        nonlocal peak_equity, maximum_drawdown
        value = equity()
        peak_equity = max(peak_equity, value)
        if peak_equity > 0:
            maximum_drawdown = max(
                maximum_drawdown,
                (peak_equity - value) / peak_equity,
            )

    def settle_through(timestamp_ns: int) -> None:
        nonlocal cash, realised_pnl
        while open_positions and open_positions[0][0] <= timestamp_ns:
            _, _, stake, trade_pnl, record = heapq.heappop(open_positions)
            cash += stake + trade_pnl
            realised_pnl += trade_pnl
            record["pnl_sol"] = trade_pnl
            record["ending_cash_after_exit_sol"] = cash
            closed.append(record)
            mark_drawdown()

    for index in ordered:
        now = int(corpus.create_ns[index])
        settle_through(now)
        if len(open_positions) >= base.MAX_CONCURRENT:
            skipped_concurrency += 1
            continue
        current_equity = equity()
        available_cash = max(0.0, cash - base.RESERVE_SOL)
        stake = min(available_cash, current_equity * base.POSITION_FRACTION)
        if stake <= 0:
            continue
        raw_return = base.finite(static_pnl[index]) / base.ENTRY_BUDGET_SOL
        trade_pnl = stake * raw_return
        cash -= stake
        exit_ns = now + int(max(0.0, exit_delays[index]) * 1_000_000)
        record = {
            "index": index,
            "mint": str(corpus.mints[index]),
            "run_id": str(corpus.run_ids[index]),
            "create_ns": now,
            "exit_ns": exit_ns,
            "score": float(scores[index]),
            "stake_sol": stake,
            "scaled_return": raw_return,
            "pnl_sol": None,
            "selected_by_e4": bool(corpus.selected[index]),
            "landed_by_e4": bool(corpus.landed[index]),
        }
        heapq.heappush(
            open_positions,
            (exit_ns, sequence, stake, trade_pnl, record),
        )
        sequence += 1
        mark_drawdown()

    settle_through(2**63 - 1)
    pnls = [float(row["pnl_sol"]) for row in closed]
    wins = sum(value > 0 for value in pnls)
    gains = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    total_profit = sum(gains)
    largest = max(gains, default=0.0)
    ending_equity = cash
    return {
        "trades": len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": wins / len(closed) if closed else 0.0,
        "wilson_lower": base.wilson_lower(wins, len(closed)),
        "net_pnl_sol": ending_equity - base.STARTING_BANKROLL_SOL,
        "ending_bankroll_sol": ending_equity,
        "profit_factor": base.profit_factor(pnls),
        "maximum_drawdown_fraction": maximum_drawdown,
        "expectancy_sol": statistics.fmean(pnls) if pnls else 0.0,
        "average_win_sol": statistics.fmean(gains) if gains else 0.0,
        "average_loss_sol": statistics.fmean(losses) if losses else 0.0,
        "largest_winner_profit_share": largest / total_profit if total_profit > 0 else 0.0,
        "capture_windows": len({row["run_id"] for row in closed}),
        "e4_selected_overlap": sum(row["selected_by_e4"] for row in closed),
        "e4_landed_overlap": sum(row["landed_by_e4"] for row in closed),
        "skipped_for_concurrency": skipped_concurrency,
        "open_positions_at_end": 0,
        "realised_pnl_sol": realised_pnl,
        "chronology": "stake locked at entry; proceeds settled at exit",
        "closed": closed,
    }


base.chronological_economics = chronological_economics_strict


if __name__ == "__main__":
    # Importing the memory-bounded entry point patches corpus loading and then
    # delegates to the original argument parser, which now resolves this strict
    # economics function through module globals.
    from scripts import e4_v12_allout_profit_hazard_stream as stream

    base.load_corpus = stream.load_corpus_stream
    raise SystemExit(base.main())
