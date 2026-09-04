#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)
LAMPORTS_PER_SOL = 1_000_000_000


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return "redacted"


def account_keys(payload: Mapping[str, Any]) -> list[str]:
    message = (((payload.get("transaction") or {}).get("message")) or {})
    values = []
    for value in message.get("accountKeys") or []:
        if isinstance(value, Mapping):
            values.append(str(value.get("pubkey") or ""))
        else:
            values.append(str(value or ""))
    loaded = ((payload.get("meta") or {}).get("loadedAddresses") or {})
    values.extend(str(value or "") for value in loaded.get("writable") or [])
    values.extend(str(value or "") for value in loaded.get("readonly") or [])
    return [value for value in values if value]


def token_amount(row: Mapping[str, Any]) -> float:
    amount = (row.get("uiTokenAmount") or {}).get("uiAmountString")
    if amount is not None:
        return finite(amount)
    raw = (row.get("uiTokenAmount") or {}).get("amount")
    decimals = integer((row.get("uiTokenAmount") or {}).get("decimals"))
    return finite(raw) / (10**decimals) if raw is not None else 0.0


def token_deltas(meta: Mapping[str, Any], wallet: str) -> dict[str, float]:
    pre: dict[str, float] = defaultdict(float)
    post: dict[str, float] = defaultdict(float)
    for row in meta.get("preTokenBalances") or []:
        if str(row.get("owner") or "") == wallet:
            pre[str(row.get("mint") or "")] += token_amount(row)
    for row in meta.get("postTokenBalances") or []:
        if str(row.get("owner") or "") == wallet:
            post[str(row.get("mint") or "")] += token_amount(row)
    return {
        mint: post.get(mint, 0.0) - pre.get(mint, 0.0)
        for mint in set(pre) | set(post)
        if mint and abs(post.get(mint, 0.0) - pre.get(mint, 0.0)) > 1e-12
    }


def program_ids(payload: Mapping[str, Any], keys: list[str]) -> list[str]:
    message = (((payload.get("transaction") or {}).get("message")) or {})
    output: list[str] = []
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, Mapping):
            continue
        program = str(instruction.get("programId") or "")
        if not program:
            index = instruction.get("programIdIndex")
            if isinstance(index, int) and 0 <= index < len(keys):
                program = keys[index]
        if program:
            output.append(program)
    meta = payload.get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        for instruction in group.get("instructions") or []:
            if not isinstance(instruction, Mapping):
                continue
            program = str(instruction.get("programId") or "")
            if not program:
                index = instruction.get("programIdIndex")
                if isinstance(index, int) and 0 <= index < len(keys):
                    program = keys[index]
            if program:
                output.append(program)
    for line in meta.get("logMessages") or []:
        text = str(line or "")
        if text.startswith("Program ") and " invoke [" in text:
            output.append(text.split(" ", 2)[1])
    return list(dict.fromkeys(output))


def parsed_transfer_counter(payload: Mapping[str, Any]) -> Counter[str]:
    message = (((payload.get("transaction") or {}).get("message")) or {})
    counter: Counter[str] = Counter()
    groups = [message.get("instructions") or []]
    groups.extend(group.get("instructions") or [] for group in (payload.get("meta") or {}).get("innerInstructions") or [])
    for instructions in groups:
        for instruction in instructions:
            if not isinstance(instruction, Mapping):
                continue
            parsed = instruction.get("parsed")
            if isinstance(parsed, Mapping):
                kind = str(parsed.get("type") or "").lower()
                if kind:
                    counter[kind] += 1
    return counter


