#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL = "So11111111111111111111111111111111111111112"
PUMP_TOKEN = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
JITO_TIPS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
NOZOMI_PREFIXES = ("noz", "TEMP")
DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://solana-mainnet.api.onfinality.io/public",
    "https://api.mainnet-beta.solana.com",
)


def host(uri: str | None) -> str:
    if not uri:
        return "unknown"
    parsed = urlparse(uri)
    return (parsed.netloc or "unknown").lower()


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class RpcPool:
    def __init__(self, urls: Sequence[str], timeout: float, concurrency: int):
        self.urls = tuple(dict.fromkeys(urls))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.limit = asyncio.Semaphore(concurrency)
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
        last: Exception | None = None
        async with self.limit:
            for attempt in range(max(1, retries) * len(self.urls)):
                url = self.urls[(self.cursor + attempt) % len(self.urls)]
                self.request_id += 1
                started = time.perf_counter()
                try:
                    async with self.session.post(
                        url,
                        json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
                    ) as response:
                        text = await response.text()
                        if response.status == 429 or response.status >= 500:
                            raise RuntimeError(f"HTTP {response.status}")
                        if response.status >= 400:
                            raise RuntimeError(f"HTTP {response.status}: {text[:120]}")
                        payload = json.loads(text)
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        return payload.get("result")
                except Exception as exc:
                    last = exc
                    self.errors.append(
                        f"{method}@{url}:{type(exc).__name__}:{exc}:{time.perf_counter()-started:.2f}s"
                    )
                    await asyncio.sleep(min(0.5, 0.04 * (attempt + 1) + random.random() * 0.03))
        raise RuntimeError(f"{method} exhausted endpoints: {last}")


async def transaction(rpc: RpcPool, signature: str) -> Mapping[str, Any] | None:
    try:
        value = await rpc.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return value if isinstance(value, Mapping) else None
    except Exception:
        return None


