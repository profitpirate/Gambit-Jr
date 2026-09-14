#!/usr/bin/env python3
"""Reproduce E4 wallet-conviction descriptive research on the frozen all-out corpus."""
from __future__ import annotations
import argparse, gzip, json, math, statistics
from pathlib import Path
from typing import Any, Iterable

LATENCIES = (0, 1, 2, 5, 10)
NORMAL_MIN_SOL = 1.0
NORMAL_MAX_SOL = 50.0
KEEP = {
    "split", "landed_successfully", "e4_cost_sol", "e4_pnl_sol", "e4_hold_ms", "e4_sell_count",
    "observed_e4_first_buy_delay_ms", "mayhem_mode", "cashback_enabled",
    "creator_prior_selection_count", "creator_prior_known_win_rate",
}
KEEP.update(f"paper_{lat}ms_hold_2000ms_pnl_sol" for lat in LATENCIES)

def finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default

def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            source = json.loads(line)
            rows.append({key: source.get(key) for key in KEEP})
    return rows

def stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    pnls = [finite(row.get("e4_pnl_sol")) for row in rows]
    pnls = [float(x) for x in pnls if x is not None]
    wins = sum(x > 0 for x in pnls)
    losses = sum(x <= 0 for x in pnls)
    gains = sum(x for x in pnls if x > 0)
    loss_abs = -sum(x for x in pnls if x < 0)
    holds = [finite(row.get("e4_hold_ms")) for row in rows]
    holds = [float(x) for x in holds if x is not None]
    return {
        "trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(pnls) if pnls else None,
        "net_pnl_sol": sum(pnls),
        "profit_factor": gains / loss_abs if loss_abs > 0 else (999.0 if gains > 0 else None),
        "median_hold_ms": statistics.median(holds) if holds else None,
    }

def paper_rule(rows: list[dict[str, Any]], split: str) -> dict[str, Any]:
    selected = [
        row for row in rows
        if str(row.get("split")) == split
        and not bool(row.get("mayhem_mode"))
        and bool(row.get("cashback_enabled"))
        and (finite(row.get("creator_prior_selection_count"), 0.0) or 0.0) >= 3
        and (finite(row.get("creator_prior_known_win_rate"), 0.0) or 0.0) >= 0.25
    ]
    per_latency = {}
    for latency in LATENCIES:
        key = f"paper_{latency}ms_hold_2000ms_pnl_sol"
        pnls = [finite(row.get(key), -0.0555) for row in selected]
        pnls = [float(x) for x in pnls if x is not None]
        wins = sum(x > 0 for x in pnls)
        gains = sum(x for x in pnls if x > 0)
        loss_abs = -sum(x for x in pnls if x < 0)
        per_latency[str(latency)] = {
            "trades": len(pnls),
            "wins": wins,
            "losses": len(pnls) - wins,
            "win_rate": wins / len(pnls) if pnls else None,
            "net_pnl_sol": sum(pnls),
            "profit_factor": gains / loss_abs if loss_abs > 0 else (999.0 if gains > 0 else None),
        }
    blocks = list(per_latency.values())
    return {
        "trades": len(selected),
        "latencies": per_latency,
        "minimum_win_rate": min((b["win_rate"] for b in blocks if b["win_rate"] is not None), default=None),
        "minimum_net_pnl_sol": min((b["net_pnl_sol"] for b in blocks), default=None),
        "minimum_profit_factor": min((b["profit_factor"] for b in blocks if b["profit_factor"] is not None), default=None),
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.corpus)

    landed = []
    for row in rows:
        cost = finite(row.get("e4_cost_sol"))
        pnl = finite(row.get("e4_pnl_sol"))
        if bool(row.get("landed_successfully")) and cost is not None and pnl is not None and NORMAL_MIN_SOL <= cost < NORMAL_MAX_SOL:
            landed.append(row)

    size_cohorts = {
        str(threshold): stats(row for row in landed if (finite(row.get("e4_cost_sol"), 0.0) or 0.0) >= threshold)
        for threshold in (1.0, 2.0, 2.5, 3.0)
    }
    delay_cohorts = {
        str(delay_ms): stats(
            row for row in landed
            if (finite(row.get("observed_e4_first_buy_delay_ms"), float("inf")) or float("inf")) <= delay_ms
        )
        for delay_ms in (1.0, 2.0, 5.0, 10.0)
    }
    joint = {}
    for threshold, delay_ms in ((2.0, 5.0), (2.0, 10.0), (2.5, 5.0), (2.5, 10.0)):
        joint[f"cost_ge_{threshold}_delay_le_{delay_ms}ms"] = stats(
            row for row in landed
            if (finite(row.get("e4_cost_sol"), 0.0) or 0.0) >= threshold
            and (finite(row.get("observed_e4_first_buy_delay_ms"), float("inf")) or float("inf")) <= delay_ms
        )

    winners = [row for row in landed if (finite(row.get("e4_pnl_sol"), 0.0) or 0.0) > 0]
    losers = [row for row in landed if (finite(row.get("e4_pnl_sol"), 0.0) or 0.0) <= 0]
    def exit_block(group: list[dict[str, Any]]) -> dict[str, Any]:
        holds = [float(finite(row.get("e4_hold_ms"), 0.0) or 0.0) for row in group]
        sells = [float(finite(row.get("e4_sell_count"), 0.0) or 0.0) for row in group]
        return {
            "trades": len(group),
            "median_hold_ms": statistics.median(holds) if holds else None,
            "fully_exited_within_5s_fraction": sum(x <= 5000 for x in holds) / len(holds) if holds else None,
            "median_sell_count": statistics.median(sells) if sells else None,
        }

    payload = {
        "version": "e4-v12-wallet-conviction-research-v1",
        "corpus_rows": len(rows),
        "normal_e4_landed": stats(landed),
        "size_cohorts": size_cohorts,
        "delay_cohorts": delay_cohorts,
        "joint_cohorts": joint,
        "exit_behaviour": {"winners": exit_block(winners), "losers": exit_block(losers)},
        "exploratory_rule": {
            "definition": "non-mayhem && cashback && creator_prior_selection_count>=3 && creator_prior_known_win_rate>=0.25; hold 2000ms",
            "validation": paper_rule(rows, "validation"),
            "holdout": paper_rule(rows, "holdout"),
            "certified": False,
        },
        "contract": {
            "source_size_and_delay_are_labels_only": True,
            "e4_wallet_must_be_excluded_from_entry_flow_features": True,
            "no_live_retuning": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
