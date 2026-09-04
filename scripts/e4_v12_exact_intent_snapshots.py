#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)


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


def parse_pairs(values: Sequence[str]) -> list[tuple[Path, Path]]:
    output = []
    for value in values:
        batch, events = value.split(":", 1)
        output.append((Path(batch), Path(events)))
    return output


def failure_rows(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    output: dict[str, dict[str, Any]] = {}
    for row in (payload.get("failed_attempts") or {}).get("rows") or []:
        mint = str(row.get("mapped_mint") or "")
        if mint and row.get("captured_mint") and row.get("mapping_ok", True):
            output[mint] = dict(row)
    return output


def outcomes(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("mint") or ""): dict(row)
        for row in (payload.get("actual_e4_fresh_sample") or {}).get("positions") or []
        if row.get("mint")
    }


class RpcPool:
    def __init__(self, urls: Sequence[str], *, timeout: float, concurrency: int) -> None:
        self.urls = tuple(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
        if not self.urls:
            raise ValueError("no RPC URLs configured")
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

    async def call(self, method: str, params: list[Any], retries: int = 5) -> Any:
        assert self.session is not None
        async with self.sem:
            last: Exception | None = None
            for offset in range(max(1, retries) * len(self.urls)):
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
                        body = json.loads(text)
                        if body.get("error"):
                            raise RuntimeError(str(body["error"]))
                        if body.get("result") is None:
                            raise RuntimeError("null result")
                        self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                        return body["result"]
                except Exception as exc:  # noqa: BLE001 - evidence preserves failures
                    last = exc
                    self.errors.append(f"{method}@{safe_url(url)}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(1.5, 0.05 * (offset + 1)))
            raise RuntimeError(f"{method} failed: {last}")


async def block_signature_maps(
    slots: Sequence[int],
    urls: Sequence[str],
    timeout: float,
    concurrency: int,
) -> tuple[dict[int, dict[str, int]], list[str]]:
    output: dict[int, dict[str, int]] = {}
    async with RpcPool(urls, timeout=timeout, concurrency=concurrency) as rpc:
        async def one(slot: int) -> None:
            try:
                block = await rpc.call(
                    "getBlock",
                    [
                        slot,
                        {
                            "commitment": "confirmed",
                            "transactionDetails": "signatures",
                            "rewards": False,
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
                signatures = block.get("signatures") or []
                output[slot] = {
                    str(signature): index
                    for index, signature in enumerate(signatures)
                    if signature
                }
            except Exception as exc:  # noqa: BLE001
                rpc.errors.append(f"slot={slot}: {type(exc).__name__}: {exc}")

        ordered = sorted(set(integer(slot) for slot in slots if integer(slot) > 0))
        for start in range(0, len(ordered), 50):
            await asyncio.gather(*(one(slot) for slot in ordered[start : start + 50]))
            print(json.dumps({"blocks": min(len(ordered), start + 50), "target": len(ordered)}), flush=True)
        return output, rpc.errors[-300:]


def event_before_intent(
    event: Mapping[str, Any],
    intent: Mapping[str, Any],
    signature_maps: Mapping[int, Mapping[str, int]],
) -> tuple[bool, str]:
    slot = integer(event.get("slot"))
    intent_slot = integer(intent.get("slot"))
    if slot < intent_slot:
        return True, "earlier_slot"
    if slot > intent_slot:
        return False, "later_slot"
    signature = str(event.get("signature") or "")
    intent_signature = str(intent.get("signature") or "")
    indices = signature_maps.get(slot) or {}
    event_index = indices.get(signature)
    intent_index = integer(intent.get("transaction_index"), -1)
    if intent_index < 0 and intent_signature:
        intent_index = integer(indices.get(intent_signature), -1)
    if event_index is not None and intent_index >= 0:
        if event_index < intent_index:
            return True, "same_slot_exact_index"
        if event_index > intent_index:
            return False, "same_slot_exact_index"
        # Same transaction: CREATE/creator seed events before E4's inner event
        # are kept only when event_index order is explicitly lower.
        return integer(event.get("event_index"), 0) < integer(intent.get("event_index"), 0), "same_transaction_event_index"
    # For successful BUYs, receive ordering is a conservative fallback. For a
    # failed transaction there is no emitted event, so unknown same-slot order
    # is excluded rather than leaking future public flow into the snapshot.
    if intent.get("kind") == "SUCCESS":
        return integer(event.get("received_ns")) < integer(intent.get("received_ns")), "same_slot_receive_fallback"
    return False, "same_slot_unknown_excluded"


def snapshot(
    launch: Mapping[str, Any],
    intent: Mapping[str, Any],
    signature_maps: Mapping[int, Mapping[str, int]],
) -> dict[str, Any]:
    included = []
    ordering = Counter()
    for event in launch.get("events") or []:
        if str(event.get("trader") or "") == E4_WALLET:
            continue
        keep, method = event_before_intent(event, intent, signature_maps)
        ordering[method] += 1
        if keep:
            included.append(event)
    included.sort(
        key=lambda event: (
            integer(event.get("slot")),
            integer((signature_maps.get(integer(event.get("slot"))) or {}).get(str(event.get("signature") or "")), 10**9),
            integer(event.get("event_index")),
            integer(event.get("received_ns")),
        )
    )
    buys = [event for event in included if str(event.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}]
    sells = [event for event in included if str(event.get("kind") or "").upper() in {"SELL", "PUMPSWAP_SELL"}]
    creator = str(launch.get("creator") or "")
    creator_buys = [event for event in buys if str(event.get("trader") or "") == creator]
    outsiders = [event for event in buys if str(event.get("trader") or "") and str(event.get("trader") or "") != creator]
    fdv = next((finite(event.get("fdv_usd")) for event in reversed(buys) if finite(event.get("fdv_usd")) > 0), finite(launch.get("create_fdv_usd")))
    price = next((finite(event.get("price_sol")) for event in reversed(buys) if finite(event.get("price_sol")) > 0), finite(launch.get("create_price_sol")))
    return {
        "creator_seed_sol": sum(max(0.0, finite(event.get("sol_amount"))) for event in creator_buys),
        "outside_sol": sum(max(0.0, finite(event.get("sol_amount"))) for event in outsiders),
        "buy_count": len(buys),
        "outside_buy_count": len(outsiders),
        "unique_outside_buyers": len({str(event.get("trader") or "") for event in outsiders}),
        "sell_count": len(sells),
        "fdv_usd": fdv,
        "price_sol": price,
        "first_buyers": [str(event.get("trader") or "") for event in outsiders[:10]],
        "first_buy_amounts_sol": [finite(event.get("sol_amount")) for event in outsiders[:10]],
        "first_buy_signatures": [str(event.get("signature") or "") for event in outsiders[:10]],
        "same_create_slot_buy_count": sum(integer(event.get("slot")) == integer(launch.get("create_slot")) for event in buys),
        "same_create_slot_outside_count": sum(integer(event.get("slot")) == integer(launch.get("create_slot")) for event in outsiders),
        "last_pre_intent_ns": max([integer(launch.get("create_ns"))] + [integer(event.get("received_ns")) for event in included]),
        "ordering_methods": dict(ordering),
    }


def load_launches(pairs: Sequence[tuple[Path, Path]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    launches: dict[str, dict[str, Any]] = {}
    result_outcomes: dict[str, dict[str, Any]] = {}
    for run_index, (batch_path, events_path) in enumerate(pairs):
        run = events_path.parts[-3] if len(events_path.parts) >= 3 else str(run_index)
        result_outcomes.update(outcomes(batch_path))
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                mint = str(row.get("mint") or "")
                kind = str(row.get("kind") or "").upper()
                raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
                if kind == "CREATE":
                    launches[mint] = {
                        "mint": mint,
                        "run": run,
                        "run_index": run_index,
                        "create_ns": integer(row.get("received_ns")),
                        "create_slot": integer(row.get("slot")),
                        "creator": str(row.get("creator") or raw.get("creator") or row.get("trader") or ""),
                        "uri": str(raw.get("uri") or ""),
                        "name": str(raw.get("name") or ""),
                        "symbol": str(raw.get("symbol") or ""),
                        "cashback": bool(raw.get("is_cashback_enabled")),
                        "mayhem": bool(raw.get("is_mayhem_mode")),
                        "token_program": str(raw.get("token_program") or ""),
                        "create_fdv_usd": finite(row.get("fdv_usd")),
                        "create_price_sol": finite(row.get("price_sol")),
                        "events": [],
                    }
                launch = launches.get(mint)
                if launch is None:
                    continue
                if kind in {"BUY", "SELL", "PUMPSWAP_BUY", "PUMPSWAP_SELL"}:
                    launch["events"].append({
                        "kind": kind,
                        "received_ns": integer(row.get("received_ns")),
                        "slot": integer(row.get("slot")),
                        "event_index": integer(row.get("event_index")),
                        "signature": str(row.get("signature") or ""),
                        "trader": str(row.get("trader") or ""),
                        "sol_amount": finite(row.get("sol_amount")),
                        "token_amount": finite(row.get("token_amount")),
                        "price_sol": finite(row.get("price_sol")),
                        "fdv_usd": finite(row.get("fdv_usd")),
                    })
    return launches, result_outcomes


def successful_intents(launches: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output = {}
    for mint, launch in launches.items():
        for event in launch.get("events") or []:
            if str(event.get("trader") or "") == E4_WALLET and str(event.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}:
                output[mint] = {
                    "kind": "SUCCESS",
                    "slot": integer(event.get("slot")),
                    "signature": str(event.get("signature") or ""),
                    "transaction_index": -1,
                    "event_index": integer(event.get("event_index")),
                    "received_ns": integer(event.get("received_ns")),
                    "entry_sol": finite(event.get("sol_amount")),
                    "entry_fdv_usd": finite(event.get("fdv_usd")),
                }
                break
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconstruct every E4 intent at exact block transaction order")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc-url", action="append", default=list(DEFAULT_RPCS))
    parser.add_argument("--timeout", type=float, default=14.0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")

    urls = list(args.rpc_url)
    for name in ("E4_PRIMARY_RPC_URL", "HELIUS_RPC_URL", "SOLANA_RPC_URL"):
        value = os.getenv(name, "").strip()
        if value:
            urls.insert(0, value)
    pairs = parse_pairs(args.pair)
    launches, result_outcomes = load_launches(pairs)
    successes = successful_intents(launches)
    failures = failure_rows(args.attempts)
    intents = dict(successes)
    for mint, row in failures.items():
        if mint not in launches or mint in intents:
            continue
        intents[mint] = {
            "kind": "FAILED_ATTEMPT",
            "slot": integer(row.get("attempt_slot")),
            "signature": str(row.get("signature") or ""),
            "transaction_index": integer(row.get("attempt_transaction_index"), -1),
            "event_index": 0,
            "received_ns": 0,
            "entry_sol": 0.0,
            "entry_fdv_usd": 0.0,
            "shortfall_fraction": row.get("shortfall_fraction"),
        }

    signature_maps, rpc_errors = asyncio.run(
        block_signature_maps(
            [integer(intent.get("slot")) for intent in intents.values()],
            urls,
            args.timeout,
            args.concurrency,
        )
    )
    rows = []
    for mint, intent in sorted(intents.items(), key=lambda item: (integer(item[1].get("slot")), item[0])):
        launch = launches[mint]
        indices = signature_maps.get(integer(intent.get("slot"))) or {}
        if integer(intent.get("transaction_index"), -1) < 0 and intent.get("signature"):
            intent["transaction_index"] = integer(indices.get(str(intent.get("signature") or "")), -1)
        before = snapshot(launch, intent, signature_maps)
        result = result_outcomes.get(mint) or {}
        rows.append({
            "mint": mint,
            "run": launch.get("run"),
            "run_index": launch.get("run_index"),
            "label": intent.get("kind"),
            "creator": launch.get("creator"),
            "uri": launch.get("uri"),
            "name": launch.get("name"),
            "symbol": launch.get("symbol"),
            "cashback": launch.get("cashback"),
            "mayhem": launch.get("mayhem"),
            "token_program": launch.get("token_program"),
            "create_ns": launch.get("create_ns"),
            "create_slot": launch.get("create_slot"),
            "intent_slot": intent.get("slot"),
            "intent_transaction_index": intent.get("transaction_index"),
            "intent_signature": intent.get("signature"),
            "intent_received_ns": intent.get("received_ns"),
            "intent_age_slots": integer(intent.get("slot")) - integer(launch.get("create_slot")),
            "intent_age_ms": (
                (integer(intent.get("received_ns")) - integer(launch.get("create_ns"))) / 1e6
                if integer(intent.get("received_ns")) > 0
                else None
            ),
            "e4_entry_sol": intent.get("entry_sol"),
            "e4_entry_fdv_usd": intent.get("entry_fdv_usd"),
            "shortfall_fraction": intent.get("shortfall_fraction"),
            "e4_pnl_sol": finite(result.get("pnl_sol")),
            "e4_won": finite(result.get("pnl_sol")) > 0,
            "block_order_resolved": bool(indices and integer(intent.get("transaction_index"), -1) >= 0),
            **before,
        })

    payload = {
        "version": "e4-v12-exact-intent-snapshots-v1",
        "methodology": {
            "positive_definition": "successful E4 Pump BUY plus mapped failed E4 Pump BUY attempts",
            "same_slot_order": "getBlock(transactionDetails=signatures) index; unknown failed-attempt ordering is excluded",
            "anti_leakage": "only transactions strictly before E4's successful or failed buy transaction are included",
        },
        "coverage": {
            "captured_launches": len(launches),
            "successful_intents": len(successes),
            "failed_intents": sum(row.get("label") == "FAILED_ATTEMPT" for row in rows),
            "intent_rows": len(rows),
            "unique_intent_slots": len({integer(row.get("intent_slot")) for row in rows}),
            "blocks_resolved": len(signature_maps),
            "exact_order_rows": sum(bool(row.get("block_order_resolved")) for row in rows),
            "exact_order_rate": sum(bool(row.get("block_order_resolved")) for row in rows) / len(rows) if rows else None,
        },
        "rows": rows,
        "rpc_errors": rpc_errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["coverage"], indent=2, sort_keys=True), flush=True)
    return 0 if payload["coverage"]["exact_order_rate"] is not None and payload["coverage"]["exact_order_rate"] >= 0.90 else 2


if __name__ == "__main__":
    raise SystemExit(main())
