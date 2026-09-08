#!/usr/bin/env python3
"""Backfill causal create-transaction infrastructure fingerprints.

E4 may recognise launch infrastructure rather than token-level market features.
This audit resolves selected launches and their strongest hard negatives to
fee-payer, signer, program, compute-budget and account-layout fingerprints using
public Solana transaction history.  No post-decision transaction is used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.e4_v12_chain_forensics_backfill import (
    RpcClient,
    account_key_strings,
    get_transaction,
    integer,
    rpc_url,
    signer_indexes,
    write_json,
)

VERSION = "e4-v12-chain-fingerprint-backfill-v1"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def fingerprint_transaction(transaction: Mapping[str, Any]) -> dict[str, Any]:
    envelope = transaction.get("transaction") or {}
    message = envelope.get("message") or {}
    meta = transaction.get("meta") or {}
    keys = account_key_strings(transaction)
    signer_positions = signer_indexes(transaction)
    signers = [keys[index] for index in sorted(signer_positions) if index < len(keys)]
    instructions = message.get("instructions") or []
    program_ids = []
    compute_budget_payloads = []
    parsed_types = []
    for instruction in instructions:
        if not isinstance(instruction, Mapping):
            continue
        program_id = str(
            instruction.get("programId")
            or instruction.get("programIdIndex")
            or instruction.get("program")
            or ""
        )
        if program_id:
            program_ids.append(program_id)
        parsed = instruction.get("parsed")
        if isinstance(parsed, Mapping):
            parsed_type = str(parsed.get("type") or "")
            if parsed_type:
                parsed_types.append(parsed_type)
        if program_id == COMPUTE_BUDGET_PROGRAM:
            compute_budget_payloads.append(
                str(instruction.get("data") or compact(parsed or {}))
            )
    inner = meta.get("innerInstructions") or []
    inner_count = sum(
        len(row.get("instructions") or [])
        for row in inner
        if isinstance(row, Mapping)
    )
    loaded = meta.get("loadedAddresses") or {}
    loaded_writable = loaded.get("writable") or [] if isinstance(loaded, Mapping) else []
    loaded_readonly = loaded.get("readonly") or [] if isinstance(loaded, Mapping) else []
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    payer_delta = (
        integer(post[0]) - integer(pre[0])
        if pre and post and len(post) == len(pre)
        else None
    )
    raw_contract = {
        "fee_payer": keys[0] if keys else "",
        "signers": signers,
        "program_ids": program_ids,
        "parsed_instruction_types": parsed_types,
        "compute_budget_payloads": compute_budget_payloads,
        "account_count": len(keys),
        "instruction_count": len(instructions),
        "inner_instruction_count": inner_count,
        "loaded_writable_count": len(loaded_writable),
        "loaded_readonly_count": len(loaded_readonly),
        "version": transaction.get("version"),
    }
    digest = hashlib.sha256(compact(raw_contract).encode()).hexdigest()
    return {
        **raw_contract,
        "fee_lamports": integer(meta.get("fee")),
        "fee_payer_balance_delta_lamports": payer_delta,
        "transaction_error": meta.get("err"),
        "compute_units_consumed": meta.get("computeUnitsConsumed"),
        "fingerprint_sha256": digest,
    }


def load_targets(path: Path, maximum_alternatives_per_group: int) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                groups[str(row.get("decision_group_id") or "")].append(row)
    targets: dict[str, dict[str, Any]] = {}
    target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    preferred_categories = {
        "SAME_SLOT_ALTERNATIVE": 0,
        "NEAREST_TIME_ALTERNATIVE": 1,
        "MATCHED_IDENTITY_ALTERNATIVE": 2,
        "MATCHED_CREATOR_HISTORY_ALTERNATIVE": 3,
        "MATCHED_BUYER_CLUSTER_ALTERNATIVE": 4,
        "MATCHED_TOPOLOGY_ALTERNATIVE": 5,
        "TOP_MARKET_ALTERNATIVE": 6,
        "ACTIVE_EXECUTABLE_ALTERNATIVE": 7,
    }
    for group_id, rows in groups.items():
        selected = [row for row in rows if bool(row.get("selected_by_e4"))]
        if len(selected) != 1:
            raise ValueError(f"group {group_id} selected row count {len(selected)}")
        chosen = selected[0]
        ranked = []
        for row in rows:
            if bool(row.get("selected_by_e4")):
                continue
            categories = row.get("hard_negative_categories") or []
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except json.JSONDecodeError:
                    categories = []
            priority = min(
                (preferred_categories.get(str(category), 99) for category in categories),
                default=99,
            )
            ranked.append(
                (
                    priority,
                    float(row.get("candidate_age_ms") or 0),
                    str(row.get("mint") or ""),
                    row,
                )
            )
        ranked.sort(key=lambda item: item[:3])
        included = [chosen] + [item[3] for item in ranked[:maximum_alternatives_per_group]]
        for row in included:
            signature = str(row.get("candidate_create_signature") or "")
            if not signature:
                continue
            target = {
                "signature": signature,
                "mint": str(row.get("mint") or ""),
                "creator": str(row.get("creator_address") or ""),
                "decision_ns": integer(row.get("decision_ns")),
                "selected_by_e4": bool(row.get("selected_by_e4")),
                "source_run_id": str(row.get("source_run_id") or ""),
            }
            targets.setdefault(signature, target)
            target_groups[group_id].append(target)
    return list(targets.values()), target_groups


async def run(args: argparse.Namespace) -> dict[str, Any]:
    targets, groups = load_targets(args.input, args.maximum_alternatives_per_group)
    resolved: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async with RpcClient(args.rpc_url, args.concurrency, args.requests_per_second) as client:
        async def one(target: Mapping[str, Any]) -> None:
            signature = str(target["signature"])
            async with semaphore:
                try:
                    transaction = await get_transaction(client, signature)
                    if transaction is None:
                        failures.append({**target, "reason": "TRANSACTION_NOT_FOUND"})
                        return
                    block_time = transaction.get("blockTime")
                    block_time_ns = integer(block_time) * 1_000_000_000 if block_time is not None else None
                    # Block time is second-resolution and may round to the same
                    # second as decision_ns.  It is retained as provenance but
                    # cannot by itself prove sub-second ordering.
                    resolved[signature] = {
                        **target,
                        "slot": integer(transaction.get("slot")),
                        "block_time_ns": block_time_ns,
                        **fingerprint_transaction(transaction),
                    }
                except Exception as exc:  # noqa: BLE001 - every miss is audited
                    failures.append({**target, "reason": "RPC_FAILURE", "error": str(exc)})

        await asyncio.gather(*(one(target) for target in targets))

    fee_payer_selected: Counter[str] = Counter()
    fee_payer_ignored: Counter[str] = Counter()
    fingerprint_selected: Counter[str] = Counter()
    fingerprint_ignored: Counter[str] = Counter()
    for row in resolved.values():
        fee_payer = str(row.get("fee_payer") or "")
        fingerprint = str(row.get("fingerprint_sha256") or "")
        if row.get("selected_by_e4"):
            fee_payer_selected[fee_payer] += 1
            fingerprint_selected[fingerprint] += 1
        else:
            fee_payer_ignored[fee_payer] += 1
            fingerprint_ignored[fingerprint] += 1

    group_rows = []
    for group_id, members in groups.items():
        enriched = []
        for member in members:
            signature = str(member["signature"])
            fingerprint = resolved.get(signature)
            enriched.append(
                {
                    **member,
                    "resolved": fingerprint is not None,
                    "fee_payer": (fingerprint or {}).get("fee_payer"),
                    "signers": (fingerprint or {}).get("signers"),
                    "program_ids": (fingerprint or {}).get("program_ids"),
                    "compute_budget_payloads": (fingerprint or {}).get("compute_budget_payloads"),
                    "fingerprint_sha256": (fingerprint or {}).get("fingerprint_sha256"),
                    "fee_lamports": (fingerprint or {}).get("fee_lamports"),
                    "compute_units_consumed": (fingerprint or {}).get("compute_units_consumed"),
                }
            )
        group_rows.append({"decision_group_id": group_id, "candidates": enriched})

    return {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "path": str(args.input),
            "targets": len(targets),
            "decision_groups": len(groups),
            "maximum_alternatives_per_group": args.maximum_alternatives_per_group,
        },
        "coverage": {
            "transactions_resolved": len(resolved),
            "transactions_failed": len(failures),
            "selected_resolved": sum(bool(row.get("selected_by_e4")) for row in resolved.values()),
            "alternative_resolved": sum(not bool(row.get("selected_by_e4")) for row in resolved.values()),
        },
        "recurrence": {
            "fee_payers_selected": fee_payer_selected.most_common(),
            "fee_payers_ignored": fee_payer_ignored.most_common(),
            "fingerprints_selected": fingerprint_selected.most_common(),
            "fingerprints_ignored": fingerprint_ignored.most_common(),
            "selected_only_fee_payers": sorted(
                key for key in fee_payer_selected if key and not fee_payer_ignored[key]
            ),
            "selected_only_fingerprints": sorted(
                key for key in fingerprint_selected if key and not fingerprint_ignored[key]
            ),
        },
        "transactions": sorted(resolved.values(), key=lambda row: (row["source_run_id"], row["signature"])),
        "groups": group_rows,
        "failures": failures,
        "causality": {
            "current_create_transaction_only": True,
            "future_transactions_loaded": 0,
            "block_time_used_as_subsecond_order": False,
        },
        "production_paths_changed": 0,
        "live_trading": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc-url", default=rpc_url())
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests-per-second", type=float, default=8.0)
    parser.add_argument("--maximum-alternatives-per-group", type=int, default=8)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    write_json(args.output, report)
    print(json.dumps({"version":report["version"],**report["coverage"],"selected_only_fee_payers":len(report["recurrence"]["selected_only_fee_payers"]),"selected_only_fingerprints":len(report["recurrence"]["selected_only_fingerprints"])},indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
