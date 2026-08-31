#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import aiohttp

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class RpcPool:
    def __init__(self, urls: Sequence[str], timeout: float, concurrency: int) -> None:
        self.urls = tuple(dict.fromkeys(urls))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0

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
            for attempt in range(max(1, retries) * len(self.urls)):
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
                            raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
                        payload = json.loads(text)
                        if payload.get("error"):
                            raise RuntimeError(str(payload["error"]))
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        return payload.get("result")
                except Exception as exc:
                    last = exc
                    await asyncio.sleep(min(0.8, 0.05 * (attempt + 1)))
            raise RuntimeError(f"{method} failed: {last}")


async def tx(rpc: RpcPool, signature: str) -> Mapping[str, Any] | None:
    try:
        result = await rpc.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )
        return result if isinstance(result, Mapping) else None
    except Exception:
        return None


async def creator_launches(rpc: RpcPool, creator: str, max_signatures: int) -> list[dict[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    before: str | None = None
    while len(rows) < max_signatures:
        options: dict[str, Any] = {"limit": min(1_000, max_signatures - len(rows))}
        if before:
            options["before"] = before
        batch = await rpc.call("getSignaturesForAddress", [creator, options])
        batch = list(batch or [])
        if not batch:
            break
        rows.extend(batch)
        before = str(batch[-1].get("signature") or "")
        if len(batch) < options["limit"]:
            break
    launches: dict[str, dict[str, Any]] = {}
    for start in range(0, len(rows), 24):
        batch = rows[start : start + 24]
        transactions = await asyncio.gather(*(tx(rpc, str(row["signature"])) for row in batch))
        for row, transaction in zip(batch, transactions):
            if not transaction:
                continue
            logs = list((transaction.get("meta") or {}).get("logMessages") or [])
            for event in anchor_events_from_logs(logs, PUMP_PROGRAM_ID):
                if event.get("anchor_event") != "CreateEvent":
                    continue
                event_creator = str(event.get("creator") or event.get("user") or "")
                if event_creator != creator:
                    continue
                mint = str(event.get("mint") or "")
                if not mint:
                    continue
                uri = str(event.get("uri") or "")
                launches[mint] = {
                    "mint": mint,
                    "signature": str(row.get("signature") or ""),
                    "slot": int(transaction.get("slot") or row.get("slot") or 0),
                    "block_time": int(transaction.get("blockTime") or row.get("blockTime") or 0),
                    "name": event.get("name"),
                    "symbol": event.get("symbol"),
                    "uri": uri,
                    "metadata_host": urlparse(uri).netloc.lower() if uri else "",
                }
    return sorted(launches.values(), key=lambda row: (row["slot"], row["mint"]))


async def provider_history(
    session: aiohttp.ClientSession,
    endpoint: str | None,
    mint: str,
) -> dict[str, Any]:
    if not endpoint:
        return {}
    url = endpoint.format(mint=mint)
    try:
        async with session.get(url) as response:
            if response.status >= 400:
                return {}
            data = await response.json(content_type=None)
    except Exception:
        return {}
    if not isinstance(data, Mapping):
        return {}
    # Provider adapters can normalize upstream responses into any of these keys.
    for candidate in (data, data.get("data"), data.get("result"), data.get("token")):
        if not isinstance(candidate, Mapping):
            continue
        peak = candidate.get("max_market_cap_usd") or candidate.get("peak_market_cap_usd") or candidate.get("ath_market_cap")
        multiple = candidate.get("max_multiple") or candidate.get("peak_multiple")
        if peak is not None or multiple is not None:
            return {
                "max_market_cap_usd": float(peak or 0.0),
                "max_multiple": float(multiple or 0.0),
            }
    return {}


def load_known(*paths: Path) -> set[str]:
    output: set[str] = set()
    for path in paths:
        data = read_json(path)
        if not isinstance(data, Mapping):
            continue
        records = data.get("creators") or {}
        if isinstance(records, Mapping):
            output.update(str(key) for key in records)
        for row in data.get("top_creators") or []:
            if isinstance(row, Mapping) and row.get("creator"):
                output.add(str(row["creator"]))
    return output


def promote(launches: list[dict[str, Any]], teacher_hits: int, teacher_wins: int, teacher_losses: int) -> tuple[str, float, list[str]]:
    peaks = [float(row.get("max_market_cap_usd") or 0.0) for row in launches]
    multiples = [float(row.get("max_multiple") or 0.0) for row in launches]
    runners = sum(peak >= 30_000 or multiple >= 2.0 for peak, multiple in zip(peaks, multiples))
    elite_runners = sum(peak >= 100_000 or multiple >= 5.0 for peak, multiple in zip(peaks, multiples))
    teacher_rate = teacher_wins / max(1, teacher_wins + teacher_losses)
    evidence = [f"launches:{len(launches)}", f"runners:{runners}", f"elite_runners:{elite_runners}", f"e4_hits:{teacher_hits}"]
    if teacher_hits >= 3 and teacher_rate >= 0.75:
        return "ELITE", min(0.98, 0.90 + 0.02 * min(4, teacher_hits)), evidence
    if teacher_hits >= 1 and teacher_rate >= 0.50:
        return "APPROVED", min(0.94, 0.82 + 0.02 * min(5, teacher_hits)), evidence
    if elite_runners >= 2 or runners >= 3:
        return "APPROVED", min(0.92, 0.78 + 0.025 * runners + 0.03 * elite_runners), evidence
    if runners >= 1 or len(launches) >= 3:
        return "WATCH", min(0.74, 0.55 + 0.03 * runners + 0.01 * min(10, len(launches))), evidence
    return "UNKNOWN", 0.0, evidence


class TeacherJournal:
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
        output: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                identifier = str(row.get("observation_id") or row.get("signature") or "")
                if identifier and identifier in self.seen:
                    continue
                if identifier:
                    self.seen.add(identifier)
                output.append(dict(row))
            self.offset = handle.tell()
        return output


async def run(args: argparse.Namespace) -> int:
    urls = tuple(part.strip() for part in args.rpc_urls.split(",") if part.strip())
    known = load_known(args.expectancy, args.discovered)
    journal = TeacherJournal(args.journal)
    existing = read_json(args.discovered)
    profiles: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        raw = existing.get("creators") or existing.get("profiles") or {}
        if isinstance(raw, Mapping):
            profiles.update({str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    timeout = aiohttp.ClientTimeout(total=args.provider_timeout)
    async with RpcPool(urls, args.rpc_timeout, args.concurrency) as rpc, aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            rows = journal.poll()
            creators: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                creator = str(row.get("creator") or "")
                if creator:
                    creators.setdefault(creator, []).append(row)
            for creator, observations in creators.items():
                record = profiles.get(creator, {})
                e4_hits = int(record.get("e4_teacher_hits") or 0) + len(observations)
                wins = int(record.get("e4_teacher_wins") or 0)
                losses = int(record.get("e4_teacher_losses") or 0)
                launches = await creator_launches(rpc, creator, args.max_signatures)
                for start in range(0, len(launches), 20):
                    histories = await asyncio.gather(
                        *(provider_history(session, args.history_endpoint, row["mint"]) for row in launches[start : start + 20])
                    )
                    for row, history in zip(launches[start : start + 20], histories):
                        row.update(history)
                status, score, evidence = promote(launches, e4_hits, wins, losses)
                profiles[creator] = {
                    "creator": creator,
                    "status": status,
                    "score": score,
                    "source": "e4-teacher-history-scan",
                    "e4_teacher_hits": e4_hits,
                    "e4_teacher_wins": wins,
                    "e4_teacher_losses": losses,
                    "previous_launches": len(launches),
                    "profitable_launches": sum(
                        float(row.get("max_market_cap_usd") or 0.0) >= args.runner_market_cap
                        or float(row.get("max_multiple") or 0.0) >= args.runner_multiple
                        for row in launches
                    ),
                    "max_peak_market_cap_usd": max((float(row.get("max_market_cap_usd") or 0.0) for row in launches), default=0.0),
                    "common_metadata_hosts": sorted(
                        {str(row.get("metadata_host") or "") for row in launches if row.get("metadata_host")}
                    ),
                    "evidence": evidence,
                    "launches": launches[-100:],
                    "updated_ns": time.time_ns(),
                    "already_in_e4_expectancy": creator in known,
                }
                atomic_json(
                    args.discovered,
                    {
                        "version": "e4-discovered-creators-v10",
                        "updated_ns": time.time_ns(),
                        "creators": profiles,
                    },
                )
                print(json.dumps({"creator": creator, "status": status, "score": score, "launches": len(launches)}), flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.poll_seconds)
            except asyncio.TimeoutError:
                pass
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="E4 V10 asynchronous creator-history learner")
    value.add_argument("--journal", type=Path, default=Path(os.getenv("E4_TEACHER_JOURNAL", "var/e4/e4-teacher-observations.jsonl")))
    value.add_argument("--expectancy", type=Path, default=Path("models/e4/e4-creator-expectancy.json"))
    value.add_argument("--discovered", type=Path, default=Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "var/e4/e4-discovered-creators-live.json")))
    value.add_argument("--rpc-urls", default=os.getenv("E4_LEARNER_RPC_URLS", ",".join(DEFAULT_RPCS)))
    value.add_argument("--history-endpoint", default=os.getenv("E4_TOKEN_HISTORY_URL_TEMPLATE"))
    value.add_argument("--max-signatures", type=int, default=500)
    value.add_argument("--concurrency", type=int, default=12)
    value.add_argument("--rpc-timeout", type=float, default=6.0)
    value.add_argument("--provider-timeout", type=float, default=5.0)
    value.add_argument("--poll-seconds", type=float, default=0.25)
    value.add_argument("--runner-market-cap", type=float, default=30_000.0)
    value.add_argument("--runner-multiple", type=float, default=2.0)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
