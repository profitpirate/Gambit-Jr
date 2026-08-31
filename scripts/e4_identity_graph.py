#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from e4_creator_forensics import (
    DEFAULT_RPCS,
    E4_WALLET,
    RpcPool,
    entry_context,
    transaction,
    wallet_buy,
)
from e4_build_winner_creator_registry import bonding_curve_for
from memecoin_bot.realtime.pumpfun import decode_account_data, decode_bonding_curve_account


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_targets(winner_registry: Path, losers_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    winners = json.loads(winner_registry.read_text(encoding="utf-8"))
    labels: dict[str, str] = {}
    for creator in (winners.get("creators") or {}).values():
        for mint in creator.get("winner_mints") or []:
            labels[str(mint)] = "WIN"
    losers = json.loads(losers_path.read_text(encoding="utf-8"))
    for mint in losers:
        labels[str(mint)] = "LOSS"
    return labels, winners


async def resolve_creator_from_curve(rpc: RpcPool, mint: str) -> str | None:
    try:
        curve = bonding_curve_for(mint)
        result = await rpc.call(
            "getAccountInfo",
            [curve, {"encoding": "base64", "commitment": "confirmed"}],
            retries=3,
        )
        value = (result or {}).get("value") if isinstance(result, Mapping) else None
        if not value:
            return None
        decoded = decode_bonding_curve_account(decode_account_data(value.get("data")))
        creator = str(decoded.get("creator") or "")
        return creator or None
    except Exception:
        return None


async def find_e4_entries(
    rpc: RpcPool,
    targets: set[str],
    *,
    page_size: int,
    max_signatures: int,
    tx_concurrency: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    found: dict[str, dict[str, Any]] = {}
    before: str | None = None
    examined = 0
    while examined < max_signatures and len(found) < len(targets):
        params: dict[str, Any] = {"limit": min(1000, page_size)}
        if before:
            params["before"] = before
        rows = list(await rpc.call("getSignaturesForAddress", [E4_WALLET, params]) or [])
        if not rows:
            break
        examined += len(rows)
        for start in range(0, len(rows), tx_concurrency):
            batch = rows[start : start + tx_concurrency]
            txs = await asyncio.gather(
                *(transaction(rpc, str(row["signature"])) for row in batch)
            )
            for row, tx in zip(batch, txs):
                if not tx:
                    continue
                buy = wallet_buy(row, tx)
                if buy and buy["mint"] in targets and buy["mint"] not in found:
                    found[buy["mint"]] = buy
            if len(found) >= len(targets):
                break
        before = str(rows[-1]["signature"])
        print(
            json.dumps(
                {
                    "stage": "e4_history",
                    "signatures_examined": examined,
                    "entries_found": len(found),
                    "target": len(targets),
                }
            ),
            flush=True,
        )
        if len(rows) < min(1000, page_size):
            break
    return found, examined


def transfer_candidates(tx: Mapping[str, Any], creator: str) -> list[tuple[str, float]]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    meta = tx.get("meta") or {}
    instructions = list(message.get("instructions") or [])
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    output: list[tuple[str, float]] = []
    for ix in instructions:
        parsed = ix.get("parsed") if isinstance(ix, Mapping) else None
        info = parsed.get("info") if isinstance(parsed, Mapping) else None
        if not isinstance(info, Mapping):
            continue
        if str(info.get("destination") or "") != creator:
            continue
        source = str(info.get("source") or "")
        lamports = finite(info.get("lamports"))
        if source and source != creator and lamports and lamports > 0:
            output.append((source, lamports / 1_000_000_000))
    return output


async def infer_funder(
    rpc: RpcPool,
    creator: str,
    before_block_time: int | None,
    *,
    signature_limit: int,
) -> dict[str, Any]:
    try:
        rows = list(
            await rpc.call(
                "getSignaturesForAddress",
                [creator, {"limit": min(1000, signature_limit)}],
            )
            or []
        )
    except Exception as exc:
        return {"funder": None, "error": f"signatures:{type(exc).__name__}"}
    if before_block_time:
        rows = [row for row in rows if int(row.get("blockTime") or 0) <= before_block_time]
    rows = rows[-min(len(rows), 30) :]
    txs = await asyncio.gather(*(transaction(rpc, str(row["signature"])) for row in rows))
    candidates: defaultdict[str, float] = defaultdict(float)
    for tx in txs:
        if not tx:
            continue
        for source, sol in transfer_candidates(tx, creator):
            candidates[source] += sol
    if not candidates:
        return {"funder": None, "funder_sol_observed": 0.0}
    funder, amount = max(candidates.items(), key=lambda pair: pair[1])
    return {"funder": funder, "funder_sol_observed": amount}


def normalize_metadata_url(uri: str | None) -> str | None:
    if not uri:
        return None
    uri = uri.strip()
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri.removeprefix("ipfs://").lstrip("/")
    return uri if uri.startswith(("http://", "https://")) else None


def social_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    stack: list[Mapping[str, Any]] = [payload]
    while stack:
        current = stack.pop()
        for key, value in current.items():
            lower = str(key).lower()
            if isinstance(value, Mapping):
                stack.append(value)
            elif lower in {
                "twitter", "x", "x_url", "twitter_url", "telegram", "website",
                "discord", "community", "social", "socials"
            }:
                flattened[lower] = value
    return flattened


async def fetch_metadata(
    session: aiohttp.ClientSession,
    uri: str | None,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    url = normalize_metadata_url(uri)
    if not url:
        return {"metadata_fetched": False, "socials": {}}
    async with semaphore:
        try:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    return {"metadata_fetched": False, "metadata_http": response.status, "socials": {}}
                payload = await response.json(content_type=None)
                if not isinstance(payload, Mapping):
                    return {"metadata_fetched": False, "socials": {}}
                return {
                    "metadata_fetched": True,
                    "metadata_http": response.status,
                    "metadata_final_host": (urlparse(str(response.url)).netloc or "unknown").lower(),
                    "socials": social_fields(payload),
                }
        except Exception as exc:
            return {"metadata_fetched": False, "metadata_error": type(exc).__name__, "socials": {}}


def repeated_buyers(rows: list[dict[str, Any]], outcome: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        if row.get("outcome") != outcome:
            continue
        for wallet in set(row.get("early_noncreator_wallets") or []):
            if wallet and wallet != E4_WALLET:
                counter[wallet] += 1
    return counter


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    winners = [row for row in rows if row["outcome"] == "WIN"]
    losers = [row for row in rows if row["outcome"] == "LOSS"]

    creators: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "mints": [], "gross_pnl_sol": 0.0}
    )
    funders: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "creators": set(), "mints": []}
    )
    hosts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    social_domains: defaultdict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        outcome = row["outcome"]
        creator = row.get("creator")
        if creator:
            record = creators[creator]
            record["wins" if outcome == "WIN" else "losses"] += 1
            record["mints"].append(row["mint"])
            record["gross_pnl_sol"] += float(row.get("gross_pnl_sol") or 0.0)
        funder = row.get("funder")
        if funder:
            record = funders[funder]
            record["wins" if outcome == "WIN" else "losses"] += 1
            if creator:
                record["creators"].add(creator)
            record["mints"].append(row["mint"])
        hosts[outcome][str(row.get("metadata_host") or "unknown")] += 1
        for value in (row.get("socials") or {}).values():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                social_domains[outcome][(urlparse(value).netloc or "unknown").lower()] += 1

    creator_records = []
    for creator, record in creators.items():
        total = record["wins"] + record["losses"]
        creator_records.append(
            {
                "creator": creator,
                **record,
                "trades": total,
                "gross_win_rate": record["wins"] / total if total else 0.0,
            }
        )
    creator_records.sort(key=lambda row: (-row["trades"], -row["gross_win_rate"], -row["gross_pnl_sol"]))

    funder_records = []
    for funder, record in funders.items():
        total = record["wins"] + record["losses"]
        funder_records.append(
            {
                "funder": funder,
                "wins": record["wins"],
                "losses": record["losses"],
                "trades": total,
                "gross_win_rate": record["wins"] / total if total else 0.0,
                "unique_creators": len(record["creators"]),
                "creators": sorted(record["creators"]),
                "mints": record["mints"],
            }
        )
    funder_records.sort(key=lambda row: (-row["trades"], -row["unique_creators"], -row["gross_win_rate"]))

    win_buyers = repeated_buyers(rows, "WIN")
    loss_buyers = repeated_buyers(rows, "LOSS")
    buyer_edge = []
    for wallet in set(win_buyers) | set(loss_buyers):
        w, l = win_buyers[wallet], loss_buyers[wallet]
        if w + l < 2:
            continue
        buyer_edge.append({"wallet": wallet, "winner_entries": w, "loser_entries": l, "edge": w - l})
    buyer_edge.sort(key=lambda row: (-row["edge"], -row["winner_entries"], row["loser_entries"]))

    def group_stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(group),
            "j7_count": sum(row.get("metadata_host") == "metadata.j7tracker.io" for row in group),
            "j7_rate": sum(row.get("metadata_host") == "metadata.j7tracker.io" for row in group) / len(group) if group else 0.0,
            "social_metadata_count": sum(bool(row.get("socials")) for row in group),
            "social_metadata_rate": sum(bool(row.get("socials")) for row in group) / len(group) if group else 0.0,
            "creator_only_or_one_buyer": sum((row.get("noncreator_buyers_before_entry") or 0) <= 1 for row in group),
            "median_noncreator_buyers": statistics.median([row.get("noncreator_buyers_before_entry") or 0 for row in group]) if group else None,
            "median_noncreator_sol": statistics.median([row.get("noncreator_sol_before_entry") or 0.0 for row in group]) if group else None,
            "median_creator_seed_sol": statistics.median([row.get("creator_buy_sol_before_entry") or 0.0 for row in group]) if group else None,
            "median_slot_delay": statistics.median([row.get("slot_delay") or 0 for row in group]) if group else None,
            "pre_entry_sell_count": sum((row.get("sells_before_entry") or 0) > 0 for row in group),
        }

    return {
        "winner_stats": group_stats(winners),
        "loser_stats": group_stats(losers),
        "metadata_hosts": {key: dict(value) for key, value in hosts.items()},
        "social_domains": {key: dict(value) for key, value in social_domains.items()},
        "creator_expectancy": creator_records,
        "funder_expectancy": funder_records,
        "repeat_early_buyer_edge": buyer_edge,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    labels, winner_registry = load_targets(args.winner_registry, args.losers)
    targets = set(labels)
    rpc_urls = [part.strip() for part in args.rpc_urls.split(",") if part.strip()]
    async with RpcPool(rpc_urls, timeout=args.rpc_timeout, concurrency=args.rpc_concurrency) as rpc:
        entries, signatures_examined = await find_e4_entries(
            rpc,
            targets,
            page_size=args.page_size,
            max_signatures=args.max_signatures,
            tx_concurrency=args.tx_concurrency,
        )

        # Resolve context from the exact E4 entry signature. This reconstructs
        # creator seed, public buyers, sells, URI/source and execution tip state.
        rows: list[dict[str, Any]] = []
        found_items = list(entries.items())
        for start in range(0, len(found_items), args.context_concurrency):
            batch = found_items[start : start + args.context_concurrency]
            contexts = await asyncio.gather(
                *(entry_context(rpc, entry, args.before_limit) for _mint, entry in batch)
            )
            for (mint, entry), context in zip(batch, contexts):
                # Preserve early wallets separately for recurrence analysis.
                rows.append(
                    {
                        "mint": mint,
                        "outcome": labels[mint],
                        "gross_pnl_sol": 0.0,
                        **{key: value for key, value in entry.items() if key != "tx"},
                        **context,
                    }
                )
            print(json.dumps({"stage": "entry_context", "processed": len(rows), "target": len(entries)}), flush=True)

        # Curve resolution is a fallback for any context whose create event was
        # not recovered from transaction history.
        missing_creator = [row for row in rows if not row.get("creator")]
        creators = await asyncio.gather(*(resolve_creator_from_curve(rpc, row["mint"]) for row in missing_creator))
        for row, creator in zip(missing_creator, creators):
            row["creator"] = creator

        # Infer one pre-launch funding source per creator and cache it across all
        # positions from that creator.
        by_creator: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row.get("creator"):
                by_creator[str(row["creator"])].append(row)
        funder_cache: dict[str, dict[str, Any]] = {}
        creator_items = list(by_creator.items())
        for start in range(0, len(creator_items), args.funder_concurrency):
            batch = creator_items[start : start + args.funder_concurrency]
            inferred = await asyncio.gather(
                *(
                    infer_funder(
                        rpc,
                        creator,
                        min((int(row.get("block_time") or 0) for row in items), default=None),
                        signature_limit=args.creator_signature_limit,
                    )
                    for creator, items in batch
                )
            )
            for (creator, _items), value in zip(batch, inferred):
                funder_cache[creator] = value
            print(json.dumps({"stage": "funders", "processed": min(start + len(batch), len(creator_items)), "target": len(creator_items)}), flush=True)
        for row in rows:
            row.update(funder_cache.get(str(row.get("creator") or ""), {}))
        rpc_errors = rpc.errors[-250:]

    # Fetch token metadata/social links independently from RPC limits.
    timeout = aiohttp.ClientTimeout(total=args.metadata_timeout)
    metadata_sem = asyncio.Semaphore(args.metadata_concurrency)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Gambit-E4-Identity/1.0"}) as session:
        metadata = await asyncio.gather(*(fetch_metadata(session, row.get("uri"), metadata_sem) for row in rows))
    for row, value in zip(rows, metadata):
        row.update(value)

    # Attach exact winner P&L known from the existing registry where available.
    pnl_by_mint: dict[str, float] = {}
    for creator in (winner_registry.get("creators") or {}).values():
        total_pnl = float(creator.get("e4_gross_pnl_sol") or 0.0)
        mints = list(creator.get("winner_mints") or [])
        if len(mints) == 1:
            pnl_by_mint[mints[0]] = total_pnl
    for row in rows:
        if row["mint"] in pnl_by_mint:
            row["gross_pnl_sol"] = pnl_by_mint[row["mint"]]

    summary = summarize(rows)
    unresolved = sorted(targets - set(entries))
    return {
        "version": "e4-identity-graph-v1",
        "target_positions": len(targets),
        "winner_targets": sum(value == "WIN" for value in labels.values()),
        "loser_targets": sum(value == "LOSS" for value in labels.values()),
        "entries_recovered": len(entries),
        "unresolved_entries": unresolved,
        "signatures_examined": signatures_examined,
        "rpc_errors": rpc_errors,
        "summary": summary,
        "positions": rows,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build E4 creator/funder/source/social identity graph")
    p.add_argument("--winner-registry", type=Path, default=Path("models/e4/e4-winning-creators.json"))
    p.add_argument("--losers", type=Path, default=Path("models/e4/e4-losing-mints.json"))
    p.add_argument("--output", type=Path, default=Path("artifacts/e4-identity-graph.json"))
    p.add_argument("--rpc-urls", default=",".join(DEFAULT_RPCS))
    p.add_argument("--rpc-timeout", type=float, default=6.0)
    p.add_argument("--rpc-concurrency", type=int, default=16)
    p.add_argument("--tx-concurrency", type=int, default=24)
    p.add_argument("--context-concurrency", type=int, default=12)
    p.add_argument("--funder-concurrency", type=int, default=10)
    p.add_argument("--page-size", type=int, default=1000)
    p.add_argument("--max-signatures", type=int, default=12000)
    p.add_argument("--before-limit", type=int, default=30)
    p.add_argument("--creator-signature-limit", type=int, default=100)
    p.add_argument("--metadata-timeout", type=float, default=6.0)
    p.add_argument("--metadata-concurrency", type=int, default=24)
    return p


def main() -> int:
    args = parser().parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    summary_path = args.output.with_suffix(".md")
    s = report["summary"]
    summary_path.write_text(
        "# E4 identity graph\n\n"
        f"Recovered **{report['entries_recovered']}/{report['target_positions']}** target E4 entries.\n\n"
        f"- Winners: {json.dumps(s['winner_stats'], indent=2)}\n"
        f"- Losers: {json.dumps(s['loser_stats'], indent=2)}\n\n"
        "## Top creator expectancy\n\n"
        + "\n".join(
            f"- `{row['creator']}` — {row['wins']}W/{row['losses']}L ({row['gross_win_rate']:.1%}), {row['trades']} E4 trades"
            for row in s["creator_expectancy"][:40]
        )
        + "\n\n## Top funder/operator clusters\n\n"
        + "\n".join(
            f"- `{row['funder']}` — {row['wins']}W/{row['losses']}L ({row['gross_win_rate']:.1%}), {row['unique_creators']} creators"
            for row in s["funder_expectancy"][:40]
        )
        + "\n\n## Repeat early-buyer edge\n\n"
        + "\n".join(
            f"- `{row['wallet']}` — winner entries {row['winner_entries']}, loser entries {row['loser_entries']}"
            for row in s["repeat_early_buyer_edge"][:40]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "entries": report["entries_recovered"], "target": report["target_positions"]}), flush=True)
    return 0 if report["entries_recovered"] >= int(report["target_positions"] * 0.90) else 2


if __name__ == "__main__":
    raise SystemExit(main())
