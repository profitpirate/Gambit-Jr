#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
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


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def median(values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


def uri_host(value: Any) -> str:
    try:
        return (urlparse(str(value or "").strip()).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


class RpcPool:
    def __init__(self, urls: list[str], *, timeout: float = 12.0, concurrency: int = 8) -> None:
        self.urls = tuple(dict.fromkeys(url for url in urls if url))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.sem = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0
        self.errors: list[str] = []

    async def __aenter__(self) -> "RpcPool":
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=600, keepalive_timeout=45)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def call(self, method: str, params: list[Any], *, retries: int = 3) -> Any:
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
                    self.errors.append(f"{method}@{url}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(min(1.2, 0.08 * (offset + 1)))
            raise RuntimeError(f"{method} failed: {last}")


def event_sort(row: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        integer(row.get("received_ns")),
        integer(row.get("slot")),
        integer(row.get("event_index")),
    )


def load_pair(batch_path: Path, events_path: Path) -> dict[str, Any]:
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    outcomes = {
        str(row.get("mint") or ""): {
            "pnl_sol": finite(row.get("pnl_sol")),
            "won": finite(row.get("pnl_sol")) > 0,
        }
        for row in (batch.get("actual_e4_fresh_sample") or {}).get("positions") or []
        if str(row.get("mint") or "")
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    e4_transactions: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            mint = str(row.get("mint") or "")
            if not mint:
                continue
            grouped[mint].append(row)
            if str(row.get("trader") or "") == E4_WALLET:
                e4_transactions.append(row)
    for rows in grouped.values():
        rows.sort(key=event_sort)

    launches: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for mint, rows in grouped.items():
        create = next((row for row in rows if str(row.get("kind") or "").upper() == "CREATE"), None)
        if create is None:
            continue
        raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
        creator = str(create.get("creator") or raw.get("creator") or create.get("trader") or "")
        e4_buy = next(
            (
                row
                for row in rows
                if str(row.get("trader") or "") == E4_WALLET
                and str(row.get("kind") or "").upper() in {"BUY", "PUMPSWAP_BUY"}
            ),
            None,
        )
        cutoff = integer(e4_buy.get("received_ns")) if e4_buy else 2**63 - 1
        buys: list[dict[str, Any]] = []
        sells = 0
        seed = 0.0
        outside_sol = 0.0
        buyer_order: list[str] = []
        for row in rows:
            if integer(row.get("received_ns")) > cutoff:
                break
            kind = str(row.get("kind") or "").upper()
            trader = str(row.get("trader") or "")
            if trader == E4_WALLET:
                continue
            if kind in {"SELL", "PUMPSWAP_SELL"}:
                sells += 1
                continue
            if kind not in {"BUY", "PUMPSWAP_BUY"}:
                continue
            buys.append(row)
            amount = max(0.0, finite(row.get("sol_amount")))
            if trader == creator:
                seed += amount
            elif trader:
                outside_sol += amount
                if trader not in buyer_order:
                    buyer_order.append(trader)

        last = buys[-1] if buys else create
        launch = {
            "mint": mint,
            "creator": creator,
            "create_ns": integer(create.get("received_ns")),
            "create_slot": integer(create.get("slot")),
            "create_signature": str(create.get("signature") or ""),
            "name": str(raw.get("name") or ""),
            "symbol": str(raw.get("symbol") or ""),
            "uri": str(raw.get("uri") or ""),
            "uri_host": uri_host(raw.get("uri")),
            "token_program": str(raw.get("token_program") or ""),
            "mayhem": bool(raw.get("is_mayhem_mode")),
            "cashback": bool(raw.get("is_cashback_enabled")),
            "buy_count": len(buys),
            "sell_count": sells,
            "creator_seed_sol": seed,
            "outside_sol": outside_sol,
            "first_buyers": buyer_order[:8],
            "buy_signatures": list(dict.fromkeys(str(row.get("signature") or "") for row in buys)),
            "buy_slots": [integer(row.get("slot")) for row in buys],
            "buy_amounts": [max(0.0, finite(row.get("sol_amount"))) for row in buys],
            "fdv_usd": finite(last.get("fdv_usd")),
            "price_sol": finite(last.get("price_sol")),
            "selected": e4_buy is not None,
        }
        launches[mint] = launch
        if e4_buy is not None:
            previous = buys[-1] if buys else create
            outcome = outcomes.get(mint) or {}
            entry = {
                **launch,
                "e4_entry_ns": integer(e4_buy.get("received_ns")),
                "e4_entry_slot": integer(e4_buy.get("slot")),
                "e4_entry_signature": str(e4_buy.get("signature") or ""),
                "e4_entry_sol": finite(e4_buy.get("sol_amount")),
                "e4_entry_fdv_usd": finite(e4_buy.get("fdv_usd")),
                "last_pre_entry_signature": str(previous.get("signature") or ""),
                "last_pre_entry_ns": integer(previous.get("received_ns")),
                "last_pre_entry_slot": integer(previous.get("slot")),
                "last_event_to_e4_ms": max(
                    0.0,
                    (integer(e4_buy.get("received_ns")) - integer(previous.get("received_ns"))) / 1e6,
                ),
                "same_slot_as_create": integer(e4_buy.get("slot")) == integer(create.get("slot")),
                "same_slot_as_last_event": integer(e4_buy.get("slot")) == integer(previous.get("slot")),
                "e4_pnl_sol": finite(outcome.get("pnl_sol")),
                "e4_won": bool(outcome.get("won")),
            }
            entries.append(entry)
    return {
        "batch": str(batch_path),
        "events": str(events_path),
        "launches": launches,
        "entries": entries,
        "e4_transactions": e4_transactions,
    }


def fingerprint(launch: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(launch.get("creator") or ""),
        str(launch.get("uri_host") or ""),
        tuple((launch.get("first_buyers") or [])[:3]),
        integer(launch.get("buy_count")),
        round(finite(launch.get("creator_seed_sol")), 6),
        round(finite(launch.get("outside_sol")), 6),
        round(finite(launch.get("fdv_usd")), 3),
        integer(launch.get("sell_count")),
        str(launch.get("token_program") or ""),
        bool(launch.get("mayhem")),
        bool(launch.get("cashback")),
    )


def social_values(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key).lower()
                child_path = f"{path}.{key_text}" if path else key_text
                if any(term in key_text for term in ("twitter", "telegram", "discord", "website", "social", "external", "x_url")):
                    if isinstance(child, (str, int, float)) and str(child).strip():
                        found.append(f"{child_path}={str(child).strip()}")
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload)
    return list(dict.fromkeys(found))


async def fetch_metadata(session: aiohttp.ClientSession, uri: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    if not uri:
        return {"uri": uri, "ok": False, "error": "missing URI"}
    async with sem:
        try:
            async with session.get(uri, allow_redirects=True) as response:
                body = await response.read()
                text = body.decode("utf-8", "replace")
                payload: Any = None
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    pass
                image = str(payload.get("image") or "") if isinstance(payload, Mapping) else ""
                description = str(payload.get("description") or "") if isinstance(payload, Mapping) else ""
                socials = social_values(payload) if payload is not None else []
                return {
                    "uri": uri,
                    "ok": response.status < 400,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                    "json": isinstance(payload, Mapping),
                    "keys": sorted(str(key) for key in payload) if isinstance(payload, Mapping) else [],
                    "description_length": len(description),
                    "image": image,
                    "image_host": uri_host(image),
                    "social_values": socials,
                    "has_social": bool(socials),
                }
        except Exception as exc:
            return {"uri": uri, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    output = []
    for value in message.get("accountKeys") or []:
        if isinstance(value, Mapping):
            output.append(str(value.get("pubkey") or ""))
        else:
            output.append(str(value or ""))
    return [value for value in output if value]


def transaction_summary(signature: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"signature": signature, "ok": False}
    tx = payload.get("transaction") if isinstance(payload.get("transaction"), Mapping) else {}
    message = tx.get("message") if isinstance(tx.get("message"), Mapping) else {}
    header = message.get("header") if isinstance(message.get("header"), Mapping) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    keys = account_keys(payload)
    programs = []
    parsed_transfers = []
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, Mapping):
            continue
        program_id = str(instruction.get("programId") or instruction.get("program") or "")
        if program_id:
            programs.append(program_id)
        parsed = instruction.get("parsed")
        if isinstance(parsed, Mapping) and str(parsed.get("type") or "").lower() == "transfer":
            parsed_transfers.append(parsed)
    required = integer(header.get("numRequiredSignatures"), 1)
    fee = integer(meta.get("fee"))
    return {
        "signature": signature,
        "ok": True,
        "slot": integer(payload.get("slot")),
        "block_time": payload.get("blockTime"),
        "version": payload.get("version"),
        "fee_lamports": fee,
        "estimated_priority_fee_lamports": max(0, fee - 5_000 * max(1, required)),
        "compute_units_consumed": integer(meta.get("computeUnitsConsumed")),
        "required_signatures": required,
        "fee_payer": keys[0] if keys else "",
        "account_key_count": len(keys),
        "program_ids": list(dict.fromkeys(programs)),
        "parsed_transfers": parsed_transfers,
        "error": meta.get("err"),
        "log_tail": list(meta.get("logMessages") or [])[-12:],
    }


async def rpc_forensics(entries: list[dict[str, Any]], launches: Mapping[str, Mapping[str, Any]], urls: list[str]) -> dict[str, Any]:
    slots = sorted({integer(entry.get("e4_entry_slot")) for entry in entries if integer(entry.get("e4_entry_slot")) > 0})
    target_signatures = set()
    for entry in entries:
        target_signatures.add(str(entry.get("create_signature") or ""))
        target_signatures.add(str(entry.get("e4_entry_signature") or ""))
        target_signatures.update(str(value or "") for value in entry.get("buy_signatures") or [])
    target_signatures.discard("")

    blocks: dict[int, Any] = {}
    transactions: dict[str, Any] = {}
    async with RpcPool(urls) as rpc:
        async def block(slot: int) -> None:
            try:
                blocks[slot] = await rpc.call(
                    "getBlock",
                    [
                        slot,
                        {
                            "commitment": "confirmed",
                            "encoding": "json",
                            "transactionDetails": "signatures",
                            "rewards": False,
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
            except Exception as exc:
                blocks[slot] = {"_error": f"{type(exc).__name__}: {exc}"}

        await asyncio.gather(*(block(slot) for slot in slots))

        async def transaction(signature: str) -> None:
            try:
                payload = await rpc.call(
                    "getTransaction",
                    [signature, {"commitment": "confirmed", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                )
                transactions[signature] = transaction_summary(signature, payload)
            except Exception as exc:
                transactions[signature] = {"signature": signature, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

        await asyncio.gather(*(transaction(signature) for signature in sorted(target_signatures)))
        errors = rpc.errors[-250:]

    adjacency = []
    for entry in entries:
        slot = integer(entry.get("e4_entry_slot"))
        payload = blocks.get(slot)
        signatures: list[str] = []
        if isinstance(payload, Mapping):
            raw_signatures = payload.get("signatures")
            if isinstance(raw_signatures, list):
                signatures = [str(value or "") for value in raw_signatures]
            elif isinstance(payload.get("transactions"), list):
                for row in payload["transactions"]:
                    values = ((row or {}).get("transaction") or {}).get("signatures") or []
                    if values:
                        signatures.append(str(values[0]))
        index = {signature: position for position, signature in enumerate(signatures)}
        e4_signature = str(entry.get("e4_entry_signature") or "")
        pre_signatures = [
            signature
            for signature in [str(entry.get("create_signature") or ""), *(entry.get("buy_signatures") or [])]
            if signature and signature in index
        ]
        e4_index = index.get(e4_signature)
        pre_indices = sorted({index[signature] for signature in pre_signatures})
        last_pre = max(pre_indices) if pre_indices else None
        gap = e4_index - last_pre if e4_index is not None and last_pre is not None else None
        adjacency.append(
            {
                "mint": entry.get("mint"),
                "slot": slot,
                "block_resolved": bool(signatures),
                "block_transaction_count": len(signatures),
                "e4_index": e4_index,
                "pre_indices": pre_indices,
                "last_pre_to_e4_index_gap": gap,
                "adjacent": gap == 1,
                "within_two_transactions": gap is not None and 1 <= gap <= 2,
                "within_five_transactions": gap is not None and 1 <= gap <= 5,
            }
        )
    return {"adjacency": adjacency, "transactions": transactions, "rpc_errors": errors}


async def main_async(args: argparse.Namespace) -> int:
    captures = []
    for item in args.pair:
        batch, events = item.split(":", 1)
        captures.append(load_pair(Path(batch), Path(events)))
    entries = [entry for capture in captures for entry in capture["entries"]]
    e4_transactions = [row for capture in captures for row in capture["e4_transactions"]]
    launches: dict[str, dict[str, Any]] = {}
    for capture in captures:
        launches.update(capture["launches"])

    ignored_by_fingerprint: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for launch in launches.values():
        if not launch.get("selected"):
            ignored_by_fingerprint[fingerprint(launch)].append(launch)
    indistinguishable = []
    metadata_targets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        matches = ignored_by_fingerprint.get(fingerprint(entry), [])
        indistinguishable.append(
            {
                "mint": entry["mint"],
                "e4_won": entry["e4_won"],
                "match_count": len(matches),
                "ignored_mints": [row["mint"] for row in matches],
            }
        )
        metadata_targets[entry["mint"]] = entry
        for match in matches:
            metadata_targets[match["mint"]] = match

    timeout = aiohttp.ClientTimeout(total=15.0)
    connector = aiohttp.TCPConnector(limit=24, ttl_dns_cache=600, keepalive_timeout=45)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        sem = asyncio.Semaphore(12)
        metadata_rows = await asyncio.gather(
            *(fetch_metadata(session, str(row.get("uri") or ""), sem) for row in metadata_targets.values())
        )
    metadata = {
        mint: {**row, "selected": bool(metadata_targets[mint].get("selected"))}
        for mint, row in zip(metadata_targets, metadata_rows)
    }

    rpc = await rpc_forensics(entries, launches, list(args.rpc_url))
    adjacency = rpc["adjacency"]
    resolved = [row for row in adjacency if row["block_resolved"] and row["last_pre_to_e4_index_gap"] is not None]
    selected_metadata = [metadata[mint] for mint, row in metadata_targets.items() if row.get("selected") and mint in metadata]
    ignored_metadata = [metadata[mint] for mint, row in metadata_targets.items() if not row.get("selected") and mint in metadata]

    first_partial = []
    sell_count_by_mint = Counter()
    entry_tokens = {}
    for row in sorted(e4_transactions, key=event_sort):
        mint = str(row.get("mint") or "")
        kind = str(row.get("kind") or "").upper()
        if kind in {"BUY", "PUMPSWAP_BUY"}:
            entry_tokens[mint] = finite(row.get("token_amount"))
        elif kind in {"SELL", "PUMPSWAP_SELL"}:
            sell_count_by_mint[mint] += 1
            if sell_count_by_mint[mint] == 1 and entry_tokens.get(mint, 0) > 0:
                first_partial.append(finite(row.get("token_amount")) / entry_tokens[mint])

    result = {
        "version": "e4-v12-bundle-metadata-forensics-v1",
        "coverage": {
            "launches": len(launches),
            "e4_entries": len(entries),
            "e4_wallet_transactions": len(e4_transactions),
            "e4_sells": sum(str(row.get("kind") or "").upper() in {"SELL", "PUMPSWAP_SELL"} for row in e4_transactions),
            "captured_batches": len(captures),
        },
        "entry_timing": {
            "same_slot_as_create_rate": sum(bool(row["same_slot_as_create"]) for row in entries) / len(entries) if entries else None,
            "same_slot_as_last_event_rate": sum(bool(row["same_slot_as_last_event"]) for row in entries) / len(entries) if entries else None,
            "within_1ms_of_last_event_rate": sum(row["last_event_to_e4_ms"] <= 1.0 for row in entries) / len(entries) if entries else None,
            "within_5ms_of_last_event_rate": sum(row["last_event_to_e4_ms"] <= 5.0 for row in entries) / len(entries) if entries else None,
            "within_20ms_of_last_event_rate": sum(row["last_event_to_e4_ms"] <= 20.0 for row in entries) / len(entries) if entries else None,
            "median_last_event_to_e4_ms": median([row["last_event_to_e4_ms"] for row in entries]),
        },
        "indistinguishable_onchain_fingerprints": {
            "selected_with_at_least_one_ignored_twin": sum(row["match_count"] > 0 for row in indistinguishable),
            "rate": sum(row["match_count"] > 0 for row in indistinguishable) / len(indistinguishable) if indistinguishable else None,
            "total_ignored_twins": sum(row["match_count"] for row in indistinguishable),
            "rows": indistinguishable,
        },
        "block_order": {
            "resolved": len(resolved),
            "adjacent_rate": sum(bool(row["adjacent"]) for row in resolved) / len(resolved) if resolved else None,
            "within_two_rate": sum(bool(row["within_two_transactions"]) for row in resolved) / len(resolved) if resolved else None,
            "within_five_rate": sum(bool(row["within_five_transactions"]) for row in resolved) / len(resolved) if resolved else None,
            "rows": adjacency,
        },
        "metadata": {
            "targets": len(metadata),
            "selected_fetch_ok": sum(bool(row.get("ok")) for row in selected_metadata),
            "ignored_fetch_ok": sum(bool(row.get("ok")) for row in ignored_metadata),
            "selected_social_rate": sum(bool(row.get("has_social")) for row in selected_metadata) / len(selected_metadata) if selected_metadata else None,
            "ignored_social_rate": sum(bool(row.get("has_social")) for row in ignored_metadata) / len(ignored_metadata) if ignored_metadata else None,
            "rows": metadata,
        },
        "exit_behaviour": {
            "first_partial_count": len(first_partial),
            "first_partial_median": median(first_partial),
            "first_partial_20pct": sum(abs(value - 0.20) <= 0.005 for value in first_partial),
            "first_partial_30pct": sum(abs(value - 0.30) <= 0.005 for value in first_partial),
            "sell_count_distribution": dict(sorted(Counter(sell_count_by_mint.values()).items())),
        },
        "entries": entries,
        "e4_transactions": e4_transactions,
        "rpc_transaction_summaries": rpc["transactions"],
        "rpc_errors": rpc["rpc_errors"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "coverage": result["coverage"],
        "entry_timing": result["entry_timing"],
        "indistinguishable": {key: value for key, value in result["indistinguishable_onchain_fingerprints"].items() if key != "rows"},
        "block_order": {key: value for key, value in result["block_order"].items() if key != "rows"},
        "metadata": {key: value for key, value in result["metadata"].items() if key != "rows"},
        "exit_behaviour": result["exit_behaviour"],
    }, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit E4's full captured transaction, bundle-order and metadata evidence")
    parser.add_argument("--pair", action="append", default=[], metavar="BATCH:EVENTS")
    parser.add_argument("--rpc-url", action="append", default=list(DEFAULT_RPCS))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pair:
        parser.error("at least one --pair is required")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
