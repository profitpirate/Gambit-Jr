#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
X_EPOCH_MS = 1_288_834_974_657
STATUS_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status(?:es)?/(\d+)", re.I)
HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([^/?#]+)", re.I)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def urls(uri: str) -> list[str]:
    text = str(uri or "").strip()
    if not text:
        return []
    if text.startswith("ipfs://"):
        key = text[7:].lstrip("/")
        return [f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"]
    if text.startswith("ar://"):
        return [f"https://arweave.net/{text[5:].lstrip('/')}"]
    result = [text]
    if "/ipfs/" in text:
        key = text.split("/ipfs/", 1)[1]
        result.extend([f"https://ipfs.io/ipfs/{key}", f"https://gateway.pinata.cloud/ipfs/{key}"])
    return list(dict.fromkeys(result))


def walk(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"twitter", "x", "social", "socials", "website", "description", "telegram"}:
                output.extend(walk(item))
            elif isinstance(item, (Mapping, list, tuple)):
                output.extend(walk(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            output.extend(walk(item))
    return output


def social(metadata: Mapping[str, Any] | None, create_ns: int) -> dict[str, Any]:
    handle = ""
    status_id = ""
    twitter = ""
    for value in walk(metadata or {}):
        match = STATUS_RE.search(value)
        if match:
            handle = match.group(1).lower().lstrip("@")
            status_id = match.group(2)
            twitter = value
            break
    if not handle:
        for value in walk(metadata or {}):
            match = HANDLE_RE.search(value)
            if match:
                handle = match.group(1).lower().lstrip("@")
                twitter = value
                break
    tweet_age = None
    if status_id:
        tweet_ms = (int(status_id) >> 22) + X_EPOCH_MS
        tweet_age = (create_ns / 1e6 - tweet_ms) / 1000.0
    return {
        "twitter": twitter,
        "twitter_handle": handle,
        "twitter_status_id": status_id,
        "tweet_age_seconds": tweet_age,
    }


async def fetch_one(session: aiohttp.ClientSession, sem: asyncio.Semaphore, uri: str) -> tuple[Mapping[str, Any] | None, str]:
    async with sem:
        last = ""
        for url in urls(uri):
            try:
                async with session.get(url, headers={"accept": "application/json"}) as response:
                    if response.status >= 400:
                        last = f"HTTP {response.status}"
                        continue
                    payload = await response.json(content_type=None)
                    if isinstance(payload, Mapping):
                        return payload, ""
            except Exception as exc:  # noqa: BLE001 - research evidence records exact failure
                last = f"{type(exc).__name__}: {exc}"
        return None, last or "no metadata URL"


async def run(args: argparse.Namespace) -> int:
    launches: dict[str, dict[str, Any]] = {}
    with args.events.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            kind = str(row.get("kind") or "").upper()
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            if kind == "CREATE":
                launches[mint] = {
                    "mint": mint,
                    "creator": str(row.get("creator") or raw.get("creator") or row.get("trader") or ""),
                    "create_ns": integer(row.get("received_ns")),
                    "create_slot": integer(row.get("slot")),
                    "uri": str(raw.get("uri") or ""),
                    "token_program": str(raw.get("token_program") or ""),
                    "cashback": bool(raw.get("is_cashback_enabled")),
                    "mayhem": bool(raw.get("is_mayhem_mode")),
                    "e4_selected": False,
                    "e4_entry_sol": 0.0,
                }
            elif mint in launches and str(row.get("trader") or "") == E4_WALLET and kind in {"BUY", "PUMPSWAP_BUY"}:
                launches[mint]["e4_selected"] = True
                launches[mint]["e4_entry_sol"] = finite(row.get("sol_amount"))

    sem = asyncio.Semaphore(args.concurrency)
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=max(32, args.concurrency), ttl_dns_cache=600, keepalive_timeout=45)
    rows = list(launches.values())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for start in range(0, len(rows), 300):
            chunk = rows[start : start + 300]
            results = await asyncio.gather(*(fetch_one(session, sem, row["uri"]) for row in chunk))
            for row, (metadata, error) in zip(chunk, results):
                row["metadata_ok"] = bool(metadata)
                row["metadata_error"] = error
                row["metadata"] = dict(metadata or {})
                row.update(social(metadata, row["create_ns"]))
            print(json.dumps({"processed": min(len(rows), start + len(chunk)), "target": len(rows)}), flush=True)

    rows.sort(key=lambda row: (row["create_ns"], row["mint"]))
    payload = {
        "version": "e4-v12-latest-metadata-probe-v1",
        "launches": len(rows),
        "selected": sum(row["e4_selected"] for row in rows),
        "metadata_resolved": sum(row["metadata_ok"] for row in rows),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("launches", "selected", "metadata_resolved")}, indent=2), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=96)
    parser.add_argument("--timeout", type=float, default=5.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
