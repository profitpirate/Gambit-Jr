#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_TOKEN_MINT = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_SCALE = 1_000_000
DEFAULT_SUPPLY_RAW = 1_000_000_000_000_000
RPC_URLS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana-mainnet.api.onfinality.io/public",
)
PUMP_PROTOCOL_FEE = 0.0125
PUMPPORTAL_LOCAL_FEE = 0.005
CONSERVATIVE_IMPACT = 0.0025
TOTAL_PERCENT_COST = PUMP_PROTOCOL_FEE + PUMPPORTAL_LOCAL_FEE + CONSERVATIVE_IMPACT


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summary(values: Sequence[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(clean),
        "min": min(clean) if clean else None,
        "median": statistics.median(clean) if clean else None,
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "p99": percentile(clean, 0.99),
        "max": max(clean) if clean else None,
        "mean": statistics.fmean(clean) if clean else None,
    }


class RpcPool:
    def __init__(self, urls: Sequence[str], timeout: float = 15.0):
        self.urls = tuple(dict.fromkeys(urls))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0
        self.errors: list[str] = []

    async def __aenter__(self) -> "RpcPool":
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def call(self, method: str, params: list[Any], retries: int = 7) -> Any:
        if not self.session:
            raise RuntimeError("RPC session is not open")
        last: Exception | None = None
        for attempt in range(retries):
            url = self.urls[(self.cursor + attempt) % len(self.urls)]
            self.request_id += 1
            payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
            try:
                async with self.session.post(url, json=payload) as response:
                    text = await response.text()
                    if response.status == 429:
                        raise RuntimeError(f"HTTP 429 from {url}")
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status} from {url}: {text[:300]}")
                    data = json.loads(text)
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    self.cursor = (self.cursor + attempt + 1) % len(self.urls)
                    return data.get("result")
            except Exception as exc:
                last = exc
                self.errors.append(f"{method}@{url}:{exc}")
                await asyncio.sleep(min(3.0, 0.15 * (2**attempt)))
        raise RuntimeError(f"RPC {method} exhausted retries: {last}")


def account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys: list[str] = []
    for item in message.get("accountKeys") or []:
        keys.append(str(item.get("pubkey")) if isinstance(item, Mapping) else str(item))
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(item) for item in loaded.get("writable") or [])
    keys.extend(str(item) for item in loaded.get("readonly") or [])
    return keys


def token_totals(rows: Iterable[Mapping[str, Any]], wallet: str) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for item in rows or []:
        if str(item.get("owner") or "") != wallet:
            continue
        mint = str(item.get("mint") or "")
        if not mint:
            continue
        token = item.get("uiTokenAmount") or {}
        value = token.get("uiAmountString", token.get("uiAmount", 0))
        result[mint] += float(value or 0)
    return result


@dataclass(slots=True)
class WalletTrade:
    signature: str
    slot: int
    block_time: int
    mint: str
    token_delta: float
    post_token_balance: float
    sol_delta: float
    pre_sol_balance: float
    post_sol_balance: float


@dataclass(slots=True)
class OraclePosition:
    mint: str
    entry_signature: str
    entry_slot: int
    entry_time: int
    exit_signature: str
    exit_slot: int
    exit_time: int
    tokens: float
    cost_sol: float
    proceeds_sol: float
    pnl_sol: float
    pre_entry_balance_sol: float
    requested_fraction: float
    sell_trades: list[WalletTrade]


