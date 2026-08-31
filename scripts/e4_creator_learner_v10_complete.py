#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

from e4_creator_learner_v10 import (
    DEFAULT_RPCS,
    RpcPool,
    atomic_json,
    creator_launches,
    load_known,
)
from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

HANDLE_RE = re.compile(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", re.I)


class JsonlCursor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.seen: set[str] = set()

    def poll(self) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:
            self.offset = 0
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                key = str(
                    row.get("observation_id")
                    or row.get("signature")
                    or f"{row.get('mint')}:{row.get('observed_ns')}:{row.get('outcome')}"
                )
                if key in self.seen:
                    continue
                self.seen.add(key)
                rows.append(dict(row))
            self.offset = handle.tell()
        if len(self.seen) > 200_000:
            self.seen = set(list(self.seen)[-100_000:])
        return rows


async def transaction(rpc: RpcPool, signature: str) -> Mapping[str, Any] | None:
    try:
        value = await rpc.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )
        return value if isinstance(value, Mapping) else None
    except Exception:
        return None


async def token_peak_history(
    rpc: RpcPool,
    mint: str,
    *,
    max_signatures: int,
    sol_usd: float,
) -> dict[str, Any]:
    try:
        signatures = await rpc.call(
            "getSignaturesForAddress",
            [mint, {"limit": min(1_000, max_signatures)}],
        )
    except Exception as exc:
        return {"history_error": f"{type(exc).__name__}: {exc}"}
    rows = list(signatures or [])
    create_supply: int | None = None
    prices: list[float] = []
    first_price: float | None = None
    trades = 0
    for start in range(0, len(rows), 20):
        batch = rows[start : start + 20]
        txs = await asyncio.gather(*(transaction(rpc, str(row.get("signature") or "")) for row in batch))
        for tx in txs:
            if not tx:
                continue
            logs = list((tx.get("meta") or {}).get("logMessages") or [])
            for event in anchor_events_from_logs(logs, PUMP_PROGRAM_ID):
                if str(event.get("mint") or "") != mint:
                    continue
                if event.get("anchor_event") == "CreateEvent":
                    create_supply = int(event.get("token_total_supply") or create_supply or 0) or create_supply
                    token = float(event.get("virtual_token_reserves") or 0.0)
                    sol = float(event.get("virtual_sol_reserves") or 0.0)
                    if token > 0 and sol > 0:
                        first_price = (sol / 1e9) / (token / 1e6)
                        prices.append(first_price)
                elif event.get("anchor_event") == "TradeEvent":
                    token = float(event.get("virtual_token_reserves") or 0.0)
                    sol = float(event.get("virtual_sol_reserves") or 0.0)
                    if token > 0 and sol > 0:
                        prices.append((sol / 1e9) / (token / 1e6))
                        trades += 1
    if not prices:
        return {"history_transactions": len(rows), "history_trade_events": 0}
    supply_tokens = (create_supply / 1e6) if create_supply else 1_000_000_000.0
    peak_price = max(prices)
    initial = first_price or prices[0]
    return {
        "history_transactions": len(rows),
        "history_trade_events": trades,
        "initial_price_sol": initial,
        "peak_price_sol": peak_price,
        "max_multiple": peak_price / initial if initial > 0 else 0.0,
        "max_market_cap_usd": peak_price * supply_tokens * sol_usd,
    }


def metadata_url(uri: str, gateway: str) -> str:
    if uri.startswith("ipfs://"):
        return gateway.rstrip("/") + "/" + uri.removeprefix("ipfs://").lstrip("/")
    return uri