def account_keys(tx: Mapping[str, Any]) -> list[str]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = [
        str(item.get("pubkey")) if isinstance(item, Mapping) else str(item)
        for item in message.get("accountKeys") or []
    ]
    loaded = ((tx.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(item) for item in loaded.get("writable") or [])
    keys.extend(str(item) for item in loaded.get("readonly") or [])
    return keys


def token_totals(rows: Sequence[Mapping[str, Any]], owner: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows or []:
        if str(row.get("owner") or "") != owner:
            continue
        mint = str(row.get("mint") or "")
        token = row.get("uiTokenAmount") or {}
        value = token.get("uiAmountString", token.get("uiAmount", 0))
        if mint:
            totals[mint] += float(value or 0)
    return totals


def wallet_buy(row: Mapping[str, Any], tx: Mapping[str, Any]) -> dict[str, Any] | None:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = account_keys(tx)
    if E4_WALLET not in keys:
        return None
    index = keys.index(E4_WALLET)
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if index >= len(pre_balances) or index >= len(post_balances):
        return None
    pre_lamports = pre_balances[index]
    post_lamports = post_balances[index]
    pre = token_totals(meta.get("preTokenBalances") or [], E4_WALLET)
    post = token_totals(meta.get("postTokenBalances") or [], E4_WALLET)
    changed = []
    for mint in set(pre) | set(post):
        if mint in {WSOL, PUMP_TOKEN}:
            continue
        delta = post.get(mint, 0) - pre.get(mint, 0)
        if delta > max(1e-8, abs(pre.get(mint, 0)) * 1e-10):
            changed.append((mint, delta))
    if len(changed) != 1:
        return None
    mint, tokens = changed[0]
    return {
        "mint": mint,
        "signature": str(row["signature"]),
        "slot": int(tx.get("slot") or row.get("slot") or 0),
        "block_time": int(tx.get("blockTime") or row.get("blockTime") or 0),
        "tokens": tokens,
        "sol_cost": max(0.0, (pre_lamports - post_lamports) / 1_000_000_000),
        "programs": sorted(
            key
            for key in keys
            if key in {
                PUMP_PROGRAM_ID,
                "AZiv8U5cEAe9CXEYrYwQvobhBzBkU7Jxw3W5runiJoiP",
                "8QXqiJcCwCrYh7kTyxYB4FP3b4wQmgphxBqD6ZKqd1KX",
            }
        ),
        "tx": tx,
    }


def parsed_transfers(tx: Mapping[str, Any]) -> list[dict[str, Any]]:
    meta = tx.get("meta") or {}
    message = ((tx.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    transfers = []
    for instruction in instructions:
        parsed = instruction.get("parsed") if isinstance(instruction, Mapping) else None
        info = parsed.get("info") if isinstance(parsed, Mapping) else None
        if not isinstance(info, Mapping):
            continue
        destination = str(info.get("destination") or "")
        lamports = finite(info.get("lamports"))
        if destination and lamports is not None:
            transfers.append({"destination": destination, "sol": lamports / 1_000_000_000})
    return transfers


async def entry_context(rpc: RpcPool, entry: dict[str, Any], before_limit: int) -> dict[str, Any]:
    try:
        older = await rpc.call(
            "getSignaturesForAddress",
            [entry["mint"], {"before": entry["signature"], "limit": before_limit}],
        )
    except Exception:
        older = []
    rows = list(older or [])
    rows.append(
        {
            "signature": entry["signature"],
            "slot": entry["slot"],
            "blockTime": entry["block_time"],
        }
    )
    fetched = await asyncio.gather(
        *(transaction(rpc, str(row["signature"])) for row in rows)
    )
    decoded: list[tuple[int, int, str, dict[str, Any]]] = []
    for order, (row, tx) in enumerate(zip(rows, fetched)):
        if not tx:
            continue
        logs = list((tx.get("meta") or {}).get("logMessages") or [])
        slot = int(tx.get("slot") or row.get("slot") or 0)
        for event_index, event in enumerate(anchor_events_from_logs(logs, PUMP_PROGRAM_ID)):
            if str(event.get("mint") or "") == entry["mint"]:
                decoded.append((slot, order * 100 + event_index, str(row["signature"]), event))
    decoded.sort(key=lambda item: (item[0], -item[1]))
    create = next((event for _, _, _, event in decoded if event.get("anchor_event") == "CreateEvent"), None)
    creator = str((create or {}).get("creator") or (create or {}).get("user") or "") or None
    trades = []
    for slot, _, signature, event in decoded:
        if event.get("anchor_event") != "TradeEvent":
            continue
        if slot > entry["slot"] or (
            signature == entry["signature"] and str(event.get("user")) == E4_WALLET
        ):
            continue
        trades.append(
            {
                "slot": slot,
                "signature": signature,
                "user": str(event.get("user") or ""),
                "is_buy": bool(event.get("is_buy")),
                "sol": (finite(event.get("sol_amount")) or 0) / 1_000_000_000,
            }
        )
    buys = [trade for trade in trades if trade["is_buy"]]
    noncreator = [trade for trade in buys if trade["user"] not in {creator, E4_WALLET}]
    tips = parsed_transfers(entry["tx"])
    create_slot = next(
        (slot for slot, _, _, event in decoded if event.get("anchor_event") == "CreateEvent"),
        None,
    )
    return {
        "creator": creator,
        "name": (create or {}).get("name"),
        "symbol": (create or {}).get("symbol"),
        "uri": (create or {}).get("uri"),
        "metadata_host": host((create or {}).get("uri")),
        "create_slot": create_slot,
        "entry_slot": entry["slot"],
        "slot_delay": entry["slot"] - (create_slot if create_slot is not None else entry["slot"]),
        "creator_buy_sol_before_entry": sum(
            trade["sol"] for trade in buys if trade["user"] == creator
        ),
        "noncreator_buyers_before_entry": len(
            {trade["user"] for trade in noncreator if trade["user"]}
        ),
        "noncreator_sol_before_entry": sum(trade["sol"] for trade in noncreator),
        "sells_before_entry": sum(not trade["is_buy"] for trade in trades),
        "jito_tip_sol": sum(
            tip["sol"] for tip in tips if tip["destination"] in JITO_TIPS
        ),
        "nozomi_tip_sol": sum(
            tip["sol"]
            for tip in tips
            if tip["destination"].startswith(NOZOMI_PREFIXES)
        ),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    urls = tuple(item.strip() for item in args.rpc_urls.split(",") if item.strip())
    async with RpcPool(urls, args.timeout, args.concurrency) as rpc:
        signatures = await rpc.call(
            "getSignaturesForAddress",
            [E4_WALLET, {"limit": min(1000, args.signatures)}],
        )
        rows = list(signatures or [])
        txs = await asyncio.gather(*(transaction(rpc, str(row["signature"])) for row in rows))
        entries = [
            value
            for row, tx in zip(rows, txs)
            if tx and (value := wallet_buy(row, tx))
        ]
        entries.sort(key=lambda row: (row["slot"], row["signature"]), reverse=True)
        unique = []
        seen = set()
        for entry in entries:
            if entry["mint"] in seen:
                continue
            seen.add(entry["mint"])
            unique.append(entry)
            if len(unique) >= args.positions:
                break
        contexts = []
        for index in range(0, len(unique), args.concurrency):
            batch = unique[index : index + args.concurrency]
            results = await asyncio.gather(
                *(entry_context(rpc, entry, args.before_limit) for entry in batch)
            )
            contexts.extend(
                {**entry, **context, "tx": None}
                for entry, context in zip(batch, results)
            )
            print(
                json.dumps(
                    {
                        "progress": len(contexts),
                        "target": len(unique),
                        "rpc_errors": len(rpc.errors),
                    }
                ),
                flush=True,
            )
        rpc_errors = rpc.errors[-100:]

    creators = Counter(str(row["creator"]) for row in contexts if row.get("creator"))
    hosts = Counter(row["metadata_host"] for row in contexts)
    wrappers = Counter(
        "AZiv"
        if any(program.startswith("AZiv") for program in row["programs"])
        else "direct_pump"
        for row in contexts
    )
    return {
        "report_version": "e4-creator-forensics-v1",
        "wallet_signatures_examined": len(rows),
        "unique_entries": len(contexts),
        "entries": contexts,
        "metadata_hosts": dict(hosts),
        "unique_creators": len(creators),
        "repeated_creators": {
            creator: count for creator, count in creators.items() if count > 1
        },
        "entry_wrapper_distribution": dict(wrappers),
        "median_slot_delay": statistics.median(
            [row["slot_delay"] for row in contexts if row.get("create_slot") is not None]
        )
        if any(row.get("create_slot") is not None for row in contexts)
        else None,
        "creator_only_or_near_creator_only_entries": sum(
            row["noncreator_buyers_before_entry"] <= 1 for row in contexts
        ),
        "j7_metadata_entries": hosts.get("metadata.j7tracker.io", 0),
        "rpc_errors": rpc_errors,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Lean creator/source forensics for actual E4 entries"
    )
    value.add_argument("--signatures", type=int, default=300)
    value.add_argument("--positions", type=int, default=30)
    value.add_argument("--before-limit", type=int, default=20)
    value.add_argument("--concurrency", type=int, default=8)
    value.add_argument("--timeout", type=float, default=5.0)
    value.add_argument("--rpc-urls", default=",".join(DEFAULT_RPCS))
    value.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "entries": report["unique_entries"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