async def fetch_wallet_positions(rpc: RpcPool, signature_limit: int) -> tuple[list[OraclePosition], dict[str, Any]]:
    signatures: list[Mapping[str, Any]] = []
    before = None
    while len(signatures) < signature_limit:
        config: dict[str, Any] = {"limit": min(1000, signature_limit - len(signatures))}
        if before:
            config["before"] = before
        batch = await rpc.call("getSignaturesForAddress", [E4_WALLET, config])
        if not batch:
            break
        signatures.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < config["limit"]:
            break

    semaphore = asyncio.Semaphore(4)

    async def fetch(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        async with semaphore:
            try:
                tx = await rpc.call(
                    "getTransaction",
                    [
                        row["signature"],
                        {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
                    ],
                )
                return row, tx if isinstance(tx, Mapping) else None
            except Exception:
                return row, None

    fetched = await asyncio.gather(*(fetch(row) for row in signatures))
    trades: list[WalletTrade] = []
    fetched_count = 0
    for row, transaction in fetched:
        if not transaction:
            continue
        fetched_count += 1
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            continue
        keys = account_keys(transaction)
        if E4_WALLET not in keys:
            continue
        wallet_index = keys.index(E4_WALLET)
        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        if wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
            continue
        pre_sol = float(pre_balances[wallet_index]) / LAMPORTS_PER_SOL
        post_sol = float(post_balances[wallet_index]) / LAMPORTS_PER_SOL
        pre = token_totals(meta.get("preTokenBalances") or [], E4_WALLET)
        post = token_totals(meta.get("postTokenBalances") or [], E4_WALLET)
        changed = []
        for mint in set(pre) | set(post):
            if mint in {WSOL_MINT, PUMP_TOKEN_MINT}:
                continue
            delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
            if abs(delta) > max(1e-9, abs(pre.get(mint, 0.0)) * 1e-12):
                changed.append((mint, delta, post.get(mint, 0.0)))
        if len(changed) != 1:
            continue
        mint, delta, post_token = changed[0]
        trades.append(
            WalletTrade(
                signature=str(row["signature"]),
                slot=int(transaction.get("slot") or row.get("slot") or 0),
                block_time=int(transaction.get("blockTime") or row.get("blockTime") or 0),
                mint=mint,
                token_delta=float(delta),
                post_token_balance=float(post_token),
                sol_delta=post_sol - pre_sol,
                pre_sol_balance=pre_sol,
                post_sol_balance=post_sol,
            )
        )
    trades.sort(key=lambda item: (item.slot, item.block_time, item.signature))

    open_positions: dict[str, dict[str, Any]] = {}
    closed: list[OraclePosition] = []
    reentries = 0
    for trade in trades:
        if trade.token_delta > 0:
            if trade.mint in open_positions:
                reentries += 1
                state = open_positions[trade.mint]
                state["tokens"] += trade.token_delta
                state["cost"] += max(0.0, -trade.sol_delta)
                state["buys"].append(trade)
            else:
                open_positions[trade.mint] = {
                    "entry": trade,
                    "tokens": trade.token_delta,
                    "cost": max(0.0, -trade.sol_delta),
                    "proceeds": 0.0,
                    "sold": 0.0,
                    "buys": [trade],
                    "sells": [],
                }
            continue
        state = open_positions.get(trade.mint)
        if not state:
            continue
        state["sells"].append(trade)
        state["sold"] += min(state["tokens"], max(0.0, -trade.token_delta))
        state["proceeds"] += max(0.0, trade.sol_delta)
        if trade.post_token_balance <= max(1e-6, state["tokens"] * 1e-7) or state["sold"] >= state["tokens"] * 0.995:
            entry: WalletTrade = state["entry"]
            fraction = state["cost"] / entry.pre_sol_balance if entry.pre_sol_balance > 0 else 0.0
            closed.append(
                OraclePosition(
                    mint=trade.mint,
                    entry_signature=entry.signature,
                    entry_slot=entry.slot,
                    entry_time=entry.block_time,
                    exit_signature=trade.signature,
                    exit_slot=trade.slot,
                    exit_time=trade.block_time,
                    tokens=state["tokens"],
                    cost_sol=state["cost"],
                    proceeds_sol=state["proceeds"],
                    pnl_sol=state["proceeds"] - state["cost"],
                    pre_entry_balance_sol=entry.pre_sol_balance,
                    requested_fraction=fraction,
                    sell_trades=list(state["sells"]),
                )
            )
            open_positions.pop(trade.mint, None)
    return closed, {
        "signatures_requested": len(signatures),
        "transactions_fetched": fetched_count,
        "wallet_trade_events": len(trades),
        "closed_positions": len(closed),
        "open_positions": len(open_positions),
        "reentries": reentries,
    }


@dataclass(slots=True)
class MarketEvent:
    mint: str
    kind: str
    trader: str | None
    signature: str
    slot: int
    tx_index: int
    tx_count: int
    logical_ms: float
    sol_amount: float
    token_amount: float
    price_sol: float | None
    fdv_usd: float | None
    creator: str | None
    is_cashback: bool | None


def event_price(item: Mapping[str, Any]) -> float | None:
    vsol = finite(item.get("virtual_sol_reserves") or item.get("virtual_quote_reserves"))
    vtok = finite(item.get("virtual_token_reserves"))
    if not vsol or not vtok or vsol <= 0 or vtok <= 0:
        return None
    return (vsol / LAMPORTS_PER_SOL) / (vtok / TOKEN_SCALE)


async def signature_window(rpc: RpcPool, position: OraclePosition) -> list[Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    try:
        pre = await rpc.call(
            "getSignaturesForAddress",
            [position.mint, {"limit": 80, "before": position.entry_signature}],
        )
        for item in pre or []:
            if int(item.get("slot") or 0) >= position.entry_slot - 3:
                rows[str(item["signature"])] = item
    except Exception:
        pass
    signals = [position.entry_signature, *(item.signature for item in position.sell_trades)]
    for older, newer in zip(signals, signals[1:]):
        try:
            batch = await rpc.call(
                "getSignaturesForAddress",
                [position.mint, {"limit": 1000, "before": newer, "until": older}],
            )
            for item in batch or []:
                if position.entry_slot - 2 <= int(item.get("slot") or 0) <= position.exit_slot + 3:
                    rows[str(item["signature"])] = item
        except Exception:
            pass
    for trade in [position.entry_signature, *(item.signature for item in position.sell_trades)]:
        rows[trade] = {"signature": trade}
    return list(rows.values())


async def reconstruct_timeline(rpc: RpcPool, position: OraclePosition, sol_usd: float) -> tuple[list[MarketEvent], dict[str, Any]]:
    signature_rows = await signature_window(rpc, position)
    semaphore = asyncio.Semaphore(4)

    async def fetch_tx(row: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None]:
        async with semaphore:
            signature = str(row["signature"])
            try:
                tx = await rpc.call(
                    "getTransaction",
                    [
                        signature,
                        {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
                    ],
                )
                return signature, tx if isinstance(tx, Mapping) else None
            except Exception:
                return signature, None

    fetched = await asyncio.gather(*(fetch_tx(row) for row in signature_rows))
    txs = {signature: tx for signature, tx in fetched if tx}
    slots = sorted({int(tx.get("slot") or 0) for tx in txs.values() if tx.get("slot") is not None})
    signature_order: dict[str, tuple[int, int]] = {}
    tx_counts: dict[int, int] = {}
    for slot in slots:
        try:
            block = await rpc.call(
                "getBlock",
                [
                    slot,
                    {"encoding": "json", "transactionDetails": "signatures", "rewards": False, "maxSupportedTransactionVersion": 0},
                ],
            )
        except Exception:
            continue
        signatures = list((block or {}).get("signatures") or [])
        tx_counts[slot] = max(1, len(signatures))
        for index, signature in enumerate(signatures):
            signature_order[str(signature)] = (slot, index)

    entry_order = signature_order.get(position.entry_signature, (position.entry_slot, 0))
    entry_count = tx_counts.get(entry_order[0], 1)
    events: list[MarketEvent] = []
    create_event: Mapping[str, Any] | None = None
    for signature, tx in txs.items():
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            continue
        slot = int(tx.get("slot") or 0)
        _, tx_index = signature_order.get(signature, (slot, 0))
        tx_count = tx_counts.get(slot, max(1, tx_index + 1))
        logical_ms = (slot - entry_order[0]) * 400.0 + (
            tx_index / tx_count - entry_order[1] / entry_count
        ) * 400.0
        decoded = anchor_events_from_logs(list(meta.get("logMessages") or []), PUMP_PROGRAM_ID)
        for item in decoded:
            if str(item.get("mint") or "") != position.mint:
                continue
            name = str(item.get("anchor_event") or "")
            if name not in {"CreateEvent", "TradeEvent"}:
                continue
            price = event_price(item)
            supply = finite(item.get("token_total_supply")) or DEFAULT_SUPPLY_RAW
            fdv = price * (supply / TOKEN_SCALE) * sol_usd if price else None
            if name == "CreateEvent":
                create_event = item
                kind = "CREATE"
                sol_amount = 0.0
                token_amount = 0.0
            else:
                kind = "BUY" if item.get("is_buy") else "SELL"
                sol_amount = float(item.get("sol_amount") or 0) / LAMPORTS_PER_SOL
                token_amount = float(item.get("token_amount") or 0) / TOKEN_SCALE
            events.append(
                MarketEvent(
                    mint=position.mint,
                    kind=kind,
                    trader=str(item.get("user") or item.get("creator") or "") or None,
                    signature=signature,
                    slot=slot,
                    tx_index=tx_index,
                    tx_count=tx_count,
                    logical_ms=logical_ms,
                    sol_amount=sol_amount,
                    token_amount=token_amount,
                    price_sol=price,
                    fdv_usd=fdv,
                    creator=str(item.get("creator") or "") or None,
                    is_cashback=(bool(item.get("is_cashback_enabled")) if "is_cashback_enabled" in item else None),
                )
            )
    events.sort(key=lambda item: (item.slot, item.tx_index, item.kind != "CREATE", item.signature))
    for index, event in enumerate(events):
        if not math.isfinite(event.logical_ms):
            event.logical_ms = index * 2.0
    return events, {
        "signature_rows": len(signature_rows),
        "transactions": len(txs),
        "events": len(events),
        "create_observed": create_event is not None,
    }


def next_trade(events: Sequence[MarketEvent], due_ms: float, start: int = 0) -> tuple[int, MarketEvent] | None:
    for index in range(start, len(events)):
        event = events[index]
        if event.kind in {"BUY", "SELL"} and event.price_sol and event.logical_ms >= due_ms:
            return index, event
    return None


def route_fee(amount: float, urgent: bool = False) -> float:
    return min(0.15, amount * (0.03 if urgent else 0.015))


@dataclass(slots=True)
class MirrorResult:
    mint: str
    delay_ms: int
    reconstructed: bool
    entry_fdv_usd: float | None
    entry_slippage_bps: float | None
    gross_return: float
    net_return: float
    actual_return: float
    hold_ms: float
    first_partial_fraction: float | None
    sell_count: int
    stale_fills: int
    requested_fraction: float
    actual_cost_sol: float
    actual_pnl_sol: float


def simulate_position(position: OraclePosition, events: Sequence[MarketEvent], delay_ms: int) -> MirrorResult | None:
    trades = [item for item in events if item.kind in {"BUY", "SELL"} and item.price_sol]
    if not trades:
        return None
    entry_signal = next((item for item in trades if item.signature == position.entry_signature), None)
    if entry_signal is None:
        return None
    entry_index = trades.index(entry_signal)
    resolved = next_trade(trades, entry_signal.logical_ms + delay_ms, entry_index)
    if resolved is None:
        return None
    fill_index, entry_fill = resolved
    entry_price = float(entry_fill.price_sol or 0)
    signal_price = float(entry_signal.price_sol or entry_price)
    if entry_price <= 0:
        return None
    oracle_remaining = position.tokens
    mirror_remaining = 1.0
    gross_proceeds = 0.0
    net_proceeds = 0.0
    first_partial: float | None = None
    stale = 0
    last_fill_ms = entry_fill.logical_ms
    cursor = fill_index
    for sell in position.sell_trades:
        signal = next((item for item in trades if item.signature == sell.signature), None)
        sold = min(oracle_remaining, max(0.0, -sell.token_delta))
        fraction = min(1.0, sold / oracle_remaining) if oracle_remaining > 0 else 1.0
        oracle_remaining = max(0.0, oracle_remaining - sold)
        if signal is None:
            stale += 1
            price = signal_price
            fill_ms = last_fill_ms
        else:
            result = next_trade(trades, signal.logical_ms + delay_ms, cursor)
            if result is None:
                stale += 1
                price = float(signal.price_sol or signal_price)
                fill_ms = signal.logical_ms + delay_ms
            else:
                cursor, fill = result
                price = float(fill.price_sol or signal_price)
                fill_ms = fill.logical_ms
        sold_mirror = mirror_remaining * fraction
        gross_proceeds += sold_mirror * (price / entry_price)
        urgent = fraction >= 0.999
        net_proceeds += sold_mirror * (price / entry_price) * (1.0 - TOTAL_PERCENT_COST)
        net_proceeds -= route_fee(sold_mirror, urgent)
        mirror_remaining = max(0.0, mirror_remaining - sold_mirror)
        last_fill_ms = max(last_fill_ms, fill_ms)
        if first_partial is None and fraction < 0.999:
            first_partial = fraction
    if mirror_remaining > 1e-9:
        gross_proceeds += mirror_remaining
        net_proceeds += mirror_remaining * (1.0 - TOTAL_PERCENT_COST) - route_fee(mirror_remaining, True)
    entry_cost = 1.0 + route_fee(1.0, False)
    actual_return = position.pnl_sol / position.cost_sol if position.cost_sol > 0 else 0.0
    return MirrorResult(
        mint=position.mint,
        delay_ms=delay_ms,
        reconstructed=True,
        entry_fdv_usd=entry_fill.fdv_usd,
        entry_slippage_bps=(entry_price / signal_price - 1.0) * 10_000 if signal_price else None,
        gross_return=gross_proceeds - 1.0,
        net_return=net_proceeds - entry_cost,
        actual_return=actual_return,
        hold_ms=max(0.0, last_fill_ms - entry_fill.logical_ms),
        first_partial_fraction=first_partial,
        sell_count=len(position.sell_trades),
        stale_fills=stale,
        requested_fraction=position.requested_fraction,
        actual_cost_sol=position.cost_sol,
        actual_pnl_sol=position.pnl_sol,
    )


def metrics(results: Sequence[MirrorResult]) -> dict[str, Any]:
    wins = [item for item in results if item.net_return > 0]
    losses = [item for item in results if item.net_return <= 0]
    gross_wins = [item for item in results if item.gross_return > 0]
    actual_wins = [item for item in results if item.actual_return > 0]
    return {
        "closed_positions": len(results),
        "net_win_rate": ratio(len(wins), len(results)),
        "gross_win_rate": ratio(len(gross_wins), len(results)),
        "actual_e4_net_win_rate_same_positions": ratio(len(actual_wins), len(results)),
        "normalized_net_pnl": sum(item.net_return for item in results),
        "normalized_gross_pnl": sum(item.gross_return for item in results),
        "actual_e4_normalized_pnl_same_positions": sum(item.actual_return for item in results),
        "profit_factor": (
            sum(item.net_return for item in wins) / abs(sum(item.net_return for item in losses))
            if losses and sum(item.net_return for item in losses) < 0
            else None
        ),
        "median_hold_ms": statistics.median([item.hold_ms for item in results]) if results else None,
        "fully_exited_within_5s_fraction": ratio(sum(item.hold_ms <= 5_000 for item in results), len(results)),
        "fully_exited_within_10s_fraction": ratio(sum(item.hold_ms <= 10_000 for item in results), len(results)),
        "losers_exited_within_5s_fraction": ratio(sum(item.hold_ms <= 5_000 for item in losses), len(losses)),
        "entry_slippage_bps": summary([item.entry_slippage_bps for item in results if item.entry_slippage_bps is not None]),
        "entry_fdv_usd": summary([item.entry_fdv_usd for item in results if item.entry_fdv_usd is not None]),
        "first_partial_20pct_count": sum(item.first_partial_fraction is not None and abs(item.first_partial_fraction - 0.20) <= 0.03 for item in results),
        "first_partial_30pct_count": sum(item.first_partial_fraction is not None and abs(item.first_partial_fraction - 0.30) <= 0.03 for item in results),
        "stale_fill_count": sum(item.stale_fills for item in results),
        "results": [asdict(item) for item in results],
    }


def portfolio(results: Sequence[MirrorResult], starting_balance: float, min_position: float) -> dict[str, Any]:
    balance = starting_balance
    trades = 0
    wins = 0
    skipped = 0
    pnl = 0.0
    sizes: list[float] = []
    for result in results:
        fraction = min(0.11, max(0.005, result.requested_fraction or 0.0157))
        deployable = max(0.0, balance - 0.03)
        size = min(5.0, deployable * fraction)
        if size < min_position:
            skipped += 1
            continue
        trade_pnl = size * result.net_return
        balance += trade_pnl
        pnl += trade_pnl
        trades += 1
        wins += int(trade_pnl > 0)
        sizes.append(size)
    return {
        "starting_balance_sol": starting_balance,
        "minimum_position_sol": min_position,
        "ending_balance_sol": balance,
        "net_pnl_sol": pnl,
        "trades": trades,
        "skipped_for_size": skipped,
        "win_rate": ratio(wins, trades),
        "sizes_sol": summary(sizes),
    }


async def sol_usd() -> float:
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                headers={"accept": "application/json", "user-agent": "Gambit-E4-Mirror-Stress/1"},
            ) as response:
                data = await response.json()
                value = finite((data.get("solana") or {}).get("usd"))
                if value and value > 0:
                    return value
    except Exception:
        pass
    return 200.0


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    price = await sol_usd()
    async with RpcPool(RPC_URLS) as rpc:
        positions, wallet_meta = await fetch_wallet_positions(rpc, args.signatures)
        positions = positions[-args.positions :]
        timelines: dict[str, list[MarketEvent]] = {}
        reconstruction: dict[str, Any] = {}
        semaphore = asyncio.Semaphore(2)

        async def reconstruct(position: OraclePosition) -> None:
            async with semaphore:
                events, info = await reconstruct_timeline(rpc, position, price)
                timelines[position.mint] = events
                reconstruction[position.mint] = info

        await asyncio.gather(*(reconstruct(position) for position in positions))
        scenarios: dict[str, Any] = {}
        result_sets: dict[int, list[MirrorResult]] = {}
        for delay in args.delays:
            values = []
            for position in positions:
                result = simulate_position(position, timelines.get(position.mint, []), delay)
                if result:
                    values.append(result)
            result_sets[delay] = values
            scenarios[str(delay)] = metrics(values)
        primary = result_sets.get(args.primary_delay, [])
        portfolios = [
            portfolio(primary, balance, minimum)
            for balance in (0.3, 1.2, 5.0, 20.0)
            for minimum in (0.005, 0.01)
        ]
        fractions = [position.requested_fraction for position in positions if position.requested_fraction > 0]
        actual = {
            "positions": len(positions),
            "net_win_rate": ratio(sum(position.pnl_sol > 0 for position in positions), len(positions)),
            "net_pnl_sol": sum(position.pnl_sol for position in positions),
            "normalized_pnl": sum(position.pnl_sol / position.cost_sol for position in positions if position.cost_sol > 0),
            "position_fraction": summary(fractions),
            "cost_sol": summary([position.cost_sol for position in positions]),
        }
        primary_metrics = scenarios.get(str(args.primary_delay), {})
        simulation_ready = (
            (primary_metrics.get("closed_positions") or 0) >= 15
            and (primary_metrics.get("net_win_rate") or 0) >= max(0.55, (actual.get("net_win_rate") or 0) - 0.10)
            and (primary_metrics.get("normalized_net_pnl") or 0) > 0
            and (primary_metrics.get("fully_exited_within_10s_fraction") or 0) >= 0.80
            and (primary_metrics.get("losers_exited_within_5s_fraction") or 0) >= 0.80
        )
        report = {
            "generated_at": time.time(),
            "mode": "HYPOTHETICAL_ORACLE_MIRROR_NO_MAINNET_TRANSACTIONS",
            "sol_usd": price,
            "wallet_fetch": wallet_meta,
            "actual_e4": actual,
            "positions_requested": args.positions,
            "positions_reconstructed": len(primary),
            "delays_ms": args.delays,
            "primary_delay_ms": args.primary_delay,
            "scenarios": scenarios,
            "portfolios": portfolios,
            "reconstruction": reconstruction,
            "simulation_ready": simulation_ready,
            "live_certified": False,
            "live_certification_reason": "No funded mainnet copy order and no authenticated private-route canary were used.",
            "rpc_errors": rpc.errors[-100:],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        timeline_path = args.output.with_name(args.output.stem + "-timelines.jsonl")
        with timeline_path.open("w", encoding="utf-8") as handle:
            for position in positions:
                handle.write(
                    json.dumps(
                        {
                            "position": asdict(position),
                            "events": [asdict(event) for event in timelines.get(position.mint, [])],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Hypothetical delayed mirror of the observed E4 wallet")
    value.add_argument("--signatures", type=int, default=800)
    value.add_argument("--positions", type=int, default=30)
    value.add_argument("--delays", default="0,50,100,150,250,400,600,1000")
    value.add_argument("--primary-delay", type=int, default=250)
    value.add_argument("--output", type=Path, default=Path("outputs/e4-oracle-mirror-stress.json"))
    return value


def main() -> None:
    args = parser().parse_args()
    args.delays = [int(item.strip()) for item in str(args.delays).split(",") if item.strip()]
    report = asyncio.run(main_async(args))
    print(json.dumps({
        "positions_reconstructed": report["positions_reconstructed"],
        "primary": report["scenarios"].get(str(report["primary_delay_ms"]), {}),
        "simulation_ready": report["simulation_ready"],
        "live_certified": report["live_certified"],
    }, indent=2))


if __name__ == "__main__":
    main()