def classify(programs: set[str], sol_delta: float, deltas: Mapping[str, float], failed: bool) -> str:
    if failed:
        return "FAILED"
    positive = [mint for mint, value in deltas.items() if value > 0]
    negative = [mint for mint, value in deltas.items() if value < 0]
    pump = PUMP_PROGRAM_ID in programs
    pumpswap = PUMPSWAP_PROGRAM_ID in programs
    venue = "PUMP" if pump else "PUMPSWAP" if pumpswap else "TOKEN"
    if positive and sol_delta < 0:
        return f"{venue}_BUY"
    if negative and sol_delta > 0:
        return f"{venue}_SELL"
    if positive and negative:
        return "TOKEN_SWAP"
    if positive:
        return "TOKEN_RECEIVE"
    if negative:
        return "TOKEN_SEND"
    if sol_delta > 0.000001:
        return "SOL_RECEIVE"
    if sol_delta < -0.000001:
        return "SOL_SEND_OR_FEE"
    return "OTHER"


class RpcPool:
    def __init__(self, urls: list[str], *, timeout: float, concurrency: int) -> None:
        self.urls = tuple(dict.fromkeys(url for url in urls if url))
        if not self.urls:
            raise ValueError("at least one RPC URL is required")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0
        self.errors: list[str] = []

    async def __aenter__(self) -> "RpcPool":
        connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=600, keepalive_timeout=45)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def call(self, method: str, params: list[Any], *, retries: int = 3) -> Any:
        assert self.session is not None
        async with self.sem:
            last: Exception | None = None
            attempts = max(1, retries) * len(self.urls)
            for offset in range(attempts):
                url = self.urls[(self.cursor + offset) % len(self.urls)]
                self.request_id += 1
                try:
                    async with self.session.post(
                        url,
                        json={"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
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
                    self.errors.append(f"{method}@{safe_url(url)}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(1.5, 0.08 * (offset + 1)))
            raise RuntimeError(f"{method} failed: {last}")


async def signature_history_for_provider(
    url: str,
    wallet: str,
    *,
    timeout: float,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    before: str | None = None
    seen: set[str] = set()
    complete = False
    error = ""
    pages = 0
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=600, keepalive_timeout=45)
    async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
        while pages < max_pages:
            pages += 1
            options: dict[str, Any] = {"limit": 1_000, "commitment": "confirmed"}
            if before:
                options["before"] = before
            try:
                async with session.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": pages,
                        "method": "getSignaturesForAddress",
                        "params": [wallet, options],
                    },
                ) as response:
                    text = await response.text()
                    if response.status == 429 or response.status >= 500:
                        raise RuntimeError(f"HTTP {response.status}")
                    payload = json.loads(text)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    page = payload.get("result") or []
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            if not page:
                complete = True
                break
            added = 0
            for row in page:
                signature = str(row.get("signature") or "")
                if signature and signature not in seen:
                    seen.add(signature)
                    rows.append(dict(row))
                    added += 1
            next_before = str(page[-1].get("signature") or "")
            if len(page) < 1_000:
                complete = True
                break
            if not next_before or next_before == before or added == 0:
                error = "pagination made no progress"
                break
            before = next_before
    return rows, {
        "rpc": safe_url(url),
        "pages": pages,
        "signatures": len(rows),
        "complete": complete,
        "truncated_by_max_pages": pages >= max_pages and not complete,
        "error": error,
        "newest_slot": max((integer(row.get("slot")) for row in rows), default=0),
        "oldest_slot": min((integer(row.get("slot")) for row in rows), default=0),
        "newest_block_time": max((integer(row.get("blockTime")) for row in rows), default=0),
        "oldest_block_time": min((integer(row.get("blockTime")) for row in rows if row.get("blockTime")), default=0),
    }


async def fetch_details(
    pool: RpcPool,
    wallet: str,
    signatures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any] | None] = [None] * len(signatures)

    async def one(index: int, signature_row: Mapping[str, Any]) -> None:
        signature = str(signature_row.get("signature") or "")
        try:
            payload = await pool.call(
                "getTransaction",
                [
                    signature,
                    {
                        "commitment": "confirmed",
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                retries=4,
            )
        except Exception as exc:
            output[index] = {
                **dict(signature_row),
                "detail_ok": False,
                "detail_error": f"{type(exc).__name__}: {exc}",
            }
            return
        if not isinstance(payload, Mapping):
            output[index] = {**dict(signature_row), "detail_ok": False, "detail_error": "transaction unavailable"}
            return
        meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
        keys = account_keys(payload)
        wallet_index = keys.index(wallet) if wallet in keys else -1
        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        pre_lamports = integer(pre_balances[wallet_index]) if 0 <= wallet_index < len(pre_balances) else 0
        post_lamports = integer(post_balances[wallet_index]) if 0 <= wallet_index < len(post_balances) else 0
        sol_delta = (post_lamports - pre_lamports) / LAMPORTS_PER_SOL
        deltas = token_deltas(meta, wallet)
        programs = set(program_ids(payload, keys))
        header = ((((payload.get("transaction") or {}).get("message")) or {}).get("header") or {})
        required_signatures = integer(header.get("numRequiredSignatures"), 1)
        fee = integer(meta.get("fee"))
        classification = classify(programs, sol_delta, deltas, meta.get("err") is not None)
        output[index] = {
            **dict(signature_row),
            "detail_ok": True,
            "slot": integer(payload.get("slot"), integer(signature_row.get("slot"))),
            "blockTime": payload.get("blockTime", signature_row.get("blockTime")),
            "transaction_version": payload.get("version"),
            "classification": classification,
            "fee_lamports": fee,
            "estimated_priority_fee_lamports": max(0, fee - 5_000 * max(1, required_signatures)),
            "compute_units_consumed": integer(meta.get("computeUnitsConsumed")),
            "wallet_sol_delta": sol_delta,
            "wallet_pre_sol": pre_lamports / LAMPORTS_PER_SOL,
            "wallet_post_sol": post_lamports / LAMPORTS_PER_SOL,
            "token_deltas": deltas,
            "primary_mint": max(deltas, key=lambda mint: abs(deltas[mint])) if deltas else "",
            "program_ids": sorted(programs),
            "required_signatures": required_signatures,
            "fee_payer": keys[0] if keys else "",
            "wallet_is_fee_payer": bool(keys and keys[0] == wallet),
            "account_key_count": len(keys),
            "instruction_count": len((((payload.get("transaction") or {}).get("message")) or {}).get("instructions") or []),
            "inner_instruction_groups": len(meta.get("innerInstructions") or []),
            "parsed_instruction_types": dict(parsed_transfer_counter(payload)),
            "log_messages": list(meta.get("logMessages") or []),
            "error": meta.get("err"),
        }

    chunk = 250
    for start in range(0, len(signatures), chunk):
        await asyncio.gather(*(one(index, signatures[index]) for index in range(start, min(len(signatures), start + chunk))))
        print(json.dumps({"detailed": min(len(signatures), start + chunk), "target": len(signatures)}), flush=True)
    return [row for row in output if row is not None]


def summary(details: list[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = [row for row in details if row.get("detail_ok")]
    classifications = Counter(str(row.get("classification") or "UNRESOLVED") for row in details)
    buys = [row for row in resolved if str(row.get("classification") or "").endswith("_BUY")]
    sells = [row for row in resolved if str(row.get("classification") or "").endswith("_SELL")]
    priorities = [integer(row.get("estimated_priority_fee_lamports")) for row in resolved]
    compute = [integer(row.get("compute_units_consumed")) for row in resolved if integer(row.get("compute_units_consumed")) > 0]
    return {
        "signatures": len(details),
        "transactions_resolved": len(resolved),
        "resolution_rate": len(resolved) / len(details) if details else None,
        "classification_counts": dict(classifications),
        "buy_transactions": len(buys),
        "sell_transactions": len(sells),
        "unique_bought_mints": len({str(row.get("primary_mint") or "") for row in buys if row.get("primary_mint")}),
        "failed_transactions": classifications.get("FAILED", 0),
        "wallet_is_fee_payer_rate": sum(bool(row.get("wallet_is_fee_payer")) for row in resolved) / len(resolved) if resolved else None,
        "priority_fee_lamports": {
            "median": statistics.median(priorities) if priorities else None,
            "p90": sorted(priorities)[min(len(priorities) - 1, int(0.90 * (len(priorities) - 1)))] if priorities else None,
            "max": max(priorities) if priorities else None,
        },
        "compute_units": {
            "median": statistics.median(compute) if compute else None,
            "p90": sorted(compute)[min(len(compute) - 1, int(0.90 * (len(compute) - 1)))] if compute else None,
            "max": max(compute) if compute else None,
        },
        "earliest_block_time": min((integer(row.get("blockTime")) for row in details if row.get("blockTime")), default=0),
        "latest_block_time": max((integer(row.get("blockTime")) for row in details if row.get("blockTime")), default=0),
    }


async def main_async(args: argparse.Namespace) -> int:
    urls = list(args.rpc_url)
    for name in ("E4_PRIMARY_RPC_URL", "HELIUS_RPC_URL", "SOLANA_RPC_URL"):
        value = os.getenv(name, "").strip()
        if value:
            urls.insert(0, value)
    urls = list(dict.fromkeys(urls))

    provider_results = await asyncio.gather(
        *(
            signature_history_for_provider(
                url,
                args.wallet,
                timeout=args.timeout,
                max_pages=args.max_pages,
            )
            for url in urls
        )
    )
    merged: dict[str, dict[str, Any]] = {}
    provider_coverage = []
    for rows, coverage in provider_results:
        provider_coverage.append(coverage)
        for row in rows:
            signature = str(row.get("signature") or "")
            if signature:
                merged.setdefault(signature, dict(row))
    signatures = sorted(
        merged.values(),
        key=lambda row: (integer(row.get("slot")), integer(row.get("blockTime"))),
        reverse=True,
    )
    if args.max_transactions > 0:
        signatures = signatures[: args.max_transactions]

    async with RpcPool(urls, timeout=args.timeout, concurrency=args.concurrency) as pool:
        details = await fetch_details(pool, args.wallet, signatures)
        rpc_errors = pool.errors[-500:]

    payload = {
        "version": "e4-v12-full-wallet-history-v1",
        "generated_at_unix": int(time.time()),
        "wallet": args.wallet,
        "methodology": {
            "signature_pagination": "getSignaturesForAddress(limit=1000,before=cursor) independently across every configured RPC until exhaustion or max_pages",
            "transaction_resolution": "getTransaction(jsonParsed,confirmed) for every merged signature",
            "completeness_rule": "complete only when at least one provider paginated to an empty/short final page and no max-transaction truncation was applied",
            "max_pages_per_provider": args.max_pages,
            "max_transactions": args.max_transactions,
            "configured_rpc_hosts": [safe_url(url) for url in urls],
        },
        "coverage": {
            "providers": provider_coverage,
            "merged_unique_signatures": len(merged),
            "transactions_requested": len(signatures),
            "complete_available_history": bool(
                any(row.get("complete") for row in provider_coverage)
                and not any(row.get("truncated_by_max_pages") for row in provider_coverage if row.get("signatures") == max((item.get("signatures", 0) for item in provider_coverage), default=0))
                and (args.max_transactions <= 0 or args.max_transactions >= len(merged))
            ),
        },
        "summary": summary(details),
        "transactions": details,
        "rpc_detail_errors": rpc_errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"coverage": payload["coverage"], "summary": payload["summary"]}, indent=2, sort_keys=True), flush=True)
    return 0 if payload["summary"]["resolution_rate"] is not None and payload["summary"]["resolution_rate"] >= 0.95 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve and classify every E4 wallet transaction available from Solana RPC history")
    parser.add_argument("--wallet", default=E4_WALLET)
    parser.add_argument("--rpc-url", action="append", default=list(DEFAULT_RPCS))
    parser.add_argument("--timeout", type=float, default=14.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-transactions", type=int, default=0, help="0 means no transaction cap")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
