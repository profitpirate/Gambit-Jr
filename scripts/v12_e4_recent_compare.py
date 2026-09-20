"""Reconstruct E4's recent on-chain trades and compare them with frozen V12 Pre-Armed."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e4_live_market_stress as stress


def ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def pct(values: list[float], q: float) -> float | None:
    return stress.percentile(values, q)


async def signatures_since(
    rpc: stress.RpcPool, cutoff: int, *, prebuffer_seconds: int = 3600
) -> list[Mapping[str, Any]]:
    target = cutoff - prebuffer_seconds
    rows: list[Mapping[str, Any]] = []
    before = None
    for _page in range(30):
        config: dict[str, Any] = {"limit": 1000}
        if before:
            config["before"] = before
        batch = await rpc.call("getSignaturesForAddress", [stress.E4_WALLET, config])
        if not batch:
            break
        rows.extend(batch)
        times = [int(row.get("blockTime") or 0) for row in batch if row.get("blockTime")]
        before = batch[-1]["signature"]
        if times and min(times) < target:
            break
        if len(batch) < 1000:
            break
    return [row for row in rows if int(row.get("blockTime") or 0) >= target]


async def fetch_transactions(
    rpc: stress.RpcPool, signatures: list[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    semaphore = asyncio.Semaphore(8)

    async def one(row: Mapping[str, Any]):
        async with semaphore:
            for attempt in range(4):
                try:
                    tx = await rpc.call(
                        "getTransaction",
                        [
                            row["signature"],
                            {
                                "encoding": "jsonParsed",
                                "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )
                    return row, tx if isinstance(tx, Mapping) else None
                except Exception:
                    await asyncio.sleep(0.2 * (attempt + 1))
            return row, None

    return await asyncio.gather(*(one(row) for row in signatures))


def wallet_events(
    fetched: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    counts = {
        "transactions_seen": 0,
        "transactions_missing": 0,
        "failed_transactions": 0,
        "quote_asset_changes_ignored": 0,
        "multi_asset_changes_ignored": 0,
        "non_trade_token_movements_ignored": 0,
    }
    for row, tx in fetched:
        if not tx:
            counts["transactions_missing"] += 1
            continue
        counts["transactions_seen"] += 1
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            counts["failed_transactions"] += 1
            continue
        keys = stress.account_keys(tx)
        if stress.E4_WALLET not in keys:
            continue
        idx = keys.index(stress.E4_WALLET)
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []
        if idx >= len(pre_sol) or idx >= len(post_sol):
            continue
        sol_delta = (float(post_sol[idx]) - float(pre_sol[idx])) / stress.core.LAMPORTS_PER_SOL
        pre = stress.token_totals(meta.get("preTokenBalances") or [], stress.E4_WALLET)
        post = stress.token_totals(meta.get("postTokenBalances") or [], stress.E4_WALLET)
        changed = []
        for mint in set(pre) | set(post):
            delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
            if abs(delta) <= max(1e-9, abs(pre.get(mint, 0.0)) * 1e-12):
                continue
            if mint in stress.QUOTE_ASSET_MINTS:
                counts["quote_asset_changes_ignored"] += 1
                continue
            changed.append((mint, delta, post.get(mint, 0.0)))
        if len(changed) != 1:
            if changed:
                counts["multi_asset_changes_ignored"] += 1
            continue
        mint, token_delta, post_balance = changed[0]
        if not stress.trade_like_wallet_event(float(token_delta), float(sol_delta)):
            counts["non_trade_token_movements_ignored"] += 1
            continue
        events.append(
            {
                "signature": row["signature"],
                "slot": tx.get("slot") or row.get("slot"),
                "block_time": int(tx.get("blockTime") or row.get("blockTime") or 0),
                "mint": mint,
                "token_delta": float(token_delta),
                "post_token_balance": float(post_balance),
                "sol_delta": float(sol_delta),
                "fee_lamports": int(meta.get("fee") or 0),
            }
        )
    events.sort(key=lambda row: (row["block_time"], row.get("slot") or 0))
    return events, counts


def positions_from_events(events: list[Mapping[str, Any]], cutoff: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    open_positions: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    reentries = 0
    orphan_sells = 0
    for event in events:
        mint = str(event["mint"])
        delta = finite(event["token_delta"])
        if delta > 0:
            cost = max(0.0, -finite(event["sol_delta"]))
            if cost <= 0.0005:
                continue
            if mint in open_positions:
                reentries += 1
                row = open_positions[mint]
                row["tokens"] += delta
                row["cost_sol"] += cost
                row["buy_count"] += 1
            else:
                open_positions[mint] = {
                    "mint": mint,
                    "entry_time": int(event["block_time"]),
                    "entry_slot": event.get("slot"),
                    "tokens": delta,
                    "sold": 0.0,
                    "cost_sol": cost,
                    "proceeds_sol": 0.0,
                    "first_partial_fraction": None,
                    "sell_count": 0,
                    "buy_count": 1,
                }
            continue
        row = open_positions.get(mint)
        if row is None:
            orphan_sells += 1
            continue
        proceeds = max(0.0, finite(event["sol_delta"]))
        if proceeds <= 0.0005:
            continue
        sold = min(row["tokens"], max(0.0, -delta))
        if row["first_partial_fraction"] is None and row["tokens"] > 0:
            row["first_partial_fraction"] = sold / row["tokens"]
        row["sold"] += sold
        row["proceeds_sol"] += proceeds
        row["sell_count"] += 1
        if (
            finite(event["post_token_balance"]) <= max(1e-6, row["tokens"] * 1e-7)
            or row["sold"] >= row["tokens"] * 0.995
        ):
            row["exit_time"] = int(event["block_time"])
            row["exit_slot"] = event.get("slot")
            row["hold_ms"] = max(0, (row["exit_time"] - row["entry_time"]) * 1000)
            row["pnl_sol"] = row["proceeds_sol"] - row["cost_sol"]
            if row["entry_time"] >= cutoff:
                closed.append(dict(row))
            open_positions.pop(mint, None)
    return closed, {
        "reentries": reentries,
        "orphan_sells": orphan_sells,
        "open_positions_after_reconstruction": len(open_positions),
    }


def summarize(positions: list[Mapping[str, Any]], aux: Mapping[str, Any], hours: float) -> dict[str, Any]:
    wins = [row for row in positions if finite(row.get("pnl_sol")) > 0]
    losses = [row for row in positions if finite(row.get("pnl_sol")) <= 0]
    pnls = [finite(row.get("pnl_sol")) for row in positions]
    gains = sum(value for value in pnls if value > 0)
    loss_sum = abs(sum(value for value in pnls if value <= 0))
    holds = [finite(row.get("hold_ms")) for row in positions]
    costs = [finite(row.get("cost_sol")) for row in positions]
    intervals = []
    for row in positions:
        intervals.append((int(row["entry_time"]), 1))
        intervals.append((int(row["exit_time"]), -1))
    concurrency = maximum = 0
    for _, delta in sorted(intervals, key=lambda x: (x[0], x[1])):
        concurrency += delta
        maximum = max(maximum, concurrency)
    return {
        "closed_positions": len(positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": ratio(len(wins), len(positions)),
        "net_pnl_sol": sum(pnls),
        "expectancy_sol": ratio(sum(pnls), len(positions)),
        "profit_factor": gains / loss_sum if loss_sum > 1e-12 else (float("inf") if gains > 0 else 0.0),
        "trades_per_hour": ratio(len(positions), hours),
        "median_hold_ms": statistics.median(holds) if holds else None,
        "p90_hold_ms": pct(holds, 0.90),
        "exited_within_2s_fraction": ratio(sum(x <= 2000 for x in holds), len(holds)),
        "exited_within_5s_fraction": ratio(sum(x <= 5000 for x in holds), len(holds)),
        "exited_within_10s_fraction": ratio(sum(x <= 10000 for x in holds), len(holds)),
        "losers_exited_within_2s_fraction": ratio(sum(finite(x.get("hold_ms")) <= 2000 for x in losses), len(losses)),
        "losers_exited_within_5s_fraction": ratio(sum(finite(x.get("hold_ms")) <= 5000 for x in losses), len(losses)),
        "first_partial_20pct_count": sum(
            row.get("first_partial_fraction") is not None
            and abs(finite(row.get("first_partial_fraction")) - 0.20) <= 0.03
            for row in positions
        ),
        "first_partial_30pct_count": sum(
            row.get("first_partial_fraction") is not None
            and abs(finite(row.get("first_partial_fraction")) - 0.30) <= 0.03
            for row in positions
        ),
        "reentries": int(aux.get("reentries") or 0),
        "max_concurrent_positions": maximum,
        "entry_size_sol": {
            "median": statistics.median(costs) if costs else None,
            "mean": statistics.fmean(costs) if costs else None,
            "p90": pct(costs, 0.90),
            "max": max(costs) if costs else None,
        },
    }


def v12_summary(paper: Mapping[str, Any], shadow: Mapping[str, Any]) -> dict[str, Any]:
    ledger = list(paper.get("ledger") or [])
    metrics = paper.get("metrics") or {}
    holds = [
        max(0.0, (int(row.get("exit_ns") or 0) - int(row.get("fill_ns") or 0)) / 1e6)
        for row in ledger
    ]
    entries = [finite(row.get("entry_cost_sol")) for row in ledger]
    creators = shadow.get("creator_concentration") or {}
    windows = int(metrics.get("capture_windows") or 0)
    launches = windows * 3000
    return {
        "closed_positions": int(metrics.get("closed_trades") or len(ledger)),
        "wins": int(metrics.get("wins") or 0),
        "losses": int(metrics.get("losses") or 0),
        "win_rate": finite(metrics.get("win_rate")),
        "net_pnl_sol": finite(metrics.get("net_pnl_sol")),
        "expectancy_sol": ratio(finite(metrics.get("net_pnl_sol")), len(ledger)),
        "profit_factor": finite(metrics.get("profit_factor")),
        "max_drawdown_fraction": finite(metrics.get("maximum_closed_equity_drawdown_fraction")),
        "capture_windows": windows,
        "launches_observed": launches,
        "trade_rate_per_launch": ratio(len(ledger), launches),
        "median_hold_ms": statistics.median(holds) if holds else None,
        "exited_within_2s_fraction": ratio(sum(x <= 2000 for x in holds), len(holds)),
        "losers_exited_within_2s_fraction": ratio(
            sum(
                max(0.0, (int(row.get("exit_ns") or 0) - int(row.get("fill_ns") or 0)) / 1e6) <= 2000
                for row in ledger if finite(row.get("pnl_sol")) <= 0
            ),
            sum(finite(row.get("pnl_sol")) <= 0 for row in ledger),
        ),
        "entry_size_sol": {
            "median": statistics.median(entries) if entries else None,
            "mean": statistics.fmean(entries) if entries else None,
            "max": max(entries) if entries else None,
        },
        "max_concurrent_positions": 2,
        "configured_first_partial_fraction": 0.30,
        "top_creator_trade_share": finite(creators.get("top_creator_trade_share")),
        "effective_creator_count": finite(creators.get("effective_creator_count")),
        "without_top_creator": creators.get("without_top_creator"),
        "mints": [str(row.get("mint")) for row in ledger],
    }


def render(result: Mapping[str, Any]) -> str:
    e4 = result["e4_48h"]
    v12 = result["v12"]
    overlap = result["mint_overlap"]
    lines = [
        "# E4 last-48h vs V12 Pre-Armed",
        "",
        "## E4 last 48 hours",
        "",
        f"- Trades: {e4['closed_positions']} ({e4['wins']}W/{e4['losses']}L)",
        f"- WR: {(e4['win_rate'] or 0):.1%}",
        f"- Net PnL: {e4['net_pnl_sol']:.4f} SOL",
        f"- PF: {e4['profit_factor']:.2f}",
        f"- Expectancy: {(e4['expectancy_sol'] or 0):.4f} SOL/trade",
        f"- Median hold: {e4['median_hold_ms']} ms",
        f"- <=2s exits: {(e4['exited_within_2s_fraction'] or 0):.1%}",
        f"- Losing trades <=2s: {(e4['losers_exited_within_2s_fraction'] or 0):.1%}",
        f"- Median entry: {(e4['entry_size_sol']['median'] or 0):.4f} SOL",
        f"- Reentries: {e4['reentries']}; max concurrency: {e4['max_concurrent_positions']}",
        "",
        "## Current V12 causal sample",
        "",
        f"- Trades: {v12['closed_positions']} ({v12['wins']}W/{v12['losses']}L)",
        f"- WR: {v12['win_rate']:.1%}",
        f"- Net PnL: {v12['net_pnl_sol']:.4f} SOL",
        f"- PF: {v12['profit_factor']:.2f}",
        f"- Expectancy: {(v12['expectancy_sol'] or 0):.4f} SOL/trade",
        f"- Median hold: {v12['median_hold_ms']} ms",
        f"- <=2s exits: {(v12['exited_within_2s_fraction'] or 0):.1%}",
        f"- Losing trades <=2s: {(v12['losers_exited_within_2s_fraction'] or 0):.1%}",
        f"- Median entry: {(v12['entry_size_sol']['median'] or 0):.4f} SOL",
        f"- Selection: {v12['closed_positions']}/{v12['launches_observed']} launches "
        f"({(v12['trade_rate_per_launch'] or 0):.3%})",
        f"- Top creator share: {v12['top_creator_trade_share']:.1%}",
        "",
        "## Direct overlap",
        "",
        f"- Same mints traded by both: {overlap['count']}",
        f"- Mints: {', '.join(overlap['mints']) or 'none'}",
        "",
    ]
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    now = int(time.time())
    cutoff = now - args.hours * 3600
    async with stress.RpcPool(stress.DEFAULT_HTTP_RPCS, timeout=12) as rpc:
        sigs = await signatures_since(rpc, cutoff)
        fetched = await fetch_transactions(rpc, sigs)
        events, event_counts = wallet_events(fetched)
        positions, aux = positions_from_events(events, cutoff)
        e4 = summarize(positions, aux, float(args.hours))
        rpc_errors = rpc.errors[-50:]
    paper = json.loads(args.v12_paper.read_text())
    shadow = json.loads(args.v12_shadow.read_text())
    v12 = v12_summary(paper, shadow)
    e4_mints = {str(row["mint"]) for row in positions}
    overlap_mints = sorted(e4_mints & set(v12["mints"]))
    result = {
        "version": "v12-e4-recent-comparison-v1",
        "generated_epoch": now,
        "window_hours": args.hours,
        "cutoff_epoch": cutoff,
        "wallet": stress.E4_WALLET,
        "filters": {
            "quote_asset_mints": sorted(stress.QUOTE_ASSET_MINTS),
            "requires_material_sol_flow": True,
            "minimum_trade_sol_flow": 0.0005,
        },
        "e4_48h": e4,
        "v12": v12,
        "mint_overlap": {"count": len(overlap_mints), "mints": overlap_mints},
        "e4_positions": positions,
        "event_diagnostics": event_counts,
        "reconstruction_diagnostics": aux,
        "signatures_scanned": len(sigs),
        "wallet_trade_events": len(events),
        "rpc_errors": rpc_errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    args.report.write_text(render(result) + "\n", encoding="utf-8")
    print(json.dumps({
        "e4": e4,
        "v12_trades": v12["closed_positions"],
        "overlap": result["mint_overlap"],
        "signatures_scanned": len(sigs),
    }, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=48)
    p.add_argument("--v12-paper", type=Path, required=True)
    p.add_argument("--v12-shadow", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