def social_values(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    handles: list[str] = []
    links: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            if "http://" in value or "https://" in value:
                links.append(value)
                handles.extend(match.lower() for match in HANDLE_RE.findall(value))
    return list(dict.fromkeys(handles)), list(dict.fromkeys(links))


async def metadata_profile(
    session: aiohttp.ClientSession,
    uri: str | None,
    *,
    gateway: str,
) -> dict[str, Any]:
    if not uri:
        return {}
    url = metadata_url(uri, gateway)
    try:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                return {"metadata_status": response.status}
            payload = await response.json(content_type=None)
    except Exception as exc:
        return {"metadata_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, Mapping):
        return {}
    handles, links = social_values(payload)
    return {
        "social_handles": handles,
        "social_links": links,
        "metadata_name": payload.get("name"),
        "metadata_symbol": payload.get("symbol"),
    }


def social_registry(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    raw = data.get("handles") or data.get("accounts") or {} if isinstance(data, Mapping) else {}
    output: dict[str, float] = {}
    if isinstance(raw, Mapping):
        iterator = raw.items()
    else:
        iterator = (
            (str(row.get("handle") or row.get("username") or ""), row)
            for row in raw if isinstance(row, Mapping)
        ) if isinstance(raw, list) else []
    for handle, row in iterator:
        handle = str(handle).lower().lstrip("@")
        if not handle:
            continue
        if isinstance(row, Mapping):
            value = row.get("authority") or row.get("score") or row.get("authority_score") or 0.0
        else:
            value = 0.0
        try:
            score = float(value)
        except (TypeError, ValueError):
            score = 0.0
        if score > 1:
            score /= 100
        output[handle] = min(1.0, max(0.0, score))
    return output


def classify(
    launches: list[dict[str, Any]],
    *,
    e4_hits: int,
    e4_wins: int,
    e4_losses: int,
    authority: Mapping[str, float],
) -> tuple[str, float, list[str]]:
    rate = e4_wins / max(1, e4_wins + e4_losses)
    runners = sum(
        float(row.get("max_market_cap_usd") or 0.0) >= 30_000
        or float(row.get("max_multiple") or 0.0) >= 2.0
        for row in launches
    )
    elite_runners = sum(
        float(row.get("max_market_cap_usd") or 0.0) >= 100_000
        or float(row.get("max_multiple") or 0.0) >= 5.0
        for row in launches
    )
    handles = Counter(
        handle
        for row in launches
        for handle in row.get("social_handles") or []
    )
    linked = {handle: authority.get(handle, 0.0) for handle in handles if authority.get(handle, 0.0) > 0}
    best_social = max(linked.values(), default=0.0)
    evidence = [
        f"launches:{len(launches)}",
        f"runners:{runners}",
        f"elite_runners:{elite_runners}",
        f"e4:{e4_wins}W/{e4_losses}L/{e4_hits} buys",
        f"best_social:{best_social:.3f}",
    ]
    if e4_wins + e4_losses >= 3 and rate <= 0.25:
        return "NEGATIVE", 0.05, evidence
    if e4_wins + e4_losses >= 3 and e4_wins >= 2 and rate >= 0.75:
        return "ELITE", min(0.99, 0.92 + 0.015 * min(4, e4_hits)), evidence
    if e4_wins >= 1 and rate >= 0.50:
        return "APPROVED", min(0.96, 0.84 + 0.02 * min(5, e4_hits)), evidence
    if elite_runners >= 2 or runners >= 4:
        return "APPROVED", min(0.94, 0.80 + 0.025 * runners + 0.035 * elite_runners), evidence
    if best_social >= 0.75 and runners >= 1:
        return "APPROVED", min(0.92, 0.76 + 0.12 * best_social + 0.02 * runners), evidence
    if runners >= 1 or e4_hits >= 1 or best_social >= 0.60:
        return "WATCH", min(0.76, 0.56 + 0.035 * runners + 0.04 * min(2, e4_hits) + 0.08 * best_social), evidence
    return "UNKNOWN", 0.0, evidence


async def enrich_creator(
    rpc: RpcPool,
    session: aiohttp.ClientSession,
    creator: str,
    *,
    max_creator_signatures: int,
    max_token_signatures: int,
    sol_usd: float,
    metadata_concurrency: int,
    ipfs_gateway: str,
) -> list[dict[str, Any]]:
    launches = await creator_launches(rpc, creator, max_creator_signatures)
    semaphore = asyncio.Semaphore(metadata_concurrency)

    async def enrich(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            history, metadata = await asyncio.gather(
                token_peak_history(
                    rpc,
                    row["mint"],
                    max_signatures=max_token_signatures,
                    sol_usd=sol_usd,
                ),
                metadata_profile(session, row.get("uri"), gateway=ipfs_gateway),
            )
            return {**row, **history, **metadata}

    output: list[dict[str, Any]] = []
    for start in range(0, len(launches), 20):
        output.extend(await asyncio.gather(*(enrich(row) for row in launches[start : start + 20])))
    return output


async def run(args: argparse.Namespace) -> int:
    urls = tuple(part.strip() for part in args.rpc_urls.split(",") if part.strip())
    authority = social_registry(args.social_sources)
    known = load_known(args.expectancy, args.discovered)
    observation_cursor = JsonlCursor(args.observations)
    outcome_cursor = JsonlCursor(args.outcomes)
    existing = {}
    try:
        current = json.loads(args.discovered.read_text(encoding="utf-8"))
        if isinstance(current, Mapping) and isinstance(current.get("creators"), Mapping):
            existing = {str(key): dict(value) for key, value in current["creators"].items() if isinstance(value, Mapping)}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    timeout = aiohttp.ClientTimeout(total=args.metadata_timeout)
    async with RpcPool(urls, args.rpc_timeout, args.rpc_concurrency) as rpc, aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            observations = observation_cursor.poll()
            outcomes = outcome_cursor.poll()
            changed: set[str] = set()
            for row in observations:
                creator = str(row.get("creator") or "")
                if not creator:
                    continue
                record = existing.setdefault(creator, {"creator": creator})
                record["e4_teacher_hits"] = int(record.get("e4_teacher_hits") or 0) + 1
                changed.add(creator)
            for row in outcomes:
                creator = str(row.get("creator") or "")
                if not creator:
                    continue
                record = existing.setdefault(creator, {"creator": creator})
                if str(row.get("outcome") or "").upper() == "WIN":
                    record["e4_teacher_wins"] = int(record.get("e4_teacher_wins") or 0) + 1
                else:
                    record["e4_teacher_losses"] = int(record.get("e4_teacher_losses") or 0) + 1
                record["e4_teacher_gross_pnl_sol"] = float(record.get("e4_teacher_gross_pnl_sol") or 0.0) + float(row.get("gross_pnl_sol") or 0.0)
                changed.add(creator)
            for creator in sorted(changed):
                record = existing[creator]
                launches = await enrich_creator(
                    rpc,
                    session,
                    creator,
                    max_creator_signatures=args.max_creator_signatures,
                    max_token_signatures=args.max_token_signatures,
                    sol_usd=args.sol_usd,
                    metadata_concurrency=args.metadata_concurrency,
                    ipfs_gateway=args.ipfs_gateway,
                )
                hits = int(record.get("e4_teacher_hits") or 0)
                wins = int(record.get("e4_teacher_wins") or 0)
                losses = int(record.get("e4_teacher_losses") or 0)
                status, score, evidence = classify(
                    launches,
                    e4_hits=hits,
                    e4_wins=wins,
                    e4_losses=losses,
                    authority=authority,
                )
                handles = Counter(handle for row in launches for handle in row.get("social_handles") or [])
                hosts = Counter(str(row.get("metadata_host") or "") for row in launches if row.get("metadata_host"))
                record.update(
                    {
                        "creator": creator,
                        "status": status,
                        "score": score,
                        "source": "complete-e4-teacher-onchain-history",
                        "e4_teacher_hits": hits,
                        "e4_teacher_wins": wins,
                        "e4_teacher_losses": losses,
                        "previous_launches": len(launches),
                        "profitable_launches": sum(
                            float(row.get("max_market_cap_usd") or 0.0) >= 30_000
                            or float(row.get("max_multiple") or 0.0) >= 2.0
                            for row in launches
                        ),
                        "max_peak_market_cap_usd": max((float(row.get("max_market_cap_usd") or 0.0) for row in launches), default=0.0),
                        "median_peak_market_cap_usd": sorted(float(row.get("max_market_cap_usd") or 0.0) for row in launches)[len(launches)//2] if launches else 0.0,
                        "common_social_handles": [handle for handle, _ in handles.most_common(20)],
                        "linked_monitored_socials": {handle: authority.get(handle, 0.0) for handle in handles if authority.get(handle, 0.0)>0},
                        "common_metadata_hosts": [host for host, _ in hosts.most_common(20)],
                        "evidence": evidence,
                        "launches": launches[-100:],
                        "updated_ns": time.time_ns(),
                        "already_in_e4_expectancy": creator in known,
                    }
                )
                atomic_json(
                    args.discovered,
                    {
                        "version": "e4-discovered-creators-v10-complete",
                        "updated_ns": time.time_ns(),
                        "creators": existing,
                    },
                )
                print(json.dumps({"creator": creator, "status": status, "score": score, "launches": len(launches), "wins": wins, "losses": losses}), flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.poll_seconds)
            except asyncio.TimeoutError:
                pass
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Complete E4 V10 creator/KOL/history learner")
    value.add_argument("--observations", type=Path, default=Path(os.getenv("E4_TEACHER_JOURNAL", "var/e4/e4-teacher-observations.jsonl")))
    value.add_argument("--outcomes", type=Path, default=Path(os.getenv("E4_LIVE_OUTCOME_JOURNAL", "var/e4/e4-live-outcomes.jsonl")))
    value.add_argument("--expectancy", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    value.add_argument("--discovered", type=Path, default=Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "var/e4/e4-discovered-creators-live.json")))
    value.add_argument("--social-sources", type=Path, default=Path("models/e4/e4-social-sources.json"))
    value.add_argument("--rpc-urls", default=os.getenv("E4_LEARNER_RPC_URLS", ",".join(DEFAULT_RPCS)))
    value.add_argument("--rpc-timeout", type=float, default=6.0)
    value.add_argument("--rpc-concurrency", type=int, default=12)
    value.add_argument("--metadata-timeout", type=float, default=5.0)
    value.add_argument("--metadata-concurrency", type=int, default=12)
    value.add_argument("--max-creator-signatures", type=int, default=500)
    value.add_argument("--max-token-signatures", type=int, default=500)
    value.add_argument("--sol-usd", type=float, default=float(os.getenv("E4_SOL_USD_FALLBACK", "150")))
    value.add_argument("--ipfs-gateway", default=os.getenv("E4_IPFS_GATEWAY", "https://ipfs.io/ipfs"))
    value.add_argument("--poll-seconds", type=float, default=0.25)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
