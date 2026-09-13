#!/usr/bin/env python3
"""Validate canonical replay output and report paper P&L; no order execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

EXPECTED_LATENCIES = {0.0, 1.0, 2.0, 5.0, 10.0}


def wilson_low(wins: int, count: int) -> float:
    if not count:
        return 0.0
    z = 1.96
    p = wins / count
    return max(0.0, (p + z*z/(2*count) - z*math.sqrt((p*(1-p)+z*z/(4*count))/count))/(1+z*z/count))


def summarize(data: Mapping[str, Any], guard: int, bankroll: float = 2.0) -> dict[str, Any]:
    matrix = data.get("matrix")
    if not isinstance(matrix, dict) or set(matrix) != {str(guard)}:
        raise ValueError("missing or unexpected frozen output-guard scenario")
    scenarios = matrix[str(guard)]
    if {float(key) for key in scenarios} != EXPECTED_LATENCIES or len(scenarios) != 5:
        raise ValueError("all five independent 0/1/2/5/10 ms scenarios are required")
    rows: dict[str, Any] = {}
    for key, result in scenarios.items():
        start = float(result["starting_balance_sol"])
        end = float(result["ending_balance_sol"])
        net = float(result["net_pnl_sol"])
        fees = float(result["rejection_fees_sol"])
        positions = result["positions"]
        count = len(positions)
        wins = sum(float(row["pnl_sol"]) > 0 for row in positions)
        if not all(math.isfinite(value) for value in (start, end, net, fees)) or fees < 0:
            raise ValueError("nonfinite account values or negative fees")
        if not math.isclose(start, bankroll, abs_tol=1e-12):
            raise ValueError("incorrect starting bankroll")
        if result["closed"] != count or result["wins"] != wins:
            raise ValueError("trade totals disagree with the ledger")
        expected = sum(float(row["pnl_sol"]) for row in positions) - fees
        if not math.isclose(net, end-start, abs_tol=1e-8) or not math.isclose(net, expected, abs_tol=1e-8):
            raise ValueError("account P&L does not reconcile")
        gains = sum(max(0.0, float(row["pnl_sol"])) for row in positions)
        loss_cost = sum(max(0.0, -float(row["pnl_sol"])) for row in positions) + fees
        pf = gains/loss_cost if loss_cost > 0 else None
        wr = wins/count if count else None
        low = wilson_low(wins, count)
        pf_ok = pf >= 1.25 if pf is not None else gains > 0 and loss_cost == 0
        passed = count >= 3 and wr is not None and wr >= 0.65 and low >= 0.30 and net > 0 and pf_ok
        rows[key] = {
            "trades": count, "wins": wins, "losses": count-wins, "win_rate": wr,
            "wilson_low": low, "starting_balance_sol": start, "ending_balance_sol": end,
            "net_pnl_sol": net, "roi_percent": 100*net/start, "profit_factor": pf,
            "profit_factor_no_losses": gains > 0 and loss_cost == 0,
            "rejection_fees_sol": fees, "passed": passed,
        }
    passed = all(row["passed"] for row in rows.values())
    enough = all(row["trades"] >= 3 for row in rows.values())
    return {
        "version": "e4-v12-golden-live-2sol-v2",
        "status": "PAPER_FORWARD_GATE_PASSED" if passed else ("PAPER_FORWARD_GATE_FAILED" if enough else "INSUFFICIENT_TRADES"),
        "execution_mode": "paper_replay_of_newly_captured_market_events",
        "real_money_trades": False, "real_execution_validated": False,
        "metadata_available_at_decision_validated": False,
        "latencies_are_assumptions_not_measured_fills": True,
        "starting_balance_sol": bankroll, "latencies": rows,
        "caution": "This is an experimental paper result, not proof of live profitability or sub-10ms execution. Metadata is fetched retrospectively; availability at the decision time is not established.",
    }


def markdown(report: Mapping[str, Any]) -> str:
    lines = ["# Golden thesis — 2 SOL forward paper test", "", f"Status: **{report['status']}**", "", "| Assumed latency | Trades | Wins | Losses | Win rate | Net SOL | Ending SOL | ROI |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key, row in sorted(report["latencies"].items(), key=lambda pair: float(pair[0])):
        wr = "N/A" if row["win_rate"] is None else f"{100*row['win_rate']:.2f}%"
        lines.append(f"| {key} ms | {row['trades']} | {row['wins']} | {row['losses']} | {wr} | {row['net_pnl_sol']:+.6f} | {row['ending_balance_sol']:.6f} | {row['roi_percent']:+.2f}% |")
    return "\n".join(lines) + "\n\n" + str(report["caution"]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--economics", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model_bytes = args.model.read_bytes()
    model = json.loads(model_bytes)
    if model.get("status") != "HISTORICAL_HOLDOUT_CONFIRMED":
        raise ValueError("a historically confirmed frozen model is required")
    data = json.loads(args.economics.read_text())
    report = summarize(data, int(model["rule"]["max_output_shortfall_bps"]))
    report["frozen_model_sha256"] = hashlib.sha256(model_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False), flush=True)
    return 0 if report["status"] == "PAPER_FORWARD_GATE_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
