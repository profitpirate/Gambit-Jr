#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from memecoin_bot import e4_preconfirm_v12 as preconfirm


def source_rows(evidence: Mapping[str, Any], source_runs: set[str] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run_id, audit in (evidence.get("copy_audits") or {}).items():
        if source_runs and str(run_id) not in source_runs:
            continue
        for trade in (audit or {}).get("direct_copy_trades") or []:
            signature = str(trade.get("source_signature") or "")
            if not signature or signature in seen:
                continue
            seen.add(signature)
            rows.append(
                {
                    "source_run": str(run_id),
                    "mint": str(trade.get("mint") or ""),
                    "source_signature": signature,
                    "observed_entry_sol": float(trade.get("source_entry_sol") or 0.0),
                    "e4_won": bool(trade.get("e4_won")),
                    "e4_pnl_sol": float(trade.get("e4_pnl_sol") or 0.0),
                }
            )
    return rows


async def rpc_call(session: aiohttp.ClientSession, url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            async with session.post(url, json=payload) as response:
                text = await response.text()
                if response.status == 429 or response.status >= 500:
                    raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
                body = json.loads(text)
                if body.get("error"):
                    raise RuntimeError(str(body["error"]))
                return body.get("result")
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"RPC {method} exhausted retries: {last_error}")


async def classify_one(
    semaphore: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    rpc_url: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        result = dict(row)
        try:
            tx = await rpc_call(
                session,
                rpc_url,
                "getTransaction",
                [
                    row["source_signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if not isinstance(tx, Mapping):
                result.update({"fetched": False, "error": "transaction unavailable"})
                return result
            intents = preconfirm.decode_e4_buy_intents(
                str(row["source_signature"]),
                tx,
                received_ns=0,
            )
            matching = [intent for intent in intents if not row.get("mint") or intent.mint == row.get("mint")]
            if not matching:
                result.update(
                    {
                        "fetched": True,
                        "decoded": False,
                        "instruction_family": "unknown_or_inner_only",
                        "exact_spend": False,
                    }
                )
                return result
            intent = matching[0]
            result.update(
                {
                    "fetched": True,
                    "decoded": True,
                    "instruction_family": intent.instruction_family,
                    "exact_spend": intent.exact_spend,
                    "instruction_spend_sol": intent.spend_sol,
                    "instruction_spend_ceiling_sol": intent.spend_ceiling_sol,
                    "instruction_token_target": intent.token_target,
                    "observed_minus_instruction_sol": (
                        float(row.get("observed_entry_sol") or 0.0) - float(intent.spend_sol or 0.0)
                        if intent.exact_spend
                        else None
                    ),
                }
            )
            return result
        except Exception as exc:
            result.update({"fetched": False, "error": str(exc)})
            return result


async def main_async(args: argparse.Namespace) -> int:
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    selected = set(args.source_run or []) or None
    rows = source_rows(evidence, selected)
    if args.limit > 0:
        rows = rows[-args.limit :]

    timeout = aiohttp.ClientTimeout(total=12, sock_connect=4, sock_read=10)
    connector = aiohttp.TCPConnector(limit=max(2, args.concurrency))
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        semaphore = asyncio.Semaphore(max(1, args.concurrency))
        classified = await asyncio.gather(
            *(classify_one(semaphore, session, args.rpc_url, row) for row in rows)
        )

    families = Counter(str(row.get("instruction_family") or "unfetched") for row in classified)
    fetched = [row for row in classified if row.get("fetched")]
    decoded = [row for row in classified if row.get("decoded")]
    exact = [row for row in decoded if row.get("exact_spend")]
    winners = [row for row in classified if row.get("e4_won")]
    exact_winners = [row for row in exact if row.get("e4_won")]
    summary = {
        "requested": len(rows),
        "fetched": len(fetched),
        "decoded": len(decoded),
        "exact_spend": len(exact),
        "exact_spend_fraction_of_decoded": len(exact) / len(decoded) if decoded else None,
        "e4_winners": len(winners),
        "exact_spend_e4_winners": len(exact_winners),
        "exact_spend_winner_fraction": len(exact_winners) / len(winners) if winners else None,
        "families": dict(sorted(families.items())),
    }
    output = {"summary": summary, "rows": classified}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Audit observed E4 Pump buy instruction families")
    result.add_argument("--evidence", default="models/e4/e4-v12-forward-evidence.json")
    result.add_argument("--output", default="artifacts/e4-v12-instruction-family-audit.json")
    result.add_argument("--rpc-url", default=os.getenv("E4_PRIMARY_RPC_URL") or "https://api.mainnet-beta.solana.com")
    result.add_argument("--source-run", action="append", default=[])
    result.add_argument("--limit", type=int, default=30)
    result.add_argument("--concurrency", type=int, default=2)
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parser().parse_args())))
