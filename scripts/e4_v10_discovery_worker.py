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

from memecoin_bot.e4_pipelines_v10 import _atomic_json
from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def host(uri: Any) -> str:
    try:
        return urlparse(str(uri or "")).netloc.lower()
    except Exception:
        return ""


class RpcPool:
    def __init__(self, urls: Sequence[str], timeout: float, concurrency: int) -> None:
        self.urls = tuple(dict.fromkeys(urls))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.cursor = 0
        self.request_id = 0
        self.session: aiohttp.ClientSession | None = None
        self.errors: list[str] = []

    async def __aenter__(self) -> "RpcPool":
        connector = aiohttp.TCPConnector(limit=max(16, self.sem._value * 2), ttl_dns_cache=600)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()

    async def call(self, method: str, params: list[Any], retries: int = 2) -> Any:
        assert self.session is not None
        async with self.sem:
            last: Exception | None = None
            for attempt in range(max(1, retries) * max(1, len(self.urls))):
                url = self.urls[(self.cursor + attempt) % len(self.urls)]
                self.request_id += 1
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
                    self.errors.append(f"{method}@{url}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(0.5, 0.04 * (attempt + 1)))
            raise RuntimeError(f"{method} exhausted RPC pool: {last}")


async def get_transaction(rpc: RpcPool, signature: str) -> Mapping[str, Any] | None:
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


def create_events(tx: Mapping[str, Any]) -> list[dict[str, Any]]:
    logs = list((tx.get("meta") or {}).get("logMessages") or [])
    return [
        event
        for event in anchor_events_from_logs(logs, PUMP_PROGRAM_ID)
        if event.get("anchor_event") == "CreateEvent"
    ]


def trade_events(tx: Mapping[str, Any], mint: str) -> list[dict[str, Any]]:
    logs = list((tx.get("meta") or {}).get("logMessages") or [])
    return [
        event
        for event in anchor_events_from_logs(logs, PUMP_PROGRAM_ID)
        if event.get("anchor_event") in {"TradeEvent", "CompleteEvent", "CompletePumpAmmMigrationEvent"}
        and str(event.get("mint") or "") == mint
    ]


def event_price(event: Mapping[str, Any]) -> float:
    quote = finite(event.get("virtual_sol_reserves") or event.get("virtual_quote_reserves"))
    tokens = finite(event.get("virtual_token_reserves"))
    return quote / tokens if quote > 0 and tokens > 0 else 0.0


async def metadata_json(session: aiohttp.ClientSession, uri: str) -> dict[str, Any]:
    if not uri or not uri.startswith(("http://", "https://")):
        return {}
    try:
        async with session.get(uri, timeout=aiohttp.ClientTimeout(total=4)) as response:
            if response.status >= 400:
                return {}
            value = await response.json(content_type=None)
            return dict(value) if isinstance(value, Mapping) else {}
    except Exception:
        return {}


def social_values(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()
    for key in ("twitter", "x", "telegram", "website", "discord"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    extensions = metadata.get("extensions")
    if isinstance(extensions, Mapping):
        for value in extensions.values():
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return tuple(sorted(values))


async def creator_launches(
    rpc: RpcPool,
    creator: str,
    signature_limit: int,
    tx_concurrency: int,
) -> list[dict[str, Any]]:
    rows = await rpc.call(
        "getSignaturesForAddress",
        [creator, {"limit": min(1000, signature_limit)}],
        retries=3,
    )
    signatures = [str(row.get("signature") or "") for row in rows or [] if row.get("signature")]
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(signatures), tx_concurrency):
        txs = await asyncio.gather(
            *(get_transaction(rpc, signature) for signature in signatures[start : start + tx_concurrency])
        )
        for signature, tx in zip(signatures[start : start + tx_concurrency], txs):
            if not tx:
                continue
            for event in create_events(tx):
                if str(event.get("creator") or event.get("user") or "") != creator:
                    continue
                mint = str(event.get("mint") or "")
                if mint:
                    found[mint] = {
                        "mint": mint,
                        "signature": signature,
                        "slot": int(tx.get("slot") or 0),
                        "block_time": int(tx.get("blockTime") or 0),
                        "name": str(event.get("name") or ""),
                        "symbol": str(event.get("symbol") or ""),
                        "uri": str(event.get("uri") or ""),
                        "metadata_host": host(event.get("uri")),
                    }
    return sorted(found.values(), key=lambda row: (row["slot"], row["mint"]))


async def launch_outcome(
    rpc: RpcPool,
    launch: Mapping[str, Any],
    signature_limit: int,
    tx_concurrency: int,
) -> dict[str, Any]:
    mint = str(launch["mint"])
    try:
        rows = await rpc.call(
            "getSignaturesForAddress",
            [mint, {"limit": min(1000, signature_limit)}],
            retries=2,
        )
    except Exception as exc:
        return {**dict(launch), "resolved": False, "error": f"{type(exc).__name__}: {exc}"}
    signatures = [str(row.get("signature") or "") for row in rows or [] if row.get("signature")]
    events: list[tuple[int, int, Mapping[str, Any]]] = []
    order = 0
    for start in range(0, len(signatures), tx_concurrency):
        txs = await asyncio.gather(
            *(get_transaction(rpc, signature) for signature in signatures[start : start + tx_concurrency])
        )
        for tx in txs:
            if not tx:
                continue
            slot = int(tx.get("slot") or 0)
            for event in trade_events(tx, mint):
                events.append((slot, order, event))
                order += 1
    events.sort(key=lambda row: (row[0], row[1]))
    prices = [event_price(event) for _, _, event in events if event.get("anchor_event") == "TradeEvent"]
    prices = [value for value in prices if value > 0]
    entry_price = prices[0] if prices else 0.0
    peak_price = max(prices, default=0.0)
    peak_multiple = peak_price / entry_price if entry_price > 0 else 0.0
    sells = [event for _, _, event in events if event.get("anchor_event") == "TradeEvent" and not event.get("is_buy")]
    completed = any(event.get("anchor_event") != "TradeEvent" for _, _, event in events)
    return {
        **dict(launch),
        "resolved": bool(prices),
        "trades_observed": sum(event.get("anchor_event") == "TradeEvent" for _, _, event in events),
        "entry_price": entry_price,
        "peak_price": peak_price,
        "peak_multiple": peak_multiple,
        "profitable_20pct": peak_multiple >= 1.20,
        "profitable_50pct": peak_multiple >= 1.50,
        "reached_2x": peak_multiple >= 2.0,
        "reached_5x": peak_multiple >= 5.0,
        "reached_10x": peak_multiple >= 10.0,
        "early_rug": bool(sells) and peak_multiple < 1.10,
        "completed_or_migrated": completed,
    }


def classify(outcomes: Sequence[Mapping[str, Any]]) -> tuple[str, float]:
    resolved = [row for row in outcomes if row.get("resolved")]
    if not resolved:
        return "WATCH", 0.50
    runner_rate = sum(bool(row.get("profitable_20pct")) for row in resolved) / len(resolved)
    two_x_rate = sum(bool(row.get("reached_2x")) for row in resolved) / len(resolved)
    rug_rate = sum(bool(row.get("early_rug")) for row in resolved) / len(resolved)
    peaks = [finite(row.get("peak_multiple")) for row in resolved]
    median_peak = statistics.median(peaks)
    if len(resolved) >= 6 and runner_rate >= 0.80 and median_peak >= 1.50 and rug_rate <= 0.20:
        status = "ELITE"
    elif len(resolved) >= 4 and runner_rate >= 0.70 and median_peak >= 1.30 and rug_rate <= 0.25:
        status = "STRONG"
    elif len(resolved) >= 4 and runner_rate <= 0.25:
        status = "NEGATIVE"
    else:
        status = "WATCH"
    score = min(0.97, max(0.10, 0.50 + 0.30 * runner_rate + 0.10 * two_x_rate + 0.08 * min(1.0, median_peak / 3.0) - 0.22 * rug_rate))
    return status, score


def profile(creator: str, outcomes: Sequence[Mapping[str, Any]], socials: Sequence[str]) -> dict[str, Any]:
    resolved = [row for row in outcomes if row.get("resolved")]
    status, score = classify(outcomes)
    peaks = [finite(row.get("peak_multiple")) for row in resolved]
    metadata_hosts = Counter(str(row.get("metadata_host") or "") for row in outcomes if row.get("metadata_host"))
    profitable = sum(bool(row.get("profitable_20pct")) for row in resolved)
    rugs = sum(bool(row.get("early_rug")) for row in resolved)
    return {
        "creator": creator,
        "source": "E4_DISCOVERY_CHAIN_HISTORY",
        "status": status,
        "score": score,
        "launches": len(outcomes),
        "resolved_launches": len(resolved),
        "profitable_launches": profitable,
        "profitable_launch_rate": profitable / len(resolved) if resolved else 0.0,
        "reached_2x_rate": sum(bool(row.get("reached_2x")) for row in resolved) / len(resolved) if resolved else 0.0,
        "reached_5x_rate": sum(bool(row.get("reached_5x")) for row in resolved) / len(resolved) if resolved else 0.0,
        "reached_10x_rate": sum(bool(row.get("reached_10x")) for row in resolved) / len(resolved) if resolved else 0.0,
        "rug_rate": rugs / len(resolved) if resolved else 0.0,
        "median_peak_multiple": statistics.median(peaks) if peaks else 0.0,
        "max_peak_multiple": max(peaks, default=0.0),
        "common_metadata_hosts": [key for key, _count in metadata_hosts.most_common(5)],
        "common_social_handles": sorted(set(socials)),
        "last_updated_ns": time.time_ns(),
        "launch_outcomes": list(outcomes),
    }


def read_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        creator = str(row.get("creator") or "")
        if creator and creator not in seen:
            seen.add(creator)
            rows.append(dict(row))
    return rows


async def analyse_creator(
    rpc: RpcPool,
    metadata_session: aiohttp.ClientSession,
    creator: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    launches = await creator_launches(rpc, creator, args.creator_signatures, args.tx_concurrency)
    launches = launches[-args.max_launches :]
    outcomes: list[dict[str, Any]] = []
    socials: set[str] = set()
    for start in range(0, len(launches), args.launch_concurrency):
        batch = launches[start : start + args.launch_concurrency]
        batch_outcomes = await asyncio.gather(
            *(launch_outcome(rpc, row, args.mint_signatures, args.tx_concurrency) for row in batch)
        )
        outcomes.extend(batch_outcomes)
        metadata_rows = await asyncio.gather(
            *(metadata_json(metadata_session, str(row.get("uri") or "")) for row in batch)
        )
        for metadata in metadata_rows:
            socials.update(social_values(metadata))
    return profile(creator, outcomes, sorted(socials))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    queued = read_queue(args.queue)
    if args.creator:
        queued.insert(0, {"creator": args.creator, "source": "CLI"})
    creators = list(dict.fromkeys(str(row.get("creator") or "") for row in queued if row.get("creator")))
    existing = {}
    if args.output.exists():
        try:
            payload = json.loads(args.output.read_text(encoding="utf-8"))
            existing = dict(payload.get("creators") or {}) if isinstance(payload, Mapping) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    urls = [part.strip() for part in args.rpc_urls.split(",") if part.strip()]
    timeout = aiohttp.ClientTimeout(total=6)
    async with RpcPool(urls, args.timeout, args.rpc_concurrency) as rpc, aiohttp.ClientSession(timeout=timeout) as metadata_session:
        processed = 0
        for start in range(0, len(creators), args.creator_concurrency):
            batch = creators[start : start + args.creator_concurrency]
            results = await asyncio.gather(
                *(analyse_creator(rpc, metadata_session, creator, args) for creator in batch),
                return_exceptions=True,
            )
            for creator, result in zip(batch, results):
                if isinstance(result, Exception):
                    existing[creator] = {
                        "creator": creator,
                        "status": "WATCH",
                        "score": 0.0,
                        "error": f"{type(result).__name__}: {result}",
                        "last_updated_ns": time.time_ns(),
                    }
                else:
                    existing[creator] = result
                processed += 1
                print(json.dumps({"processed": processed, "target": len(creators), "creator": creator, "status": existing[creator].get("status")}), flush=True)
        errors = rpc.errors[-200:]
    report = {
        "version": "e4-v10-discovered-known-creators-v1",
        "updated_ns": time.time_ns(),
        "queued_creators": len(creators),
        "approved_creators": sum(str(row.get("status")) in {"ELITE", "STRONG"} for row in existing.values()),
        "watch_creators": sum(str(row.get("status")) == "WATCH" for row in existing.values()),
        "negative_creators": sum(str(row.get("status")) == "NEGATIVE" for row in existing.values()),
        "creators": existing,
        "rpc_errors": errors,
    }
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Enrich unknown E4 creators and promote proven launchers")
    value.add_argument("--queue", type=Path, default=Path(os.getenv("E4_DISCOVERY_QUEUE_PATH", "runtime/e4-discovery-queue.jsonl")))
    value.add_argument("--output", type=Path, default=Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-known-creators.json")))
    value.add_argument("--creator", default="")
    value.add_argument("--rpc-urls", default=os.getenv("E4_DISCOVERY_RPC_URLS", ",".join(DEFAULT_RPCS)))
    value.add_argument("--timeout", type=float, default=6.0)
    value.add_argument("--rpc-concurrency", type=int, default=16)
    value.add_argument("--creator-concurrency", type=int, default=2)
    value.add_argument("--launch-concurrency", type=int, default=4)
    value.add_argument("--tx-concurrency", type=int, default=20)
    value.add_argument("--creator-signatures", type=int, default=500)
    value.add_argument("--mint-signatures", type=int, default=250)
    value.add_argument("--max-launches", type=int, default=20)
    return value


def main() -> int:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    _atomic_json(args.output, report)
    print(json.dumps({"output": str(args.output), "queued": report["queued_creators"], "approved": report["approved_creators"], "negative": report["negative_creators"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
