#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from memecoin_bot import e4_hardening_v10 as v10  # noqa: F401 - patches production policy


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_v10_winrate_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denominator = 1.0 + (z * z) / total
    centre = p + (z * z) / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)
    return (centre - margin) / denominator, (centre + margin) / denominator


def read_events(path: Path, base: Any) -> list[Any]:
    events: list[Any] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            events.append(base.LiveEvent(**payload))
    events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict V10 out-of-sample win-rate certification")
    parser.add_argument("--holdout-report", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--latency-ms", type=float, default=36.0)
    parser.add_argument("--starting-balance-sol", type=float, default=1.2)
    parser.add_argument("--minimum-launches", type=int, default=3000)
    parser.add_argument("--minimum-closed-trades", type=int, default=100)
    parser.add_argument("--output", default="artifacts/e4-v10-winrate-certification.json")
    args = parser.parse_args()

    base = load_base()
    report = json.loads(Path(args.holdout_report).read_text(encoding="utf-8"))
    events = read_events(Path(args.events), base)
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        grouped[event.mint].append(event)
    for sequence in grouped.values():
        sequence.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    settings = base.core.Settings(model_path=Path("missing-model.json"))
    candidates = [
        trade
        for sequence in grouped.values()
        if (trade := base.simulate_token(sequence, settings, args.latency_ms))
    ]
    portfolio = base.evaluate_portfolio(candidates, args.starting_balance_sol, settings)

    closed = int(portfolio.get("closed_positions") or 0)
    net_rate = float(portfolio.get("net_win_rate") or 0.0) if closed else None
    wins = int(round((net_rate or 0.0) * closed)) if closed else 0
    ci_low, ci_high = wilson_interval(wins, closed)
    capture = report.get("capture") or {}
    launches = int(capture.get("new_launches") or 0)
    fresh_e4 = report.get("actual_e4_fresh_sample") or {}
    e4_closed = int(fresh_e4.get("closed_positions") or 0)
    e4_rate = fresh_e4.get("net_win_rate") if e4_closed else None
    gap = (net_rate - float(e4_rate)) if net_rate is not None and e4_rate is not None else None

    pf = portfolio.get("profit_factor")
    pnl = float(portfolio.get("net_pnl_sol") or 0.0)
    reliable_sample = launches >= args.minimum_launches and closed >= args.minimum_closed_trades
    positive_expectancy = pnl > 0 and (pf is not None and float(pf) > 1.0)
    e4_like = bool(
        reliable_sample
        and positive_expectancy
        and e4_rate is not None
        and net_rate is not None
        and net_rate >= max(0.55, float(e4_rate) - 0.08)
        and float(pf or 0.0) >= 1.5
    )

    payload = {
        "version": "e4-v10-winrate-certification-v1",
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "frozen_latency_ms": args.latency_ms,
        "starting_balance_sol": args.starting_balance_sol,
        "launches": launches,
        "candidate_trades": len(candidates),
        "closed_trades": closed,
        "net_wins_estimated_from_reported_rate": wins,
        "net_win_rate": net_rate,
        "net_win_rate_wilson_95": {"lower": ci_low, "upper": ci_high},
        "net_pnl_sol": pnl,
        "profit_factor": pf,
        "max_concurrent_positions": portfolio.get("max_concurrent_positions"),
        "reentries": portfolio.get("reentries"),
        "losers_exited_within_5s_fraction": portfolio.get("losers_exited_within_5s_fraction"),
        "actual_e4_fresh_comparator": {
            "closed_trades": e4_closed,
            "net_win_rate": e4_rate,
            "net_pnl_sol": fresh_e4.get("net_pnl_sol"),
            "profit_factor": fresh_e4.get("profit_factor"),
        },
        "net_win_rate_gap_vs_e4": gap,
        "gates": {
            "minimum_launches_met": launches >= args.minimum_launches,
            "minimum_closed_trades_met": closed >= args.minimum_closed_trades,
            "positive_net_pnl": pnl > 0,
            "profit_factor_above_one": pf is not None and float(pf) > 1.0,
            "zero_reentries": portfolio.get("reentries") == 0,
            "max_two_positions": int(portfolio.get("max_concurrent_positions") or 0) <= 2,
        },
        "reliable_sample": reliable_sample,
        "e4_like_performance": e4_like,
        "classification": (
            "E4_LIKE_HYPOTHESIS_RESULT"
            if e4_like
            else "RELIABLE_BUT_NOT_E4_LIKE"
            if reliable_sample
            else "INSUFFICIENT_SAMPLE"
        ),
        "limitations": [
            "This is a forward, hypothesis-only replay of real Pump launch/trade events; no funded mainnet order is submitted.",
            "36ms is an execution-delay hypothesis applied to the observed event stream, not proof of 36ms validator landing.",
            "The E4 comparator is reconstructed from E4 wallet transactions sampled during the run and is not guaranteed to contain only the exact same launch cohort.",
            "Social-pipeline win rate is measurable only when a live official-X stream is active during the same forward period.",
        ],
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(
        "\n".join(
            [
                "# Gambit E4 V10 forward win-rate certification",
                "",
                f"**Classification:** {payload['classification']}",
                "**Hypothesis only:** yes — zero funded mainnet orders.",
                f"**Fresh launches:** {launches}",
                f"**Closed Gambit trades:** {closed}",
                f"**36ms net win rate:** {net_rate}",
                f"**95% Wilson interval:** {ci_low} to {ci_high}",
                f"**Net P&L:** {pnl} SOL",
                f"**Profit factor:** {pf}",
                f"**Fresh E4 comparator closed trades:** {e4_closed}",
                f"**Fresh E4 comparator net WR:** {e4_rate}",
                f"**WR gap vs E4:** {gap}",
                "",
                "A win-rate claim is not accepted unless the frozen run contains at least "
                f"{args.minimum_launches} fresh launches and {args.minimum_closed_trades} closed Gambit trades.",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2))
    return 0 if reliable_sample else 2


if __name__ == "__main__":
    raise SystemExit(main())
