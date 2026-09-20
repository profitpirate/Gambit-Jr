"""Reconstruct the complete observable E4 wallet trade history and creator map.

The legacy 316-trade corpus is unioned by mint with the full on-chain wallet
reconstruction. Newer wallet trades are creator-enriched from Pump CREATE
events so the canonical creator library can use all verified repeat winners.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import e4_live_market_stress as stress
import v12_e4_recent_compare as recent

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

UNKNOWN = "UNKNOWN_CREATOR"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


async def all_wallet_signatures(
    rpc: stress.RpcPool,
    *,
    hard_cap: int,
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    before = None
    while True:
        config: dict[str, Any] = {"limit": 1000}
        if before:
            config["before"] = before
        batch = await rpc.call(
            "getSignaturesForAddress",
            [stress.E4_WALLET, config],
        )
        if not batch:
            break
        rows.extend(batch)
        if len(rows) >= hard_cap:
            if len(batch) == 1000:
                raise RuntimeError(
                    f"E4 wallet history reached hard cap {hard_cap}; refusing partial history"
                )
            rows = rows[:hard_cap]
            break
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return rows


def create_from_tx(tx: Mapping[str, Any], mint: str) -> dict[str, Any] | None:
    logs = list((tx.get("meta") or {}).get("logMessages") or [])
    for event in anchor_events_from_logs(logs, PUMP_PROGRAM_ID):
        if (
            str(event.get("anchor_event") or "") == "CreateEvent"
            and str(event.get("mint") or "") == mint
        ):
            return dict(event)
    return None


async def creator_for_position(
    rpc: stress.RpcPool,
    position: Mapping[str, Any],
    *,
    pages: int,
    page_size: int,
) -> dict[str, Any]:
    mint = str(position["mint"])
    entry_signature = str(position.get("entry_signature") or "")
    entry_time = int(position.get("entry_time") or 0)
    if not entry_signature:
        return {"creator": UNKNOWN, "status": "MISSING_ENTRY_SIGNATURE"}

    # The CREATE can be in the same transaction as an ultra-early observed entry.
    try:
        tx = await rpc.call(
            "getTransaction",
            [
                entry_signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
    except RuntimeError:
        tx = None
    if isinstance(tx, Mapping):
        create = create_from_tx(tx, mint)
        if create:
            return {
                "creator": str(create.get("creator") or create.get("user") or UNKNOWN),
                "status": "RESOLVED_ENTRY_TX",
                "create_signature": entry_signature,
                "create_slot": int(tx.get("slot") or 0),
            }

    before = entry_signature
    oldest_allowed = entry_time - 3_600
    for _page in range(pages):
        try:
            batch = await rpc.call(
                "getSignaturesForAddress",
                [mint, {"before": before, "limit": page_size}],
            )
        except RuntimeError:
            break
        if not batch:
            break

        semaphore = asyncio.Semaphore(8)

        async def fetch(
            row: Mapping[str, Any],
            semaphore: asyncio.Semaphore = semaphore,
        ):
            async with semaphore:
                try:
                    value = await rpc.call(
                        "getTransaction",
                        [
                            row["signature"],
                            {
                                "encoding": "jsonParsed",
                                "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    )
                    return row, value if isinstance(value, Mapping) else None
                except RuntimeError:
                    return row, None

        fetched = await asyncio.gather(*(fetch(row) for row in batch))
        for row, candidate_tx in fetched:
            if candidate_tx is None:
                continue
            create = create_from_tx(candidate_tx, mint)
            if create:
                return {
                    "creator": str(create.get("creator") or create.get("user") or UNKNOWN),
                    "status": "RESOLVED_PRIOR_TX",
                    "create_signature": str(row["signature"]),
                    "create_slot": int(candidate_tx.get("slot") or row.get("slot") or 0),
                }

        times = [int(row.get("blockTime") or 0) for row in batch if row.get("blockTime")]
        if times and min(times) < oldest_allowed:
            break
        before = str(batch[-1]["signature"])
        if len(batch) < page_size:
            break

    return {"creator": UNKNOWN, "status": "CREATE_NOT_RESOLVED"}


def legacy_records(expectancy: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = []
    for creator in expectancy.get("top_creators", []) or []:
        if not isinstance(creator, Mapping):
            continue
        creator_id = str(creator.get("creator") or UNKNOWN)
        for mint in creator.get("winner_mints", []) or []:
            records.append(
                {
                    "mint": str(mint),
                    "creator": creator_id,
                    "outcome": "WIN",
                    "pnl_sol": None,
                    "source": "LEGACY_E4_CORPUS",
                }
            )
        for mint in creator.get("loser_mints", []) or []:
            records.append(
                {
                    "mint": str(mint),
                    "creator": creator_id,
                    "outcome": "LOSS",
                    "pnl_sol": None,
                    "source": "LEGACY_E4_CORPUS",
                }
            )
    return records


def merge_records(
    legacy: list[dict[str, Any]],
    onchain: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_mint = {row["mint"]: dict(row) for row in legacy}
    for row in onchain:
        mint = row["mint"]
        if mint in by_mint:
            existing = by_mint[mint]
            if existing["creator"] == UNKNOWN and row["creator"] != UNKNOWN:
                existing["creator"] = row["creator"]
            if row.get("pnl_sol") is not None:
                existing["pnl_sol"] = row["pnl_sol"]
            existing["source"] = "LEGACY_E4_CORPUS+ONCHAIN"
            existing["entry_time"] = row.get("entry_time")
            existing["exit_time"] = row.get("exit_time")
            continue
        by_mint[mint] = dict(row)
    return sorted(
        by_mint.values(),
        key=lambda row: (
            int(row.get("entry_time") or 0),
            str(row["mint"]),
        ),
    )


def merge_position_ledgers(
    primary: list[dict[str, Any]],
    recent_report: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Recover recent positions independently verified by the 48h ledger.

    The full-history RPC pass can lose transactions to public-RPC throttling.
    The recent ledger is produced by a separate bounded reconstruction. We only
    add mints absent from the primary scan, preserving primary rows otherwise.
    """
    by_mint = {str(row["mint"]): dict(row) for row in primary}
    recent_rows = (
        list(recent_report.get("e4_positions") or [])
        if isinstance(recent_report, Mapping)
        else []
    )
    added = 0
    skipped_without_signatures = 0
    for raw in recent_rows:
        if not isinstance(raw, Mapping) or not raw.get("mint"):
            continue
        mint = str(raw["mint"])
        if mint in by_mint:
            continue
        row = dict(raw)
        if not row.get("entry_signature"):
            skipped_without_signatures += 1
            continue
        by_mint[mint] = row
        added += 1
    merged = sorted(
        by_mint.values(),
        key=lambda row: (int(row.get("entry_time") or 0), str(row.get("mint") or "")),
    )
    return merged, {
        "primary_positions": len(primary),
        "recent_verified_positions": len(recent_rows),
        "recent_positions_added": added,
        "recent_positions_missing_signatures": skipped_without_signatures,
        "positions_after_recovery": len(merged),
    }


