#!/usr/bin/env python3
"""Backfill E4 wallet activity for the current Golden Thesis live window.

Read-only research utility. It never signs or broadcasts transactions. E4 is a
benchmark only and has no influence on Golden Thesis entry decisions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import aiohttp

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD7kGoPYz9y7n9Q7C9v7f9"
IGNORE_MINTS = {WSOL, USDC, USDT}
RPCS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)


def atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def token_amount(row: Mapping[str, Any]) -> float:
    ui = row.get("uiTokenAmount") or {}
    raw = ui.get("uiAmountString")
    if raw is not None:
        return finite(raw)
    decimals = int(ui.get("decimals") or 0)
    return finite(ui.get("amount")) / (10 ** decimals)


class Rpc:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.request_id = 0
        self.cursor = 0
        self.errors: list[str] = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *_):
        assert self.session
        await self.session.close()

    async def call(self, method: str, params: list[Any], attempts: int = 8) -> Any:
        assert self.session
        last = None
        for attempt in range(attempts):
            url = RPCS[(self.cursor + attempt) % len(RPCS)]
            self.request_id += 1
            try:
                async with self.session.post(url, json={"jsonrpc":"2.0","id":self.request_id,"method":method,"params":params}) as r:
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
                await asyncio.sleep(min(2.0, 0.2 * (2 ** min(attempt, 3))))
        raise RuntimeError(f"RPC {method} failed: {last}")


async def signatures(rpc: Rpc, start_epoch: int, end_epoch: int, max_signatures: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before = None
    while len(out) < max_signatures:
        opts: dict[str, Any] = {"limit": min(1000, max_signatures - len(out)), "commitment":"confirmed"}
        if before:
            opts["before"] = before
        page = await rpc.call("getSignaturesForAddress", [E4_WALLET, opts]) or []
        if not page:
            break
        stop = False
        for row in page:
            bt = int(row.get("blockTime") or 0)
            if bt and bt < start_epoch:
                stop = True
                break
            if row.get("err") is None and bt and bt <= end_epoch:
                out.append(dict(row))
        if stop or len(page) < opts["limit"]:
            break
        before = page[-1].get("signature")
        if not before:
            break
    out.sort(key=lambda r: (int(r.get("blockTime") or 0), int(r.get("slot") or 0)))
    return out


def account_key_strings(tx: Mapping[str, Any]) -> list[str]:
    keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    out = []
    for key in keys:
        out.append(str(key.get("pubkey")) if isinstance(key, Mapping) else str(key))
    return out


def owner_token_map(rows: list[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for row in rows:
        if str(row.get("owner") or "") != E4_WALLET:
            continue
        mint = str(row.get("mint") or "")
        if mint and mint not in IGNORE_MINTS:
            out[mint] += token_amount(row)
    return dict(out)


def parse_tx(sigrow: Mapping[str, Any], tx: Mapping[str, Any]) -> list[dict[str, Any]]:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = account_key_strings(tx)
    if E4_WALLET not in keys:
        return []
    wi = keys.index(E4_WALLET)
    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    if wi >= len(pre_bal) or wi >= len(post_bal):
        return []
    sol_delta = (float(post_bal[wi]) - float(pre_bal[wi])) / 1_000_000_000.0
    pre = owner_token_map(meta.get("preTokenBalances") or [])
    post = owner_token_map(meta.get("postTokenBalances") or [])
    mints = set(pre) | set(post)
    deltas = {m: post.get(m,0.0) - pre.get(m,0.0) for m in mints}
    meaningful = [(m,d) for m,d in deltas.items() if abs(d) > 1e-9]
    # A swap/trade should have exactly one non-SOL trade token changing in E4's wallet.
    if len(meaningful) != 1:
        return []
    mint, delta = meaningful[0]
    side = "BUY" if delta > 0 else "SELL"
    return [{
        "signature": str(sigrow.get("signature") or ""),
        "block_time": int(sigrow.get("blockTime") or 0),
        "slot": int(sigrow.get("slot") or 0),
        "mint": mint,
        "side": side,
        "token_delta": delta,
        "wallet_sol_delta": sol_delta,
        "cost_sol": -sol_delta if side == "BUY" else 0.0,
        "proceeds_sol": sol_delta if side == "SELL" else 0.0,
        "fee_sol": finite(meta.get("fee")) / 1_000_000_000.0,
    }]


async def fetch_transactions(rpc: Rpc, sigs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    sem = asyncio.Semaphore(8)
    trades: list[dict[str, Any]] = []
    skipped = 0

    async def one(row: dict[str, Any]):
        nonlocal skipped
        async with sem:
            try:
                tx = await rpc.call("getTransaction", [row["signature"], {"encoding":"jsonParsed","commitment":"confirmed","maxSupportedTransactionVersion":0}], attempts=6)
                if isinstance(tx, Mapping):
                    parsed = parse_tx(row, tx)
                    if parsed:
                        trades.extend(parsed)
                    else:
                        skipped += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
            await asyncio.sleep(0.03)

    await asyncio.gather(*(one(row) for row in sigs))
    trades.sort(key=lambda r: (r["block_time"], r["slot"], r["signature"]))
    return trades, skipped


def reconstruct(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        by_mint[row["mint"]].append(row)
    closed = []
    open_positions = []
    for mint, rows in by_mint.items():
        buys = [r for r in rows if r["side"] == "BUY" and r["cost_sol"] > 0]
        sells = [r for r in rows if r["side"] == "SELL" and r["proceeds_sol"] > 0]
        if not buys:
            # likely a position opened before the requested window; exclude from same-window economics
            continue
        bought = sum(max(0.0, r["token_delta"]) for r in buys)
        sold = sum(max(0.0, -r["token_delta"]) for r in sells)
        cost = sum(r["cost_sol"] for r in buys)
        proceeds = sum(r["proceeds_sol"] for r in sells)
        ratio = sold / bought if bought > 0 else 0.0
        record = {
            "mint": mint,
            "first_buy_time": buys[0]["block_time"],
            "last_activity_time": rows[-1]["block_time"],
            "buy_count": len(buys),
            "sell_count": len(sells),
            "cost_sol": cost,
            "proceeds_sol": proceeds,
            "tokens_bought": bought,
            "tokens_sold": sold,
            "sold_fraction": ratio,
            "pnl_sol": proceeds - cost,
            "signatures": [r["signature"] for r in rows],
        }
        # tolerate token rounding / tiny dust left behind.
        if ratio >= 0.98:
            record["win"] = record["pnl_sol"] > 0
            closed.append(record)
        else:
            open_positions.append(record)
    pnls = [r["pnl_sol"] for r in closed]
    wins = sum(x > 0 for x in pnls)
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    return {
        "same_window_positions_opened": len(closed) + len(open_positions),
        "closed_positions": len(closed),
        "open_positions": len(open_positions),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate": wins / len(closed) if closed else None,
        "net_realized_pnl_sol": sum(pnls),
        "profit_factor": gains / losses if losses > 0 else (999.0 if gains > 0 else None),
        "closed": closed,
        "open": open_positions,
    }


async def main_async(args: argparse.Namespace) -> int:
    start = int(args.start_epoch)
    end = int(args.end_epoch or time.time())
    async with Rpc() as rpc:
        sigs = await signatures(rpc, start, end, args.max_signatures)
        trades, skipped = await fetch_transactions(rpc, sigs)
        econ = reconstruct(trades)
        payload = {
            "version":"e4-same-window-benchmark-v1",
            "paper_strategy_unchanged": True,
            "benchmark_only": True,
            "wallet": E4_WALLET,
            "start_epoch": start,
            "end_epoch": end,
            "wallet_signatures_in_window": len(sigs),
            "parsed_trade_transactions": len(trades),
            "unparsed_or_nontrade_transactions": skipped,
            "rpc_recent_errors": rpc.errors[-20:],
            "economics": econ,
        }
    atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start-epoch", type=int, required=True)
    p.add_argument("--end-epoch", type=int)
    p.add_argument("--max-signatures", type=int, default=5000)
    p.add_argument("--output", type=Path, required=True)
    return asyncio.run(main_async(p.parse_args()))

if __name__ == "__main__":
    raise SystemExit(main())
