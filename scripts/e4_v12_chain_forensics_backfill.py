#!/usr/bin/env python3
"""Backfill exact block order, fee payers and pre-launch creator funders.

The input is the immutable V2 choice-risk-set JSONL.  Only public Solana RPC
methods are used.  Every relationship is timestamp checked against decision_ns;
post-decision activity is never converted into a causal feature.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

VERSION = "e4-v12-chain-forensics-backfill-v1"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdY9rKXbX7mBfYvKz2zZ1zV7P3GmZrM7vQqWk",
}


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def account_key_strings(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    output = []
    for item in message.get("accountKeys") or []:
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, Mapping):
            output.append(str(item.get("pubkey") or ""))
        else:
            output.append(str(item))
    return output


def signer_indexes(transaction: Mapping[str, Any]) -> set[int]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    output = set()
    for index, item in enumerate(keys):
        if isinstance(item, Mapping) and bool(item.get("signer")):
            output.add(index)
    if output:
        return output
    header = message.get("header") or {}
    required = integer(header.get("numRequiredSignatures"))
    return set(range(max(0, required)))


def block_time_ns(transaction: Mapping[str, Any]) -> int | None:
    value = transaction.get("blockTime")
    return integer(value) * 1_000_000_000 if value is not None else None


def infer_funding_transfer(
    transaction: Mapping[str, Any], creator: str, decision_ns: int
) -> dict[str, Any] | None:
    tx_ns = block_time_ns(transaction)
    if tx_ns is None or tx_ns >= decision_ns:
        return None
    keys = account_key_strings(transaction)
    if creator not in keys:
        return None
    meta = transaction.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if len(pre) != len(post) or len(keys) < len(pre):
        return None
    creator_index = keys.index(creator)
    creator_delta = integer(post[creator_index]) - integer(pre[creator_index])
    if creator_delta <= 0:
        return None
    signers = signer_indexes(transaction)
    payer_candidates = []
    for index, key in enumerate(keys[: len(pre)]):
        if key == creator or key in TOKEN_PROGRAMS or key == SYSTEM_PROGRAM:
            continue
        delta = integer(post[index]) - integer(pre[index])
        if delta < 0:
            payer_candidates.append(
                (
                    -delta,
                    index in signers,
                    index == 0,
                    key,
                    delta,
                )
            )
    if not payer_candidates:
        return None
    payer_candidates.sort(reverse=True)
    _, is_signer, is_fee_payer, funder, funder_delta = payer_candidates[0]
    transfer_lamports = min(creator_delta, -funder_delta)
    if transfer_lamports <= 0:
        return None
    signature = ""
    signatures = ((transaction.get("transaction") or {}).get("signatures") or [])
    if signatures:
        signature = str(signatures[0])
    return {
        "creator": creator,
        "funder": funder,
        "funding_signature": signature,
        "funding_slot": integer(transaction.get("slot")),
        "funding_block_time_ns": tx_ns,
        "funding_age_ms": (decision_ns - tx_ns) / 1_000_000,
        "estimated_transfer_sol": transfer_lamports / 1_000_000_000,
        "funder_is_signer": is_signer,
        "funder_is_fee_payer": is_fee_payer,
        "method": "pre_post_lamport_delta",
    }


@dataclass(frozen=True)
class Group:
    group_id: str
    decision_ns: int
    decision_slot: int
    selected_signature: str
    selected_creator: str
    selected_create_signature: str
    rows: tuple[dict[str, Any], ...]


def load_groups(path: Path) -> list[Group]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            grouped[str(row.get("decision_group_id") or "")].append(row)
    output = []
    for group_id, rows in grouped.items():
        selected = [row for row in rows if bool(row.get("selected_by_e4"))]
        if len(selected) != 1:
            raise ValueError(f"group {group_id} has {len(selected)} selected rows")
        chosen = selected[0]
        output.append(
            Group(
                group_id=group_id,
                decision_ns=integer(chosen.get("decision_ns")),
                decision_slot=integer(chosen.get("decision_slot")),
                selected_signature=str(chosen.get("decision_signature") or ""),
                selected_creator=str(chosen.get("creator_address") or ""),
                selected_create_signature=str(chosen.get("candidate_create_signature") or ""),
                rows=tuple(rows),
            )
        )
    output.sort(key=lambda group: (group.decision_ns, group.group_id))
    return output


class RpcClient:
    def __init__(self, url: str, concurrency: int, requests_per_second: float) -> None:
        self.url = url
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.minimum_interval = 1.0 / max(0.1, requests_per_second)
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        self.session: aiohttp.ClientSession | None = None
        self.counter = 0

    async def __aenter__(self) -> "RpcClient":
        timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=35)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = self.minimum_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def call(self, method: str, params: Sequence[Any]) -> Any:
        if self.session is None:
            raise RuntimeError("RPC session is not open")
        async with self.semaphore:
            for attempt in range(7):
                await self._throttle()
                self.counter += 1
                request_id = self.counter
                try:
                    async with self.session.post(
                        self.url,
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": list(params),
                        },
                    ) as response:
                        body = await response.text()
                        if response.status == 429:
                            await asyncio.sleep(min(30.0, 1.5**attempt + random.random()))
                            continue
                        if response.status >= 500:
                            await asyncio.sleep(min(20.0, 1.4**attempt))
                            continue
                        if response.status != 200:
                            raise RuntimeError(f"RPC HTTP {response.status}: {body[:300]}")
                        payload = json.loads(body)
                        if payload.get("error"):
                            error = payload["error"]
                            code = integer(error.get("code")) if isinstance(error, Mapping) else 0
                            if code in {-32005, -32004, -32009, -32603} and attempt < 6:
                                await asyncio.sleep(min(20.0, 1.4**attempt))
                                continue
                            raise RuntimeError(f"RPC {method} error: {error}")
                        return payload.get("result")
                except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                    if attempt >= 6:
                        raise RuntimeError(f"RPC {method} failed after retries: {exc}") from exc
                    await asyncio.sleep(min(20.0, 1.5**attempt + random.random()))
        raise RuntimeError(f"RPC {method} retry exhaustion")


async def resolve_block_order(
    client: RpcClient, groups: Sequence[Group]
) -> tuple[dict[int, dict[str, int]], list[dict[str, Any]]]:
    slots = sorted({group.decision_slot for group in groups if group.decision_slot > 0})
    mapping: dict[int, dict[str, int]] = {}
    failures: list[dict[str, Any]] = []

    async def one(slot: int) -> None:
        try:
            result = await client.call(
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
            signatures = (result or {}).get("signatures") or []
            mapping[slot] = {str(signature): index for index, signature in enumerate(signatures)}
        except Exception as exc:  # noqa: BLE001 - complete audit records failures
            failures.append({"slot": slot, "error": str(exc)})

    await asyncio.gather(*(one(slot) for slot in slots))
    return mapping, failures


async def get_transaction(client: RpcClient, signature: str) -> dict[str, Any] | None:
    if not signature:
        return None
    result = await client.call(
        "getTransaction",
        [
            signature,
            {
                "commitment": "confirmed",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    )
    return dict(result) if isinstance(result, Mapping) else None


async def resolve_creator_funder(
    client: RpcClient,
    creator: str,
    create_signature: str,
    decision_ns: int,
    transaction_cache: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not creator or not create_signature:
        return None, {"creator": creator, "reason": "MISSING_CREATOR_OR_CREATE_SIGNATURE"}
    try:
        signatures = await client.call(
            "getSignaturesForAddress",
            [
                creator,
                {
                    "before": create_signature,
                    "limit": 25,
                    "commitment": "confirmed",
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return None, {"creator": creator, "reason": "SIGNATURE_HISTORY_FAILURE", "error": str(exc)}
    for row in signatures or []:
        signature = str((row or {}).get("signature") or "")
        if not signature:
            continue
        try:
            if signature not in transaction_cache:
                transaction_cache[signature] = await get_transaction(client, signature)
            transaction = transaction_cache[signature]
        except Exception:
            continue
        if transaction is None:
            continue
        inferred = infer_funding_transfer(transaction, creator, decision_ns)
        if inferred is not None:
            return inferred, None
    return None, {"creator": creator, "reason": "NO_PRELAUNCH_LAMPORT_FUNDER_FOUND"}


def creators_by_priority(groups: Sequence[Group], maximum: int) -> list[tuple[str, str, int]]:
    selected = []
    alternatives = Counter()
    earliest: dict[str, tuple[str, int]] = {}
    for group in groups:
        if group.selected_creator and group.selected_create_signature:
            selected.append((group.selected_creator, group.selected_create_signature, group.decision_ns))
        for row in group.rows:
            if bool(row.get("selected_by_e4")):
                continue
            creator = str(row.get("creator_address") or "")
            signature = str(row.get("candidate_create_signature") or "")
            decision_ns = integer(row.get("decision_ns"))
            if not creator or not signature:
                continue
            alternatives[creator] += 1
            earliest.setdefault(creator, (signature, decision_ns))
    output = []
    seen = set()
    for creator, signature, decision_ns in selected:
        if creator in seen:
            continue
        seen.add(creator)
        output.append((creator, signature, decision_ns))
    for creator, _ in alternatives.most_common():
        if creator in seen:
            continue
        signature, decision_ns = earliest[creator]
        seen.add(creator)
        output.append((creator, signature, decision_ns))
        if len(output) >= maximum:
            break
    return output[:maximum]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    groups = load_groups(args.input)
    transaction_cache: dict[str, dict[str, Any] | None] = {}
    async with RpcClient(args.rpc_url, args.concurrency, args.requests_per_second) as client:
        block_order, block_failures = await resolve_block_order(client, groups)
        order_rows = []
        selected_order_resolved = 0
        same_slot_alternative_resolved = 0
        same_slot_alternative_total = 0
        for group in groups:
            slot_map = block_order.get(group.decision_slot, {})
            selected_index = None
            for signature in (group.selected_signature, group.selected_create_signature):
                if signature and signature in slot_map:
                    selected_index = slot_map[signature]
                    break
            if selected_index is not None:
                selected_order_resolved += 1
            alternatives = []
            for row in group.rows:
                if bool(row.get("selected_by_e4")) or not bool(row.get("same_slot_alternative")):
                    continue
                same_slot_alternative_total += 1
                signature = str(row.get("candidate_create_signature") or "")
                index = slot_map.get(signature)
                if index is not None:
                    same_slot_alternative_resolved += 1
                alternatives.append(
                    {
                        "mint": str(row.get("mint") or ""),
                        "signature": signature,
                        "transaction_index": index,
                        "relative_to_selected": (
                            None
                            if index is None or selected_index is None
                            else index - selected_index
                        ),
                    }
                )
            order_rows.append(
                {
                    "decision_group_id": group.group_id,
                    "slot": group.decision_slot,
                    "selected_signature": group.selected_signature,
                    "selected_create_signature": group.selected_create_signature,
                    "selected_transaction_index": selected_index,
                    "same_slot_alternatives": alternatives,
                }
            )

        creator_targets = creators_by_priority(groups, args.max_creators)
        funders = []
        funder_failures = []
        semaphore = asyncio.Semaphore(max(1, args.funder_concurrency))

        async def one_creator(item: tuple[str, str, int]) -> None:
            creator, signature, decision_ns = item
            async with semaphore:
                funder, failure = await resolve_creator_funder(
                    client,
                    creator,
                    signature,
                    decision_ns,
                    transaction_cache,
                )
            if funder is not None:
                funders.append(funder)
            if failure is not None:
                funder_failures.append(failure)

        await asyncio.gather(*(one_creator(item) for item in creator_targets))

    funder_counts = Counter(row["funder"] for row in funders)
    creator_funder_pairs = Counter((row["creator"], row["funder"]) for row in funders)
    report = {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "path": str(args.input),
            "decision_groups": len(groups),
            "rows": sum(len(group.rows) for group in groups),
        },
        "rpc": {
            "provider": "configured Solana JSON-RPC endpoint",
            "url_redacted": args.rpc_url.split("?")[0],
            "methods": ["getBlock", "getSignaturesForAddress", "getTransaction"],
        },
        "block_order": {
            "decision_slots_requested": len({group.decision_slot for group in groups}),
            "decision_slots_resolved": len(block_order),
            "selected_transaction_indexes_resolved": selected_order_resolved,
            "same_slot_alternatives_total": same_slot_alternative_total,
            "same_slot_alternative_indexes_resolved": same_slot_alternative_resolved,
            "failures": block_failures,
            "groups": order_rows,
        },
        "funders": {
            "creators_requested": len(creator_targets),
            "creators_resolved": len(funders),
            "unique_funders": len(funder_counts),
            "recurring_funders": [
                {"funder": funder, "creator_count": count}
                for funder, count in funder_counts.most_common()
                if count >= 2
            ],
            "relationships": sorted(funders, key=lambda row: (row["creator"], row["funding_block_time_ns"])),
            "failures": funder_failures,
            "creator_funder_pair_count": len(creator_funder_pairs),
        },
        "causality": {
            "post_decision_funding_relationships_included": 0,
            "decision_timestamp_check": "funding_block_time_ns < decision_ns",
        },
        "production_paths_changed": 0,
        "live_trading": False,
    }
    return report


def rpc_url() -> str:
    explicit = os.getenv("SOLANA_RPC_URL", "").strip()
    if explicit:
        return explicit
    helius = os.getenv("HELIUS_API_KEY", "").strip()
    if helius:
        return f"https://mainnet.helius-rpc.com/?api-key={helius}"
    return "https://api.mainnet-beta.solana.com"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc-url", default=rpc_url())
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--funder-concurrency", type=int, default=4)
    parser.add_argument("--requests-per-second", type=float, default=6.0)
    parser.add_argument("--max-creators", type=int, default=1_500)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    write_json(args.output, report)
    summary = {
        "version": report["version"],
        "decision_slots_resolved": report["block_order"]["decision_slots_resolved"],
        "selected_transaction_indexes_resolved": report["block_order"]["selected_transaction_indexes_resolved"],
        "same_slot_alternative_indexes_resolved": report["block_order"]["same_slot_alternative_indexes_resolved"],
        "creators_requested": report["funders"]["creators_requested"],
        "creators_resolved": report["funders"]["creators_resolved"],
        "unique_funders": report["funders"]["unique_funders"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
