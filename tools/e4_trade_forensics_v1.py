#!/usr/bin/env python3
"""Read-only E4 same-window trade-leg forensic reconstruction.

Preserves individual BUY/SELL legs that e4_same_window_backfill normally
aggregates into positions. Never signs or broadcasts transactions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import e4_same_window_backfill as base


def atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


async def main_async(args: argparse.Namespace) -> int:
    async with base.Rpc() as rpc:
        sigs = await base.signatures(rpc, int(args.start_epoch), int(args.end_epoch), int(args.max_signatures))
        trades, skipped = await base.fetch_transactions(rpc, sigs)
        econ = base.reconstruct(trades)
        closed_mints = {r["mint"] for r in econ["closed"]}
        open_mints = {r["mint"] for r in econ["open"]}
        legs = [r for r in trades if r["mint"] in closed_mints or r["mint"] in open_mints]
        payload = {
            "version": "e4-trade-forensics-v1",
            "benchmark_only": True,
            "wallet": base.E4_WALLET,
            "start_epoch": int(args.start_epoch),
            "end_epoch": int(args.end_epoch),
            "wallet_signatures_in_window": len(sigs),
            "parsed_trade_transactions": len(trades),
            "unparsed_or_nontrade_transactions": skipped,
            "rpc_recent_errors": rpc.errors[-20:],
            "economics": econ,
            "trade_legs": legs,
        }
    atomic(args.output, payload)
    print(json.dumps({
        "version": payload["version"],
        "closed": econ["closed_positions"],
        "open": econ["open_positions"],
        "legs": len(legs),
        "pnl_sol": econ["net_realized_pnl_sol"],
        "win_rate": econ["win_rate"],
        "rpc_errors": len(payload["rpc_recent_errors"]),
    }, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start-epoch", type=int, required=True)
    p.add_argument("--end-epoch", type=int, required=True)
    p.add_argument("--max-signatures", type=int, default=5000)
    p.add_argument("--output", type=Path, required=True)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