def creator_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row.get("creator") or UNKNOWN)].append(row)
    output = []
    for creator, rows in grouped.items():
        wins = sum(row["outcome"] == "WIN" for row in rows)
        losses = sum(row["outcome"] == "LOSS" for row in rows)
        pnls = [
            float(row["pnl_sol"])
            for row in rows
            if row.get("pnl_sol") is not None
        ]
        output.append(
            {
                "creator": creator,
                "wins": wins,
                "losses": losses,
                "trades": wins + losses,
                "win_rate": wins / max(wins + losses, 1),
                "net_pnl_sol_observed": sum(pnls) if pnls else None,
                "pnl_observed_trades": len(pnls),
                "winner_mints": [
                    row["mint"] for row in rows if row["outcome"] == "WIN"
                ],
                "loser_mints": [
                    row["mint"] for row in rows if row["outcome"] == "LOSS"
                ],
            }
        )
    output.sort(
        key=lambda row: (
            row["creator"] == UNKNOWN,
            -row["wins"],
            row["losses"],
            row["creator"],
        )
    )
    return output


async def run(args: argparse.Namespace) -> dict[str, Any]:
    expectancy = load(args.legacy_expectancy)
    async with stress.RpcPool(stress.DEFAULT_HTTP_RPCS, timeout=args.rpc_timeout) as rpc:
        signatures = await all_wallet_signatures(rpc, hard_cap=args.signature_hard_cap)
        fetched = await recent.fetch_transactions(rpc, signatures)
        events, event_diagnostics = recent.wallet_events(fetched)
        positions, reconstruction = recent.positions_from_events(events, 0)
        positions.sort(key=lambda row: (int(row["entry_time"]), str(row["mint"])))
        recent_report = (
            load(args.recent_comparison)
            if args.recent_comparison and args.recent_comparison.exists()
            else None
        )
        positions, recovery = merge_position_ledgers(positions, recent_report)

        semaphore = asyncio.Semaphore(args.creator_concurrency)

        async def enrich(position: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                resolved = await creator_for_position(
                    rpc,
                    position,
                    pages=args.creator_pages,
                    page_size=args.creator_page_size,
                )
                pnl = float(position.get("pnl_sol") or 0.0)
                return {
                    "mint": str(position["mint"]),
                    "creator": str(resolved["creator"]),
                    "creator_resolution": str(resolved["status"]),
                    "create_signature": resolved.get("create_signature"),
                    "create_slot": resolved.get("create_slot"),
                    "outcome": "WIN" if pnl > 0 else "LOSS",
                    "pnl_sol": pnl,
                    "entry_time": int(position.get("entry_time") or 0),
                    "exit_time": int(position.get("exit_time") or 0),
                    "entry_signature": str(position.get("entry_signature") or ""),
                    "exit_signature": str(position.get("exit_signature") or ""),
                    "entry_sol": float(position.get("cost_sol") or 0.0),
                    "source": "ONCHAIN_E4_WALLET",
                }

        onchain = []
        for index in range(0, len(positions), args.creator_batch):
            batch = positions[index : index + args.creator_batch]
            onchain.extend(await asyncio.gather(*(enrich(dict(row)) for row in batch)))
            print(
                json.dumps(
                    {
                        "creator_progress": len(onchain),
                        "closed_positions": len(positions),
                        "rpc_errors": len(rpc.errors),
                    }
                ),
                flush=True,
            )
        rpc_errors = rpc.errors[-200:]

    legacy = legacy_records(expectancy)
    merged = merge_records(legacy, onchain)
    creators = creator_summary(merged)
    known = [row for row in creators if row["creator"] != UNKNOWN]
    repeat = [row for row in known if row["wins"] >= 2]
    pure_repeat = [row for row in repeat if row["losses"] == 0]
    return {
        "schema_version": "e4-complete-creator-history-v1",
        "generated_epoch": int(time.time()),
        "wallet": stress.E4_WALLET,
        "completeness": {
            "wallet_signature_scan_exhausted": len(signatures) < args.signature_hard_cap,
            "signatures_scanned": len(signatures),
            "onchain_closed_positions": len(positions),
            "onchain_closed_positions_raw": recovery["primary_positions"],
            "recent_verified_positions": recovery["recent_verified_positions"],
            "recent_positions_added": recovery["recent_positions_added"],
            "positions_after_recovery": recovery["positions_after_recovery"],
            "legacy_trades": len(legacy),
            "union_trades": len(merged),
            "onchain_creator_resolved": sum(
                row["creator"] != UNKNOWN for row in onchain
            ),
            "onchain_creator_unresolved": sum(
                row["creator"] == UNKNOWN for row in onchain
            ),
        },
        "counts": {
            "creators_known": len(known),
            "repeat_winner_creators": len(repeat),
            "pure_repeat_winner_creators": len(pure_repeat),
            "union_wins": sum(row["outcome"] == "WIN" for row in merged),
            "union_losses": sum(row["outcome"] == "LOSS" for row in merged),
        },
        "creators": creators,
        "trades": merged,
        "onchain_diagnostics": {
            "event": event_diagnostics,
            "reconstruction": reconstruction,
            "recent_recovery": recovery,
            "rpc_errors": rpc_errors,
        },
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--legacy-expectancy",
        type=Path,
        default=Path("models/e4/e4-creator-expectancy.json"),
    )
    value.add_argument(
        "--output",
        type=Path,
        default=Path("models/e4/e4-complete-creator-history.json"),
    )
    value.add_argument(
        "--recent-comparison",
        type=Path,
        default=Path("research/v12-e4-48h-comparison.json"),
    )
    value.add_argument("--signature-hard-cap", type=int, default=50_000)
    value.add_argument("--creator-pages", type=int, default=4)
    value.add_argument("--creator-page-size", type=int, default=50)
    value.add_argument("--creator-concurrency", type=int, default=6)
    value.add_argument("--creator-batch", type=int, default=25)
    value.add_argument("--rpc-timeout", type=float, default=12.0)
    return value


def main() -> int:
    args = parser().parse_args()
    result = asyncio.run(run(args))
    write(args.output, result)
    print(json.dumps(result["completeness"], sort_keys=True))
    print(json.dumps(result["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
