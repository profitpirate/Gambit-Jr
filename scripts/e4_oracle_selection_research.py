#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTgVwvGrkPR1ZfSC8cDPKt1o",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
DEFAULT_RPCS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percentile(values: Sequence[float], q: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    location = (len(clean) - 1) * q
    lower = math.floor(location)
    upper = math.ceil(location)
    if lower == upper:
        return clean[lower]
    weight = location - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(clean),
        "min": min(clean) if clean else None,
        "median": statistics.median(clean) if clean else None,
        "p75": percentile(clean, 0.75),
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "max": max(clean) if clean else None,
        "mean": statistics.fmean(clean) if clean else None,
    }


class RpcPool:
    def __init__(self, urls: Sequence[str], *, concurrency: int, timeout: float):
        self.urls = tuple(dict.fromkeys(url for url in urls if url))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.session: aiohttp.ClientSession | None = None
        self.request_id = 0
        self.cursor = 0
        self.errors: list[str] = []
        self.latencies_ms: list[float] = []
        self.tx_cache: dict[str, Mapping[str, Any] | None] = {}

    async def __aenter__(self) -> "RpcPool":
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def call(self, method: str, params: list[Any], *, attempts: int = 5) -> Any:
        if not self.urls:
            raise RuntimeError("no E4 research RPC configured")
        assert self.session is not None
        last_error: Exception | None = None
        async with self.semaphore:
            for attempt in range(attempts):
                url = self.urls[(self.cursor + attempt) % len(self.urls)]
                self.request_id += 1
                started = time.perf_counter_ns()
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
                        latency = (time.perf_counter_ns() - started) / 1_000_000
                        if response.status == 429 or response.status >= 500:
                            raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
                        if response.status >= 400:
                            raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
                        payload = json.loads(text)
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        self.latencies_ms.append(latency)
                        return payload.get("result")
                except Exception as exc:
                    last_error = exc
                    self.errors.append(f"{method}@{url}: {exc}")
                    await asyncio.sleep(min(1.5, 0.1 * 2**attempt))
        raise RuntimeError(f"{method} exhausted RPC pool: {last_error}")

    async def signatures(
        self,
        address: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Mapping[str, Any]]:
        options: dict[str, Any] = {
            "limit": min(1_000, limit),
            "commitment": "confirmed",
        }
        if before:
            options["before"] = before
        result = await self.call("getSignaturesForAddress", [address, options])
        return [
            row
            for row in result or []
            if isinstance(row, Mapping) and row.get("err") is None
        ]

    async def transaction(self, signature: str) -> Mapping[str, Any] | None:
        if signature in self.tx_cache:
            return self.tx_cache[signature]
        try:
            result = await self.call(
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
            value = result if isinstance(result, Mapping) else None
        except Exception:
            value = None
        self.tx_cache[signature] = value
        return value


async def heartbeat(done: list[int], total: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        print(
            json.dumps(
                {
                    "heartbeat": True,
                    "completed": done[0],
                    "total": total,
                    "epoch": int(time.time()),
                }
            ),
            flush=True,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass


def program_ids(transaction: Mapping[str, Any] | None) -> list[str]:
    if not transaction:
        return []
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = []
    for value in message.get("accountKeys") or []:
        keys.append(
            str(value.get("pubkey")) if isinstance(value, Mapping) else str(value)
        )
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(value) for value in loaded.get("writable") or [])
    keys.extend(str(value) for value in loaded.get("readonly") or [])
    result: set[str] = set()
    instructions = list(message.get("instructions") or [])
    for group in (transaction.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    for instruction in instructions:
        if not isinstance(instruction, Mapping):
            continue
        explicit = instruction.get("programId")
        if explicit:
            result.add(str(explicit))
            continue
        index = instruction.get("programIdIndex")
        if isinstance(index, int) and 0 <= index < len(keys):
            result.add(keys[index])
    return sorted(result)


def jito_tip(transaction: Mapping[str, Any] | None) -> int:
    if not transaction:
        return 0
    message = ((transaction.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in (transaction.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    amount = 0
    for instruction in instructions:
        parsed = instruction.get("parsed") if isinstance(instruction, Mapping) else None
        info = parsed.get("info") if isinstance(parsed, Mapping) else None
        if not isinstance(info, Mapping):
            continue
        if str(info.get("destination") or "") in JITO_TIP_ACCOUNTS:
            amount += int(info.get("lamports") or 0)
    return amount


def decoded_events(
    transaction: Mapping[str, Any] | None,
    signature: str,
) -> list[dict[str, Any]]:
    if not transaction:
        return []
    logs = list((transaction.get("meta") or {}).get("logMessages") or [])
    slot = int(transaction.get("slot") or 0)
    block_time = int(transaction.get("blockTime") or 0)
    result = []
    for index, item in enumerate(anchor_events_from_logs(logs, PUMP_PROGRAM_ID)):
        row = dict(item)
        row.update(
            {
                "signature": signature,
                "slot": slot,
                "block_time": block_time,
                "event_index": index,
            }
        )
        result.append(row)
    return result


async def creator_history(
    rpc: RpcPool,
    creator: str,
    create_signature: str,
    limit: int,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if creator in cache:
        return cache[creator]
    signatures = await rpc.signatures(creator, before=create_signature, limit=limit)
    transactions = await asyncio.gather(
        *(rpc.transaction(str(row["signature"])) for row in signatures)
    )
    launches = []
    for metadata, transaction in zip(signatures, transactions):
        for event in decoded_events(transaction, str(metadata["signature"])):
            if event.get("anchor_event") != "CreateEvent":
                continue
            if str(event.get("creator") or event.get("user") or "") != creator:
                continue
            launches.append(
                {
                    "mint": event.get("mint"),
                    "signature": event["signature"],
                    "slot": event["slot"],
                    "block_time": event["block_time"],
                }
            )
    result = {
        "signatures_inspected": len(signatures),
        "prior_pump_launches_found": len({row["mint"] for row in launches}),
        "prior_launches": launches[:10],
    }
    cache[creator] = result
    return result


async def analyse_position(
    rpc: RpcPool,
    observed: Mapping[str, Any],
    *,
    prior_limit: int,
    creator_limit: int,
    creator_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    mint = str(observed["mint"])
    buy_signature = str(observed["buy_signature"])
    buy_transaction_task = asyncio.create_task(rpc.transaction(buy_signature))
    prior_rows = await rpc.signatures(mint, before=buy_signature, limit=prior_limit)
    prior_transactions = await asyncio.gather(
        *(rpc.transaction(str(row["signature"])) for row in prior_rows)
    )
    buy_transaction = await buy_transaction_task

    events = []
    for metadata, transaction in zip(prior_rows, prior_transactions):
        events.extend(decoded_events(transaction, str(metadata["signature"])))
    events.sort(
        key=lambda row: (
            int(row.get("slot") or 0),
            int(row.get("block_time") or 0),
            int(row.get("event_index") or 0),
        )
    )
    creates = [
        row
        for row in events
        if row.get("anchor_event") == "CreateEvent" and row.get("mint") == mint
    ]
    create = creates[0] if creates else None
    trades = [
        row
        for row in events
        if row.get("anchor_event") == "TradeEvent" and row.get("mint") == mint
    ]
    buys = [row for row in trades if row.get("is_buy")]
    sells = [row for row in trades if not row.get("is_buy")]
    creator = (
        str((create or {}).get("creator") or (create or {}).get("user") or "")
        or None
    )
    creator_buys = [
        row for row in buys if creator and str(row.get("user") or "") == creator
    ]
    external_buys = [
        row for row in buys if not creator or str(row.get("user") or "") != creator
    ]
    signature_counts = Counter(str(row.get("signature") or "") for row in buys)
    create_signature = str((create or {}).get("signature") or "")
    history = (
        await creator_history(
            rpc, creator, create_signature, creator_limit, creator_cache
        )
        if creator and create_signature and creator_limit > 0
        else {
            "signatures_inspected": 0,
            "prior_pump_launches_found": None,
            "prior_launches": [],
        }
    )
    entry_slot = int((buy_transaction or {}).get("slot") or 0)
    entry_time = int(
        (buy_transaction or {}).get("blockTime")
        or observed.get("buy_block_time")
        or 0
    )
    metadata_uri = str((create or {}).get("uri") or "")
    route_programs = program_ids(buy_transaction)

    return {
        **dict(observed),
        "research_ok": create is not None,
        "create_found": create is not None,
        "creator": creator,
        "create_signature": create_signature or None,
        "create_slot": int((create or {}).get("slot") or 0) or None,
        "create_block_time": int((create or {}).get("block_time") or 0) or None,
        "entry_slot": entry_slot or None,
        "entry_block_time": entry_time or None,
        "create_to_entry_slots": (
            entry_slot - int(create["slot"]) if create and entry_slot else None
        ),
        "create_to_entry_seconds": (
            entry_time - int(create["block_time"])
            if create and entry_time
            else None
        ),
        "entry_rank": len(buys) + 1,
        "pre_buy_count": len(buys),
        "pre_sell_count": len(sells),
        "pre_unique_buyers": len(
            {str(row.get("user") or "") for row in buys}
        ),
        "pre_buy_sol": sum(
            float(row.get("sol_amount") or 0) / 1_000_000_000 for row in buys
        ),
        "pre_sell_sol": sum(
            float(row.get("sol_amount") or 0) / 1_000_000_000 for row in sells
        ),
        "creator_buy_in_create_tx": bool(
            create_signature
            and any(
                str(row.get("signature") or "") == create_signature
                for row in creator_buys
            )
        ),
        "creator_buy_sol": sum(
            float(row.get("sol_amount") or 0) / 1_000_000_000
            for row in creator_buys
        ),
        "external_pre_buyers": len(
            {str(row.get("user") or "") for row in external_buys}
        ),
        "external_pre_buy_sol": sum(
            float(row.get("sol_amount") or 0) / 1_000_000_000
            for row in external_buys
        ),
        "pre_multi_buy_signature_events": sum(
            count for count in signature_counts.values() if count > 1
        ),
        "pre_max_buys_same_signature": max(signature_counts.values(), default=0),
        "metadata_uri": metadata_uri or None,
        "metadata_host": urlparse(metadata_uri).netloc.lower()
        if metadata_uri
        else None,
        "mayhem_mode": (create or {}).get("is_mayhem_mode"),
        "cashback_enabled": (create or {}).get("is_cashback_enabled"),
        "quote_mint": (create or {}).get("quote_mint"),
        "buy_route_programs": route_programs,
        "jito_tip_lamports": jito_tip(buy_transaction),
        "creator_history": history,
        "error": None,
    }


def sample_positions(
    rows: list[Mapping[str, Any]], count: int
) -> list[Mapping[str, Any]]:
    if count >= len(rows):
        return rows
    ordered = sorted(rows, key=lambda row: int(row.get("buy_block_time") or 0))
    chosen = set()
    for index in range(count):
        chosen.add(round(index * (len(ordered) - 1) / max(1, count - 1)))
    # Include the largest wins, losses and sizes because conviction tiers may
    # use different entry cohorts.
    for key, reverse in (
        ("gross_pnl_sol", True),
        ("gross_pnl_sol", False),
        ("buy_sol", True),
    ):
        for row in sorted(
            ordered,
            key=lambda value: float(value.get(key) or 0),
            reverse=reverse,
        )[:5]:
            chosen.add(ordered.index(row))
    return [ordered[index] for index in sorted(chosen)]


def feature_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("research_ok")]
    winners = [
        row for row in valid if float(row.get("gross_pnl_sol") or 0) > 0
    ]
    losses = [
        row for row in valid if float(row.get("gross_pnl_sol") or 0) <= 0
    ]

    def fraction(predicate) -> float | None:
        return (
            sum(bool(predicate(row)) for row in valid) / len(valid)
            if valid
            else None
        )

    def group_metrics(group: list[Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(group),
            "create_to_entry_slots": distribution(
                [
                    row["create_to_entry_slots"]
                    for row in group
                    if row.get("create_to_entry_slots") is not None
                ]
            ),
            "entry_rank": distribution([row["entry_rank"] for row in group]),
            "pre_buy_sol": distribution([row["pre_buy_sol"] for row in group]),
            "pre_unique_buyers": distribution(
                [row["pre_unique_buyers"] for row in group]
            ),
            "creator_buy_sol": distribution(
                [row["creator_buy_sol"] for row in group]
            ),
            "external_pre_buy_sol": distribution(
                [row["external_pre_buy_sol"] for row in group]
            ),
            "prior_creator_launches": distribution(
                [
                    row["creator_history"]["prior_pump_launches_found"]
                    for row in group
                    if row.get("creator_history", {}).get(
                        "prior_pump_launches_found"
                    )
                    is not None
                ]
            ),
        }

    creators = Counter(
        str(row.get("creator") or "") for row in valid if row.get("creator")
    )
    hosts = Counter(str(row.get("metadata_host") or "unknown") for row in valid)
    programs = Counter(
        program for row in valid for program in row.get("buy_route_programs") or []
    )
    return {
        "requested": len(rows),
        "successfully_enriched": len(valid),
        "enrichment_rate": len(valid) / len(rows) if rows else None,
        "same_slot_fraction": fraction(
            lambda row: row.get("create_to_entry_slots") == 0
        ),
        "within_one_slot_fraction": fraction(
            lambda row: (row.get("create_to_entry_slots") or 0) <= 1
        ),
        "no_pre_sell_fraction": fraction(
            lambda row: int(row.get("pre_sell_count") or 0) == 0
        ),
        "creator_buy_in_create_fraction": fraction(
            lambda row: bool(row.get("creator_buy_in_create_tx"))
        ),
        "creator_only_before_e4_fraction": fraction(
            lambda row: int(row.get("external_pre_buyers") or 0) == 0
        ),
        "multi_buy_signature_before_e4_fraction": fraction(
            lambda row: int(row.get("pre_multi_buy_signature_events") or 0) > 0
        ),
        "fresh_creator_in_lookback_fraction": fraction(
            lambda row: row.get("creator_history", {}).get(
                "prior_pump_launches_found"
            )
            == 0
        ),
        "duplicate_creator_count": sum(count > 1 for count in creators.values()),
        "largest_creator_reuse": max(creators.values(), default=0),
        "top_metadata_hosts": hosts.most_common(15),
        "top_entry_programs": programs.most_common(15),
        "all": group_metrics(valid),
        "winners": group_metrics(winners),
        "losses": group_metrics(losses),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture = json.loads(args.fixture.read_text())
    positions = sample_positions(list(fixture["positions"]), args.positions)
    urls = [
        value.strip()
        for value in os.getenv("E4_RESEARCH_RPC_URLS", "").split(",")
        if value.strip()
    ]
    helius_key = os.getenv("HELIUS_API_KEY", "").strip()
    if helius_key:
        urls.insert(0, f"https://mainnet.helius-rpc.com/?api-key={helius_key}")
    if not urls:
        urls = list(DEFAULT_RPCS)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.output.with_name(args.output.stem + "-checkpoint.jsonl")
    checkpoint.write_text("", encoding="utf-8")
    results: list[dict[str, Any]] = []
    completed = [0]
    stop = asyncio.Event()
    creator_cache: dict[str, dict[str, Any]] = {}

    async with RpcPool(
        urls, concurrency=args.concurrency, timeout=args.timeout
    ) as rpc:
        heartbeat_task = asyncio.create_task(
            heartbeat(completed, len(positions), stop)
        )
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        for row in positions:
            queue.put_nowait(row)

        async def worker(worker_id: int) -> None:
            while True:
                try:
                    observed = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    row = await asyncio.wait_for(
                        analyse_position(
                            rpc,
                            observed,
                            prior_limit=args.prior_signatures,
                            creator_limit=args.creator_signatures,
                            creator_cache=creator_cache,
                        ),
                        timeout=args.position_timeout,
                    )
                except Exception as exc:
                    row = {
                        **dict(observed),
                        "research_ok": False,
                        "error": str(exc),
                    }
                results.append(row)
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(row, separators=(",", ":"), default=str)
                        + "\n"
                    )
                completed[0] += 1
                print(
                    json.dumps(
                        {
                            "worker": worker_id,
                            "completed": completed[0],
                            "total": len(positions),
                            "mint": observed.get("mint"),
                            "ok": row.get("research_ok"),
                            "error": row.get("error"),
                        }
                    ),
                    flush=True,
                )
                queue.task_done()

        workers = [
            asyncio.create_task(worker(index)) for index in range(args.workers)
        ]
        await asyncio.gather(*workers)
        stop.set()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        diagnostics = {
            "rpc_urls": [url.split("?api-key=", 1)[0] for url in urls],
            "rpc_latency_ms": distribution(rpc.latencies_ms),
            "rpc_error_count": len(rpc.errors),
            "rpc_errors_tail": rpc.errors[-30:],
            "transaction_cache_size": len(rpc.tx_cache),
        }

    results.sort(key=lambda row: int(row.get("buy_block_time") or 0))
    report = {
        "report_version": "e4-selection-research-v2",
        "wallet": fixture.get("wallet", E4_WALLET),
        "generated_at_epoch": int(time.time()),
        "hypothesis_only": True,
        "positions_requested": len(positions),
        "positions": results,
        "summary": feature_summary(results),
        "diagnostics": diagnostics,
        "limitations": [
            "Public RPC history can be rate-limited or incomplete; every missing creation is reported rather than imputed.",
            "Block time is second-resolution and slot ordering is more reliable than wall-clock delay for historical transactions.",
            "Creator lookback inspects a bounded signature sample and is not a lifetime developer history.",
            "This research identifies observable commonalities; it cannot reveal private mempool, shred or off-chain intelligence unavailable on public chain data.",
        ],
    }
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Bounded, checkpointed E4 creator and pre-entry research"
    )
    value.add_argument(
        "--fixture",
        type=Path,
        default=Path("research/e4/e4-observed-entries-221.json"),
    )
    value.add_argument("--positions", type=int, default=60)
    value.add_argument("--prior-signatures", type=int, default=40)
    value.add_argument("--creator-signatures", type=int, default=20)
    value.add_argument("--workers", type=int, default=6)
    value.add_argument("--concurrency", type=int, default=10)
    value.add_argument("--timeout", type=float, default=6.0)
    value.add_argument("--position-timeout", type=float, default=90.0)
    value.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/e4-selection-research.json"),
    )
    return value


if __name__ == "__main__":
    arguments = parser().parse_args()
    report = asyncio.run(run(arguments))
    print(
        json.dumps(
            {
                "summary": report["summary"],
                "diagnostics": report["diagnostics"],
            },
            indent=2,
        )
    )
