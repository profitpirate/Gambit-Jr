#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"


def load_base():
    path = Path(__file__).with_name("e4_oracle_selection_research.py")
    name = "e4_oracle_selection_research_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_base()


def event_price_sol(event: Mapping[str, Any]) -> float | None:
    virtual_sol = base.finite(event.get("virtual_sol_reserves"))
    virtual_tokens = base.finite(event.get("virtual_token_reserves"))
    if not virtual_sol or not virtual_tokens:
        return None
    return (virtual_sol / 1_000_000_000) / (virtual_tokens / 1_000_000)


async def discover_fixture(args: argparse.Namespace) -> Path:
    urls = [
        value.strip()
        for value in os.getenv("E4_RESEARCH_RPC_URLS", "").split(",")
        if value.strip()
    ]
    helius_key = os.getenv("HELIUS_API_KEY", "").strip()
    if helius_key:
        urls.insert(0, f"https://mainnet.helius-rpc.com/?api-key={helius_key}")
    if not urls:
        urls = list(base.DEFAULT_RPCS)

    async with base.RpcPool(
        urls,
        concurrency=args.concurrency,
        timeout=args.timeout,
    ) as rpc:
        signatures = await rpc.signatures(
            E4_WALLET,
            limit=args.wallet_signatures,
        )
        print(
            json.dumps(
                {
                    "stage": "wallet_signatures",
                    "count": len(signatures),
                }
            ),
            flush=True,
        )
        transactions = await asyncio.gather(
            *(rpc.transaction(str(row["signature"])) for row in signatures)
        )

    events = []
    for metadata, transaction in zip(signatures, transactions):
        signature = str(metadata["signature"])
        for event in base.decoded_events(transaction, signature):
            if event.get("anchor_event") != "TradeEvent":
                continue
            if str(event.get("user") or "") != E4_WALLET:
                continue
            events.append(event)
    events.sort(
        key=lambda event: (
            int(event.get("block_time") or 0),
            int(event.get("slot") or 0),
            int(event.get("event_index") or 0),
        )
    )

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event.get("mint") or "")].append(event)
    positions = []
    sol_usd = float(os.getenv("E4_SOL_USD_FALLBACK", "150"))
    for mint, rows in grouped.items():
        buys = [row for row in rows if row.get("is_buy")]
        sells = [row for row in rows if not row.get("is_buy")]
        if not buys or not mint:
            continue
        buy = buys[0]
        buy_sol = float(buy.get("sol_amount") or 0) / 1_000_000_000
        buy_tokens = float(buy.get("token_amount") or 0) / 1_000_000
        sell_sol = sum(
            float(row.get("sol_amount") or 0) / 1_000_000_000 for row in sells
        )
        first_sell = sells[0] if sells else None
        price_sol = event_price_sol(buy)
        entry_fdv = price_sol * 1_000_000_000 * sol_usd if price_sol else None
        positions.append(
            {
                "mint": mint,
                "buy_signature": buy["signature"],
                "buy_block_time": int(buy.get("block_time") or 0),
                "buy_time": int(buy.get("block_time") or 0),
                "buy_sol": buy_sol,
                "entry_fdv_usd_approx": entry_fdv,
                "gross_pnl_sol": sell_sol - buy_sol,
                "gross_roi": (sell_sol - buy_sol) / buy_sol if buy_sol else None,
                "first_sell_fraction": (
                    (float(first_sell.get("token_amount") or 0) / 1_000_000)
                    / buy_tokens
                    if first_sell and buy_tokens
                    else None
                ),
                "first_sell_delay_seconds": (
                    int(first_sell.get("block_time") or 0)
                    - int(buy.get("block_time") or 0)
                    if first_sell
                    else None
                ),
                "hold_seconds": (
                    int(sells[-1].get("block_time") or 0)
                    - int(buy.get("block_time") or 0)
                    if sells
                    else None
                ),
                "buy_programs": [],
                "sell_signatures": [
                    str(row.get("signature") or "") for row in sells
                ],
            }
        )
    positions.sort(key=lambda row: int(row["buy_block_time"]))
    path = args.output.with_name(
        args.output.stem + "-discovered-fixture.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "wallet": E4_WALLET,
                "source": "live public Solana RPC wallet history",
                "caveat": "Bounded signature window; not lifetime performance.",
                "positions": positions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "stage": "positions_reconstructed",
                "positions": len(positions),
                "fixture": str(path),
            }
        ),
        flush=True,
    )
    if not positions:
        raise RuntimeError("no E4 Pump positions reconstructed from wallet window")
    return path


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    args.fixture = await discover_fixture(args)
    return await base.run(args)


def parser() -> argparse.ArgumentParser:
    value = base.parser()
    value.add_argument("--wallet-signatures", type=int, default=400)
    return value


if __name__ == "__main__":
    arguments = parser().parse_args()
    result = asyncio.run(main_async(arguments))
    print(
        json.dumps(
            {
                "summary": result["summary"],
                "diagnostics": result["diagnostics"],
            },
            indent=2,
        )
    )
