#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

from memecoin_bot.realtime.pumpfun import (
    PUMP_PROGRAM_ID,
    decode_account_data,
    decode_bonding_curve_account,
)

DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)


class RpcPool:
    def __init__(self, urls: list[str], timeout: float = 6.0, concurrency: int = 8):
        self.urls = list(dict.fromkeys(urls))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0
        self.errors: list[str] = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *_: Any):
        if self.session:
            await self.session.close()

    async def call(self, method: str, params: list[Any], retries: int = 2) -> Any:
        assert self.session is not None
        async with self.sem:
            last: Exception | None = None
            attempts = max(1, retries) * max(1, len(self.urls))
            for offset in range(attempts):
                url = self.urls[(self.cursor + offset) % len(self.urls)]
                self.request_id += 1
                try:
                    async with self.session.post(
                        url,
                        json={
                            "jsonrpc": "2.0",
                            "id": self.request_id,
                            "method": method,
                            "params": params,
                        },
                    ) as response:
                        text = await response.text()
                        if response.status == 429 or response.status >= 500:
                            raise RuntimeError(f"HTTP {response.status}")
                        payload = json.loads(text)
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        return payload.get("result")
                except Exception as exc:
                    last = exc
                    self.errors.append(f"{method}@{url}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(0.8, 0.05 * (offset + 1)))
            raise RuntimeError(f"{method} failed: {last}")


def load_winners(pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(Path().glob(pattern)):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                mint = str(row.get("mint") or "").strip()
                if not mint or mint in seen:
                    continue
                seen.add(mint)
                rows.append(
                    {
                        "mint": mint,
                        "gross_pnl_sol": float(row["gross_pnl_sol"]),
                        "entry_sol": float(row["entry_sol"]),
                    }
                )
    return rows


def bonding_curve_for(mint: str) -> str:
    mint_key = Pubkey.from_string(mint)
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    curve, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(mint_key)], program)
    return str(curve)


async def resolve_creator(rpc: RpcPool, row: dict[str, Any]) -> dict[str, Any]:
    mint = row["mint"]
    curve = bonding_curve_for(mint)
    try:
        account = await rpc.call(
            "getAccountInfo",
            [curve, {"encoding": "base64", "commitment": "confirmed"}],
            retries=3,
        )
        value = (account or {}).get("value") if isinstance(account, dict) else None
        if not value:
            raise RuntimeError("bonding curve account unavailable")
        decoded = decode_bonding_curve_account(decode_account_data(value.get("data")))
        creator = str(decoded.get("creator") or "")
        if not creator or creator == "11111111111111111111111111111111":
            raise RuntimeError("creator missing from curve")
        return {
            **row,
            "bonding_curve": curve,
            "creator": creator,
            "curve_layout": decoded.get("account_layout"),
            "quote_mint": decoded.get("quote_mint"),
            "resolved": True,
        }
    except Exception as exc:
        return {
            **row,
            "bonding_curve": curve,
            "creator": None,
            "resolved": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def creator_score(wins: int, pnl: float, deployed: float) -> float:
    # This is an identity prior, not a guaranteed future-win probability.
    # A creator with one observed E4 winner is deliberately strong enough for
    # the immediate-repeat family, while repeated winners increase confidence.
    repeat = min(0.10, 0.035 * math.log1p(max(0, wins - 1)))
    profitability = min(0.06, 0.02 * math.log1p(max(0.0, pnl)))
    roi = pnl / deployed if deployed > 0 else 0.0
    quality = min(0.04, max(0.0, roi) * 0.02)
    return min(0.98, 0.80 + repeat + profitability + quality)


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        creator = row.get("creator")
        if creator:
            grouped[str(creator)].append(row)
        else:
            unresolved.append(row)

    creators: dict[str, Any] = {}
    for creator, items in grouped.items():
        pnl = sum(float(item["gross_pnl_sol"]) for item in items)
        deployed = sum(float(item["entry_sol"]) for item in items)
        sizes = [float(item["entry_sol"]) for item in items]
        creators[creator] = {
            "score": round(creator_score(len(items), pnl, deployed), 6),
            "e4_observed_wins": len(items),
            "e4_gross_pnl_sol": round(pnl, 9),
            "e4_deployed_sol": round(deployed, 9),
            "median_e4_entry_sol": round(statistics.median(sizes), 9),
            "max_e4_entry_sol": round(max(sizes), 9),
            "winner_mints": [item["mint"] for item in items],
            "instant_repeat_candidate": True,
        }

    return {
        "version": "e4-winning-creators-v1",
        "semantics": (
            "Creators of historically gross-profitable E4 positions. Membership is an identity prior, "
            "not permission to ignore current-chain safety checks."
        ),
        "resolved_winner_mints": sum(len(items) for items in grouped.values()),
        "unresolved_winner_mints": len(unresolved),
        "unique_winning_creators": len(creators),
        "creators": dict(sorted(creators.items(), key=lambda pair: (-pair[1]["e4_gross_pnl_sol"], pair[0]))),
        "unresolved": unresolved,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    winners = load_winners(args.inputs)
    urls = [part.strip() for part in args.rpc_urls.split(",") if part.strip()]
    async with RpcPool(urls, timeout=args.timeout, concurrency=args.concurrency) as rpc:
        resolved: list[dict[str, Any]] = []
        for start in range(0, len(winners), args.batch_size):
            batch = winners[start : start + args.batch_size]
            resolved.extend(await asyncio.gather(*(resolve_creator(rpc, row) for row in batch)))
            print(
                json.dumps(
                    {
                        "progress": len(resolved),
                        "target": len(winners),
                        "resolved": sum(bool(row.get("resolved")) for row in resolved),
                        "rpc_errors": len(rpc.errors),
                    }
                ),
                flush=True,
            )
        rpc_errors = rpc.errors[-100:]
    result = aggregate(resolved)
    result["input_winner_mints"] = len(winners)
    result["rpc_errors"] = rpc_errors
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Build E4 winning-creator identity registry")
    value.add_argument("--inputs", default="models/e4/winning-mints-*.tsv")
    value.add_argument("--output", type=Path, default=Path("models/e4/e4-winning-creators.json"))
    value.add_argument("--rpc-urls", default=os.getenv("E4_REGISTRY_RPC_URLS", ",".join(DEFAULT_RPCS)))
    value.add_argument("--timeout", type=float, default=6.0)
    value.add_argument("--concurrency", type=int, default=8)
    value.add_argument("--batch-size", type=int, default=24)
    return value


def main() -> int:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "winning_mints": report["input_winner_mints"],
                "resolved_mints": report["resolved_winner_mints"],
                "unique_winning_creators": report["unique_winning_creators"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
