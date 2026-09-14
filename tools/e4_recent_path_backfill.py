#!/usr/bin/env python3
"""Backfill event-level market paths for recent E4 Pump trades.

Read-only research utility. It uses E4 only as a benchmark and never signs or
broadcasts transactions. For each already-known E4 position it reconstructs
Pump bonding-curve TradeEvents around entry/exit and calculates curve-implied
MFE/MAE plus the managed path after E4 partial sells.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from solders.pubkey import Pubkey

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
PUMP_PROGRAM_ID = "6EF8rrecthR5DkGH5GmC2q9Jz7a2s1GqQ1kTzH5w9TQ"  # overwritten from decoder at runtime
PUMP_FEE = 0.0125
EST_SELL_TX_COST_SOL = 0.000365
RPCS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
)


def finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def normal_sol(v: Any) -> float:
    x = finite(v)
    return x / 1_000_000_000.0 if abs(x) >= 1_000_000.0 else x


def normal_tokens(v: Any) -> float:
    x = finite(v)
    return x / 1_000_000.0 if abs(x) >= 10_000_000_000.0 else x


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bonding_curve_pda(mint: str, pump_program: str) -> str:
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(pump_program),
    )
    return str(pda)


def curve_state(item: Mapping[str, Any]) -> dict[str, float] | None:
    vsol = normal_sol(item.get("virtual_sol_reserves", item.get("virtual_quote_reserves")))
    vtok = normal_tokens(item.get("virtual_token_reserves"))
    if vsol <= 0 or vtok <= 0:
        return None
    return {"vsol": vsol, "vtok": vtok}


def quote_sell(tokens: float, state: Mapping[str, float]) -> float:
    vsol = finite(state.get("vsol")); vtok = finite(state.get("vtok"))
    if tokens <= 0 or vsol <= 0 or vtok <= 0:
        return 0.0
    gross = tokens * vsol / (vtok + tokens)
    return max(0.0, gross * (1.0 - PUMP_FEE) - EST_SELL_TX_COST_SOL)


class Rpc:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.req_id = 0
        self.errors: list[str] = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    async def call(self, method: str, params: list[Any], attempts: int = 10) -> Any:
        assert self.session
        last: Exception | None = None
        for attempt in range(attempts):
            url = RPCS[(self.cursor + attempt) % len(RPCS)]
            self.req_id += 1
            try:
                async with self.session.post(url, json={"jsonrpc":"2.0","id":self.req_id,"method":method,"params":params}) as r:
                    body = await r.text()
                    if r.status == 429:
                        raise RuntimeError("429")
                    if r.status >= 400:
                        raise RuntimeError(f"HTTP {r.status}")
                    payload = json.loads(body)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    self.cursor = (RPCS.index(url) + 1) % len(RPCS)
                    return payload.get("result")
            except Exception as exc:
                last = exc
                self.errors.append(f"{method}@{url}:{type(exc).__name__}:{str(exc)[:120]}")
                await asyncio.sleep(min(2.0, 0.15 * (2 ** min(attempt, 3)) + random.random() * 0.08))
        raise RuntimeError(f"RPC {method} failed: {last}")


async def signatures_for_curve(rpc: Rpc, address: str, start: int, end: int, max_pages: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before = None
    for _ in range(max_pages):
        opts: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = await rpc.call("getSignaturesForAddress", [address, opts]) or []
        if not page:
            break
        oldest = None
        for row in page:
            bt = int(row.get("blockTime") or 0)
            if bt:
                oldest = bt if oldest is None else min(oldest, bt)
            if row.get("err") is None and bt and start <= bt <= end:
                out.append(dict(row))
        if oldest is not None and oldest < start:
            break
        if len(page) < 1000:
            break
        before = page[-1].get("signature")
        if not before:
            break
    # de-dupe and chronological order
    uniq = {str(x.get("signature")): x for x in out if x.get("signature")}
    return sorted(uniq.values(), key=lambda r: (int(r.get("slot") or 0), str(r.get("signature") or "")))


async def fetch_transactions(rpc: Rpc, rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Mapping[str, Any]]]:
    sem = asyncio.Semaphore(5)
    out: list[tuple[dict[str, Any], Mapping[str, Any]]] = []

    async def one(row: dict[str, Any]):
        async with sem:
            try:
                tx = await rpc.call("getTransaction", [row["signature"], {"encoding":"jsonParsed","commitment":"confirmed","maxSupportedTransactionVersion":0}], attempts=7)
                if isinstance(tx, Mapping):
                    out.append((row, tx))
            except Exception:
                pass
            await asyncio.sleep(0.025)

    await asyncio.gather(*(one(r) for r in rows))
    out.sort(key=lambda pair: (int(pair[0].get("slot") or 0), str(pair[0].get("signature") or "")))
    return out


def decode_trade_events(prod: Any, fetched: list[tuple[dict[str, Any], Mapping[str, Any]]], mint: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for sigrow, tx in fetched:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            continue
        logs = list(meta.get("logMessages") or [])
        if not logs:
            continue
        try:
            decoded = prod.anchor_events_from_logs(logs, prod.PUMP_PROGRAM_ID)
        except Exception:
            continue
        for idx, item in enumerate(decoded):
            if str(item.get("mint") or "") != mint:
                continue
            if str(item.get("anchor_event") or "") != "TradeEvent":
                continue
            state = curve_state(item)
            if not state:
                continue
            events.append({
                "signature": str(sigrow.get("signature") or ""),
                "block_time": int(sigrow.get("blockTime") or 0),
                "slot": int(sigrow.get("slot") or 0),
                "event_index": idx,
                "trader": str(item.get("user") or ""),
                "is_buy": bool(item.get("is_buy")),
                "sol_amount": normal_sol(item.get("sol_amount")),
                "token_amount": normal_tokens(item.get("token_amount")),
                "vsol": state["vsol"],
                "vtok": state["vtok"],
            })
    events.sort(key=lambda e: (e["slot"], e["event_index"], e["signature"]))
    return events


def summarize_position(position: Mapping[str, Any], legs: list[Mapping[str, Any]], events: list[dict[str, Any]], post_seconds: int) -> dict[str, Any]:
    mint = str(position["mint"])
    cost = finite(position["cost_sol"])
    tokens = finite(position["tokens_bought"])
    first_time = int(position["first_buy_time"])
    last_time = int(position["last_activity_time"])
    e4_sells = [dict(x) for x in legs if x.get("side") == "SELL"]
    e4_buy = next((dict(x) for x in legs if x.get("side") == "BUY"), None)
    e4_sigs = {str(x.get("signature")) for x in legs}

    remaining = tokens
    realized = 0.0
    seen_sell_sigs: set[str] = set()
    path: list[dict[str, Any]] = []
    for ev in events:
        sig = str(ev["signature"])
        # An E4 sell TradeEvent contains post-trade reserves. Apply the exact wallet leg
        # before valuing the managed book at that post-trade state.
        if sig in e4_sigs and sig not in seen_sell_sigs:
            leg = next((x for x in e4_sells if str(x.get("signature")) == sig), None)
            if leg:
                remaining = max(0.0, remaining - abs(finite(leg.get("token_delta"))))
                realized += finite(leg.get("proceeds_sol"))
                seen_sell_sigs.add(sig)
        state = {"vsol": ev["vsol"], "vtok": ev["vtok"]}
        full_quote = quote_sell(tokens, state)
        rem_quote = quote_sell(remaining, state) if remaining > 1e-9 else 0.0
        path.append({
            **ev,
            "relative_seconds": int(ev["block_time"]) - first_time,
            "is_e4_action": sig in e4_sigs,
            "remaining_token_fraction": remaining / tokens if tokens else None,
            "realized_sol_so_far": realized,
            "full_position_mark_sol": full_quote,
            "full_position_return_fraction": (full_quote / cost - 1.0) if cost else None,
            "managed_equity_sol": realized + rem_quote,
            "managed_return_fraction": ((realized + rem_quote) / cost - 1.0) if cost else None,
        })

    during = [p for p in path if first_time <= int(p["block_time"]) <= last_time]
    post = [p for p in path if last_time < int(p["block_time"]) <= last_time + post_seconds]
    before_first_sell_slot = None
    if e4_sells:
        before_first_sell_slot = min(int(x.get("slot") or 0) for x in e4_sells if int(x.get("slot") or 0) > 0) or None
    pre_initial = [p for p in during if before_first_sell_slot is None or int(p["slot"]) < before_first_sell_slot]

    def extrema(rows: list[dict[str, Any]], key: str) -> tuple[float | None, float | None]:
        vals = [finite(r.get(key), float("nan")) for r in rows]
        vals = [x for x in vals if math.isfinite(x)]
        return (max(vals) if vals else None, min(vals) if vals else None)

    mfe, mae = extrema(during, "full_position_return_fraction")
    managed_mfe, managed_mae = extrema(during, "managed_return_fraction")
    pre_mfe, pre_mae = extrema(pre_initial, "full_position_return_fraction")
    post_mfe, post_mae = extrema(post, "full_position_return_fraction")

    sell_legs = []
    for leg in e4_sells:
        sell_legs.append({
            "block_time": int(leg.get("block_time") or 0),
            "slot": int(leg.get("slot") or 0),
            "seconds_after_entry": int(leg.get("block_time") or 0) - first_time,
            "token_fraction_of_entry": abs(finite(leg.get("token_delta"))) / tokens if tokens else None,
            "proceeds_sol": finite(leg.get("proceeds_sol")),
            "signature": str(leg.get("signature") or ""),
        })

    return {
        "mint": mint,
        "realized_win": bool(position.get("win")),
        "cost_sol": cost,
        "realized_pnl_sol": finite(position.get("pnl_sol")),
        "realized_return_fraction": finite(position.get("pnl_sol")) / cost if cost else None,
        "first_buy_time": first_time,
        "last_activity_time": last_time,
        "observed_hold_seconds": last_time - first_time,
        "sell_count": int(position.get("sell_count") or 0),
        "sell_legs": sell_legs,
        "decoded_market_events": len(path),
        "e4_event_signatures_covered": sorted(e4_sigs & {str(p["signature"]) for p in path}),
        "e4_trade_signature_count": len(e4_sigs),
        "pump_event_coverage_complete": e4_sigs.issubset({str(p["signature"]) for p in path}),
        "mfe_full_position_fraction_during_e4_hold": mfe,
        "mae_full_position_fraction_during_e4_hold": mae,
        "managed_mfe_fraction_during_e4_hold": managed_mfe,
        "managed_mae_fraction_during_e4_hold": managed_mae,
        "pre_first_partial_mfe_fraction": pre_mfe,
        "pre_first_partial_mae_fraction": pre_mae,
        "post_final_exit_mfe_fraction_next_window": post_mfe,
        "post_final_exit_mae_fraction_next_window": post_mae,
        "post_window_seconds": post_seconds,
        "path": path,
    }


async def main_async(args: argparse.Namespace) -> int:
    source = json.loads(args.forensics.read_text(encoding="utf-8"))
    positions = list(((source.get("economics") or {}).get("closed") or []))
    trade_legs = list(source.get("trade_legs") or [])
    legs_by_mint: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for leg in trade_legs:
        legs_by_mint[str(leg.get("mint") or "")].append(leg)

    prod = load_module("e4_path_prod", args.production_root / "scripts" / "e4_live_market_stress.py")
    pump_program = str(prod.PUMP_PROGRAM_ID)
    results: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    async with Rpc() as rpc:
        for idx, position in enumerate(positions[: args.max_positions], 1):
            mint = str(position["mint"])
            start = int(position["first_buy_time"]) - args.pre_seconds
            end = int(position["last_activity_time"]) + args.post_seconds
            try:
                curve = bonding_curve_pda(mint, pump_program)
                sigs = await signatures_for_curve(rpc, curve, start, end, args.max_pages)
                fetched = await fetch_transactions(rpc, sigs)
                events = decode_trade_events(prod, fetched, mint)
                result = summarize_position(position, legs_by_mint[mint], events, args.post_seconds)
                result["bonding_curve"] = curve
                result["candidate_signatures"] = len(sigs)
                result["fetched_transactions"] = len(fetched)
                results.append(result)
                print(f"PATH {idx}/{min(len(positions),args.max_positions)} {mint} events={len(events)} coverage={result['pump_event_coverage_complete']}", flush=True)
            except Exception as exc:
                diagnostics.append({"mint": mint, "error": f"{type(exc).__name__}:{exc}"})
                print(f"PATH_ERROR {mint} {type(exc).__name__}:{exc}", flush=True)

        payload = {
            "version": "e4-recent-path-backfill-v1",
            "benchmark_only": True,
            "source_forensics": str(args.forensics),
            "positions_requested": min(len(positions), args.max_positions),
            "positions_reconstructed": len(results),
            "complete_pump_event_coverage": sum(bool(x.get("pump_event_coverage_complete")) for x in results),
            "positions": results,
            "diagnostics": diagnostics,
            "rpc_errors": rpc.errors[-100:],
            "mark_method": "Pump constant-product reserve mark with 1.25% protocol fee and 0.000365 SOL estimated sell tx cost; realized E4 sell proceeds remain exact wallet deltas.",
            "limitations": [
                "BlockTime is second-resolution; slot/event order is used within a second.",
                "Pump bonding-curve path ends at migration. Positions whose E4 legs occur after migration may have incomplete Pump-event coverage.",
                "MFE/MAE are event-level executable-quote approximations, not continuous-tick extrema.",
                "This does not reveal E4's private decision or exit triggers.",
            ],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    # Compact summary suitable for committing to the research branch.
    compact = {k: v for k, v in payload.items() if k != "positions"}
    compact["positions"] = []
    for p in results:
        q = {k: v for k, v in p.items() if k != "path"}
        compact["positions"].append(q)
    args.summary.write_text(json.dumps(compact, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return 0 if results else 2


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--forensics", type=Path, required=True)
    p.add_argument("--production-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--pre-seconds", type=int, default=2)
    p.add_argument("--post-seconds", type=int, default=20)
    p.add_argument("--max-pages", type=int, default=12)
    p.add_argument("--max-positions", type=int, default=13)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
