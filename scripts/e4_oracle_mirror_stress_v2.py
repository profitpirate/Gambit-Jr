#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

from memecoin_bot import e4_hardening_v3  # noqa: F401 - production policy patches
from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs


def _load_legacy_mirror():
    path = Path(__file__).with_name("e4_oracle_mirror_stress.py")
    name = "e4_oracle_mirror_stress_legacy"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_live_harness():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_mirror_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load_legacy_mirror()
live = _load_live_harness()
core = live.core

E4_WALLET = legacy.E4_WALLET
WSOL_MINT = legacy.WSOL_MINT
PUMP_TOKEN_MINT = legacy.PUMP_TOKEN_MINT
LAMPORTS_PER_SOL = legacy.LAMPORTS_PER_SOL
DEFAULT_SUPPLY_RAW = legacy.DEFAULT_SUPPLY_RAW
TOKEN_SCALE = legacy.TOKEN_SCALE
DEFAULT_RPCS = (
    "https://solana-rpc.publicnode.com",
    "https://solana-mainnet.api.onfinality.io/public",
    "https://api.mainnet-beta.solana.com",
)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    point = (len(ordered) - 1) * q
    low = math.floor(point)
    high = math.ceil(point)
    if low == high:
        return ordered[low]
    weight = point - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summary(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(item) for item in values if math.isfinite(float(item))]
    return {
        "count": len(clean),
        "min": min(clean) if clean else None,
        "median": statistics.median(clean) if clean else None,
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "max": max(clean) if clean else None,
        "mean": statistics.fmean(clean) if clean else None,
    }


class FastRpcPool:
    def __init__(self, urls: Sequence[str], timeout_seconds: float = 6.0, concurrency: int = 16):
        self.urls = tuple(dict.fromkeys(url for url in urls if url))
        self.timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=min(3.0, timeout_seconds),
            sock_read=timeout_seconds,
        )
        self.concurrency = asyncio.Semaphore(concurrency)
        self.session: aiohttp.ClientSession | None = None
        self.cursor = 0
        self.request_id = 0
        self.errors: list[str] = []
        self.latencies_ms: list[float] = []
        self.success_by_endpoint: dict[str, int] = defaultdict(int)
        self.failure_by_endpoint: dict[str, int] = defaultdict(int)

    async def __aenter__(self) -> "FastRpcPool":
        connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def call(
        self,
        method: str,
        params: list[Any],
        retries: int = 2,
        *,
        optional: bool = False,
    ) -> Any:
        if not self.session:
            raise RuntimeError("RPC session is not open")
        last_error: Exception | None = None
        attempts = max(1, retries) * max(1, len(self.urls))
        for offset in range(attempts):
            url = self.urls[(self.cursor + offset) % len(self.urls)]
            self.request_id += 1
            payload = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
            started = time.perf_counter_ns()
            try:
                async with self.concurrency:
                    async with self.session.post(url, json=payload) as response:
                        text = await response.text()
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                if response.status == 429:
                    raise RuntimeError(f"HTTP 429 from {url}")
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} from {url}: {text[:180]}")
                data = json.loads(text)
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                self.latencies_ms.append(elapsed)
                self.success_by_endpoint[url] += 1
                return data.get("result")
            except Exception as exc:
                last_error = exc
                self.failure_by_endpoint[url] += 1
                self.errors.append(f"{method}@{url}:{type(exc).__name__}:{exc}")
                await asyncio.sleep(min(0.4, 0.04 * (offset + 1) + random.random() * 0.03))
        if optional:
            return None
        raise RuntimeError(f"RPC {method} exhausted bounded retries: {last_error}")


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


def account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = [
        str(item.get("pubkey")) if isinstance(item, Mapping) else str(item)
        for item in message.get("accountKeys") or []
    ]
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


async def fetch_wallet_positions(
    rpc: FastRpcPool,
    signature_limit: int,
    position_limit: int,
) -> tuple[list[OraclePosition], dict[str, Any]]:
    signature_rows = await rpc.call(
        "getSignaturesForAddress",
        [E4_WALLET, {"limit": min(1000, signature_limit)}],
        retries=3,
    )
    signatures = [row for row in signature_rows or [] if row.get("err") is None]
    semaphore = asyncio.Semaphore(14)
    fetched_count = 0
    completed_fetches = 0

    async def fetch(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        async with semaphore:
            tx = await rpc.call(
                "getTransaction",
                [
                    row["signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                retries=2,
                optional=True,
            )
            return row, tx if isinstance(tx, Mapping) else None

    tasks = [asyncio.create_task(fetch(row)) for row in signatures]
    fetched: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    for future in asyncio.as_completed(tasks):
        row, tx = await future
        fetched.append((row, tx))
        completed_fetches += 1
        if completed_fetches % 50 == 0:
            print(f"wallet transactions fetched: {completed_fetches}/{len(tasks)}", flush=True)

    trades: list[WalletTrade] = []
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
        if (
            trade.post_token_balance <= max(1e-6, state["tokens"] * 1e-7)
            or state["sold"] >= state["tokens"] * 0.995
        ):
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
    selected = closed[-position_limit:]
    return selected, {
        "signatures_requested": len(signatures),
        "transactions_fetched": fetched_count,
        "wallet_trade_events": len(trades),
        "closed_positions_available": len(closed),
        "positions_selected": len(selected),
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
    order: int
    logical_ms: float
    sol_amount: float
    token_amount: float
    price_sol: float | None
    fdv_usd: float | None
    creator: str | None


def event_price(item: Mapping[str, Any]) -> float | None:
    vsol = legacy.finite(item.get("virtual_sol_reserves") or item.get("virtual_quote_reserves"))
    vtok = legacy.finite(item.get("virtual_token_reserves"))
    if not vsol or not vtok or vsol <= 0 or vtok <= 0:
        return None
    return (vsol / LAMPORTS_PER_SOL) / (vtok / TOKEN_SCALE)


async def signature_window(rpc: FastRpcPool, position: OraclePosition) -> list[Mapping[str, Any]]:
    after_task = asyncio.create_task(
        rpc.call(
            "getSignaturesForAddress",
            [position.mint, {"limit": 250, "until": position.entry_signature}],
            retries=2,
            optional=True,
        )
    )
    before_task = asyncio.create_task(
        rpc.call(
            "getSignaturesForAddress",
            [position.mint, {"limit": 50, "before": position.entry_signature}],
            retries=2,
            optional=True,
        )
    )
    after, before = await asyncio.gather(after_task, before_task)
    rows: dict[str, Mapping[str, Any]] = {}
    for item in [*(after or []), *(before or [])]:
        slot = int(item.get("slot") or 0)
        if position.entry_slot - 3 <= slot <= position.exit_slot + 3:
            rows[str(item["signature"])] = item
    for signature in [position.entry_signature, *(row.signature for row in position.sell_trades)]:
        rows.setdefault(signature, {"signature": signature})
    return list(rows.values())


async def reconstruct_timeline(
    rpc: FastRpcPool,
    position: OraclePosition,
    sol_usd: float,
) -> tuple[list[MarketEvent], dict[str, Any]]:
    rows = await signature_window(rpc, position)
    semaphore = asyncio.Semaphore(12)

    async def fetch(index: int, row: Mapping[str, Any]) -> tuple[int, str, Mapping[str, Any] | None]:
        async with semaphore:
            signature = str(row["signature"])
            tx = await rpc.call(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "json",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                retries=2,
                optional=True,
            )
            return index, signature, tx if isinstance(tx, Mapping) else None

    fetched = await asyncio.gather(*(fetch(index, row) for index, row in enumerate(rows)))
    tx_rows = [(index, signature, tx) for index, signature, tx in fetched if tx]
    # getSignaturesForAddress is newest-first. Reverse the source rank inside a
    # slot to approximate chronological transaction order without expensive
    # getBlock calls. Slot boundaries remain exact on-chain evidence.
    by_slot: dict[int, list[tuple[int, str, Mapping[str, Any]]]] = defaultdict(list)
    for source_index, signature, tx in tx_rows:
        by_slot[int(tx.get("slot") or 0)].append((source_index, signature, tx))
    signature_order: dict[str, tuple[int, int, int]] = {}
    for slot, values in by_slot.items():
        ordered = sorted(values, key=lambda item: item[0], reverse=True)
        for order, (_, signature, _) in enumerate(ordered):
            signature_order[signature] = (slot, order, max(1, len(ordered)))

    entry_slot, entry_order, entry_count = signature_order.get(
        position.entry_signature,
        (position.entry_slot, 0, 1),
    )
    events: list[MarketEvent] = []
    create_observed = False
    for _, signature, tx in tx_rows:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            continue
        slot, order, count = signature_order.get(signature, (int(tx.get("slot") or 0), 0, 1))
        logical_ms = (slot - entry_slot) * 400.0 + (
            order / count - entry_order / entry_count
        ) * 400.0
        decoded = anchor_events_from_logs(list(meta.get("logMessages") or []), PUMP_PROGRAM_ID)
        for item in decoded:
            if str(item.get("mint") or "") != position.mint:
                continue
            name = str(item.get("anchor_event") or "")
            if name not in {"CreateEvent", "TradeEvent"}:
                continue
            price = event_price(item)
            supply = legacy.finite(item.get("token_total_supply")) or DEFAULT_SUPPLY_RAW
            fdv = price * (supply / TOKEN_SCALE) * sol_usd if price else None
            if name == "CreateEvent":
                create_observed = True
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
                    order=order,
                    logical_ms=logical_ms,
                    sol_amount=sol_amount,
                    token_amount=token_amount,
                    price_sol=price,
                    fdv_usd=fdv,
                    creator=str(item.get("creator") or "") or None,
                )
            )
    events.sort(key=lambda item: (item.slot, item.order, item.kind != "CREATE", item.signature))
    return events, {
        "signature_rows": len(rows),
        "transactions": len(tx_rows),
        "events": len(events),
        "create_observed": create_observed,
        "entry_observed": any(item.signature == position.entry_signature for item in events),
        "exit_observed": any(item.signature == position.exit_signature for item in events),
    }


def next_trade(events: Sequence[MarketEvent], due_ms: float, start: int = 0) -> tuple[int, MarketEvent] | None:
    for index in range(start, len(events)):
        event = events[index]
        if event.kind in {"BUY", "SELL"} and event.price_sol and event.logical_ms >= due_ms:
            return index, event
    return None


def to_core_event(event: MarketEvent, origin_ns: int, event_id: int) -> Any:
    kind = {
        "CREATE": core.EventKind.CREATE,
        "BUY": core.EventKind.BUY,
        "SELL": core.EventKind.SELL,
    }[event.kind]
    timestamp = origin_ns + int(event.logical_ms * 1_000_000)
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=event.mint,
        source_ns=timestamp,
        received_ns=timestamp,
        slot=event.slot,
        signature=event.signature,
        trader=event.trader,
        sol_amount=event.sol_amount,
        token_amount=event.token_amount,
        price_sol=event.price_sol,
        fdv_usd=event.fdv_usd,
        creator=event.creator,
    )


@dataclass(slots=True)
class GuardianResult:
    mint: str
    delay_ms: int
    entry_fdv_usd: float | None
    entry_slippage_bps: float | None
    gross_return: float
    net_return: float
    actual_return: float
    hold_ms: float
    first_partial_fraction: float | None
    sell_count: int
    stale_fills: int
    failure_exit: bool
    closed: bool
    fractions_sum: float


def simulate_guardian(
    position: OraclePosition,
    events: Sequence[MarketEvent],
    delay_ms: int,
) -> GuardianResult | None:
    trades = [item for item in events if item.kind in {"BUY", "SELL"} and item.price_sol]
    if not trades:
        return None
    entry_signal = next((item for item in trades if item.signature == position.entry_signature), None)
    if entry_signal is None:
        return None
    signal_index = trades.index(entry_signal)
    resolved = next_trade(trades, entry_signal.logical_ms + delay_ms, signal_index)
    if resolved is None:
        return None
    fill_index, fill = resolved
    entry_price = float(fill.price_sol or 0)
    signal_price = float(entry_signal.price_sol or entry_price)
    if entry_price <= 0:
        return None

    origin_ns = time.time_ns() - int(max(0.0, fill.logical_ms) * 1_000_000)
    state = core.TokenState(position.mint)
    for index, event in enumerate(events):
        if event.logical_ms > fill.logical_ms:
            break
        state.apply(to_core_event(event, origin_ns, index + 1), None)
    policy = core.E4Policy(core.Settings(model_path=Path("missing-model.json")))
    runtime_position = core.Position(
        position_id=f"mirror:{position.mint}:{delay_ms}",
        mint=position.mint,
        status=core.PositionStatus.OPEN,
        opened_ns=time.time_ns(),
        entry_sol=1.0,
        tokens=1.0 / entry_price,
        remaining=1.0 / entry_price,
        entry_price=entry_price,
        max_price=entry_price,
        last_price=entry_price,
        entry_signature=position.entry_signature,
    )
    remaining = 1.0
    gross_proceeds = 0.0
    net_proceeds = 0.0
    first_partial: float | None = None
    stale = 0
    failure = False
    sell_count = 0
    fill_ms = fill.logical_ms
    cursor = fill_index + 1
    last_fill_ms = fill_ms
    fractions: list[float] = []
    for index in range(cursor, len(trades)):
        event = trades[index]
        state.apply(to_core_event(event, origin_ns, 100_000 + index), None)
        elapsed_ms = max(0.0, event.logical_ms - fill_ms)
        runtime_position.opened_ns = time.time_ns() - int(elapsed_ms * 1_000_000)
        action, fraction, reason = policy.exit(runtime_position, state)
        if not action.startswith("SELL"):
            continue
        target = next_trade(trades, event.logical_ms + delay_ms, index)
        if target is None:
            stale += 1
            sell_event = event
        else:
            _, sell_event = target
        price = float(sell_event.price_sol or runtime_position.last_price or entry_price)
        original_fraction = remaining if fraction >= 0.999 else remaining * fraction
        original_fraction = min(remaining, max(0.0, original_fraction))
        if original_fraction <= 0:
            continue
        fractions.append(original_fraction)
        gross_proceeds += original_fraction * (price / entry_price)
        net = original_fraction * (price / entry_price) * (1.0 - legacy.TOTAL_PERCENT_COST)
        net -= legacy.route_fee(original_fraction, fraction >= 0.999)
        net_proceeds += max(0.0, net)
        remaining = max(0.0, remaining - original_fraction)
        runtime_position.remaining = runtime_position.tokens * remaining
        runtime_position.last_price = price
        runtime_position.max_price = max(runtime_position.max_price, price)
        last_fill_ms = max(last_fill_ms, sell_event.logical_ms)
        sell_count += 1
        if first_partial is None and fraction < 0.999:
            first_partial = original_fraction
            runtime_position.first_partial_done = True
            runtime_position.first_partial_fraction = original_fraction
        if "failure" in reason.lower() or "confirmation failed" in reason.lower():
            failure = True
        if remaining <= 1e-9 or fraction >= 0.999:
            break

    if remaining > 1e-9:
        settings = policy.settings
        horizon = fill_ms + settings.max_hold_ms
        target = next_trade(trades, horizon + delay_ms, fill_index)
        if target is None:
            stale += 1
            sell_event = trades[-1]
        else:
            _, sell_event = target
        price = float(sell_event.price_sol or entry_price)
        fractions.append(remaining)
        gross_proceeds += remaining * (price / entry_price)
        net = remaining * (price / entry_price) * (1.0 - legacy.TOTAL_PERCENT_COST)
        net -= legacy.route_fee(remaining, True)
        net_proceeds += max(0.0, net)
        last_fill_ms = max(last_fill_ms, sell_event.logical_ms, horizon)
        remaining = 0.0
        sell_count += 1

    entry_cost = 1.0 + legacy.route_fee(1.0, False)
    actual_return = position.pnl_sol / position.cost_sol if position.cost_sol > 0 else 0.0
    return GuardianResult(
        mint=position.mint,
        delay_ms=delay_ms,
        entry_fdv_usd=fill.fdv_usd,
        entry_slippage_bps=(entry_price / signal_price - 1.0) * 10_000 if signal_price else None,
        gross_return=gross_proceeds - 1.0,
        net_return=net_proceeds - entry_cost,
        actual_return=actual_return,
        hold_ms=max(0.0, last_fill_ms - fill_ms),
        first_partial_fraction=first_partial,
        sell_count=sell_count,
        stale_fills=stale,
        failure_exit=failure,
        closed=remaining <= 1e-9,
        fractions_sum=sum(fractions),
    )


def guardian_metrics(results: Sequence[GuardianResult]) -> dict[str, Any]:
    wins = [item for item in results if item.net_return > 0]
    losses = [item for item in results if item.net_return <= 0]
    return {
        "closed_positions": len(results),
        "net_win_rate": safe_ratio(len(wins), len(results)),
        "gross_win_rate": safe_ratio(sum(item.gross_return > 0 for item in results), len(results)),
        "normalized_net_pnl": sum(item.net_return for item in results),
        "normalized_gross_pnl": sum(item.gross_return for item in results),
        "actual_e4_normalized_pnl_same_positions": sum(item.actual_return for item in results),
        "actual_e4_win_rate_same_positions": safe_ratio(
            sum(item.actual_return > 0 for item in results),
            len(results),
        ),
        "profit_factor": (
            sum(item.net_return for item in wins) / abs(sum(item.net_return for item in losses))
            if losses and sum(item.net_return for item in losses) < 0
            else None
        ),
        "median_hold_ms": statistics.median([item.hold_ms for item in results]) if results else None,
        "fully_exited_within_5s_fraction": safe_ratio(
            sum(item.hold_ms <= 5_000 for item in results), len(results)
        ),
        "fully_exited_within_10s_fraction": safe_ratio(
            sum(item.hold_ms <= 10_000 for item in results), len(results)
        ),
        "losers_exited_within_5s_fraction": safe_ratio(
            sum(item.hold_ms <= 5_000 for item in losses), len(losses)
        ),
        "entry_slippage_bps": summary(
            [item.entry_slippage_bps for item in results if item.entry_slippage_bps is not None]
        ),
        "entry_fdv_usd": summary(
            [item.entry_fdv_usd for item in results if item.entry_fdv_usd is not None]
        ),
        "first_partial_20pct_count": sum(
            item.first_partial_fraction is not None
            and abs(item.first_partial_fraction - 0.20) <= 0.03
            for item in results
        ),
        "first_partial_30pct_count": sum(
            item.first_partial_fraction is not None
            and abs(item.first_partial_fraction - 0.30) <= 0.03
            for item in results
        ),
        "stale_fill_count": sum(item.stale_fills for item in results),
        "unclosed_count": sum(not item.closed for item in results),
        "fraction_integrity_failures": sum(abs(item.fractions_sum - 1.0) > 1e-8 for item in results),
        "results": [asdict(item) for item in results],
    }


def deterministic_hash(values: Sequence[GuardianResult]) -> str:
    payload = [asdict(item) for item in sorted(values, key=lambda row: row.mint)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def fetch_sol_usd() -> float:
    fallback = float(os.getenv("E4_SOL_USD_FALLBACK", "150"))
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                headers={"user-agent": "Gambit-E4-Mirror-V2/1"},
            ) as response:
                data = await response.json()
                value = legacy.finite((data.get("solana") or {}).get("usd"))
                if value and value > 0:
                    return value
    except Exception:
        return fallback
    return fallback


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = output.with_name(output.stem + "-checkpoint.json")
    timeline_output = output.with_name(output.stem + "-timelines.jsonl")
    urls = tuple(filter(None, os.getenv("E4_MIRROR_RPC_URLS", "").split(","))) or DEFAULT_RPCS
    price = await fetch_sol_usd()

    async with FastRpcPool(urls, timeout_seconds=args.rpc_timeout, concurrency=args.rpc_concurrency) as rpc:
        positions, wallet_meta = await asyncio.wait_for(
            fetch_wallet_positions(rpc, args.signatures, args.positions),
            timeout=args.wallet_timeout,
        )
        print(f"selected actual E4 closed positions: {len(positions)}", flush=True)
        timelines: dict[str, list[MarketEvent]] = {}
        reconstruction: dict[str, Any] = {}
        reconstruction_errors: list[str] = []
        semaphore = asyncio.Semaphore(args.position_concurrency)

        async def run_position(position: OraclePosition) -> tuple[OraclePosition, list[MarketEvent], dict[str, Any]]:
            async with semaphore:
                events, info = await asyncio.wait_for(
                    reconstruct_timeline(rpc, position, price),
                    timeout=args.position_timeout,
                )
                return position, events, info

        tasks = [asyncio.create_task(run_position(position)) for position in positions]
        completed = 0
        for future in asyncio.as_completed(tasks, timeout=args.reconstruction_wall_timeout):
            try:
                position, events, info = await future
                timelines[position.mint] = events
                reconstruction[position.mint] = info
            except Exception as exc:
                reconstruction_errors.append(f"{type(exc).__name__}:{exc}")
            completed += 1
            checkpoint.write_text(
                json.dumps(
                    {
                        "completed_tasks": completed,
                        "requested_tasks": len(tasks),
                        "timelines_reconstructed": len(timelines),
                        "errors": reconstruction_errors,
                        "elapsed_seconds": time.time() - started,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"timeline tasks complete: {completed}/{len(tasks)}; usable={len(timelines)}",
                flush=True,
            )
        for task in tasks:
            if not task.done():
                task.cancel()

        delays = args.delays
        oracle_scenarios: dict[str, Any] = {}
        guardian_scenarios: dict[str, Any] = {}
        guardian_sets: dict[int, list[GuardianResult]] = {}
        for delay in delays:
            oracle_values = []
            guardian_values = []
            for position in positions:
                events = timelines.get(position.mint, [])
                oracle_result = legacy.simulate_position(position, events, delay)
                if oracle_result:
                    oracle_values.append(oracle_result)
                guardian_result = simulate_guardian(position, events, delay)
                if guardian_result:
                    guardian_values.append(guardian_result)
            oracle_scenarios[str(delay)] = legacy.metrics(oracle_values)
            guardian_scenarios[str(delay)] = guardian_metrics(guardian_values)
            guardian_sets[delay] = guardian_values

        primary = guardian_sets.get(args.primary_delay, [])
        replay_hashes = []
        replay_failures = []
        expected_hash = deterministic_hash(primary)
        for round_number in range(args.replay_rounds):
            replay = []
            try:
                for position in positions:
                    result = simulate_guardian(
                        position,
                        timelines.get(position.mint, []),
                        args.primary_delay,
                    )
                    if result:
                        replay.append(result)
                digest = deterministic_hash(replay)
                replay_hashes.append(digest)
                if digest != expected_hash:
                    replay_failures.append(
                        f"round {round_number}: {digest} != {expected_hash}"
                    )
            except Exception as exc:
                replay_failures.append(f"round {round_number}:{type(exc).__name__}:{exc}")

        actual = {
            "positions": len(positions),
            "win_rate": safe_ratio(sum(position.pnl_sol > 0 for position in positions), len(positions)),
            "net_pnl_sol": sum(position.pnl_sol for position in positions),
            "normalized_pnl": sum(
                position.pnl_sol / position.cost_sol
                for position in positions
                if position.cost_sol > 0
            ),
            "median_hold_ms": (
                statistics.median(
                    [max(0, position.exit_time - position.entry_time) * 1000 for position in positions]
                )
                if positions
                else None
            ),
            "position_fraction": summary(
                [position.requested_fraction for position in positions if position.requested_fraction > 0]
            ),
            "cost_sol": summary([position.cost_sol for position in positions]),
            "positions_detail": [asdict(position) for position in positions],
        }
        primary_metrics = guardian_scenarios.get(str(args.primary_delay), {})
        enough = (primary_metrics.get("closed_positions") or 0) >= args.minimum_comparable_positions
        deterministic = not replay_failures and len(set(replay_hashes)) <= 1
        integrity = (
            (primary_metrics.get("unclosed_count") or 0) == 0
            and (primary_metrics.get("fraction_integrity_failures") or 0) == 0
        )
        comparable = (
            enough
            and (primary_metrics.get("net_win_rate") or 0) >= max(
                0.55,
                float(actual.get("win_rate") or 0.65) - 0.12,
            )
            and (primary_metrics.get("normalized_net_pnl") or 0) > 0
            and (primary_metrics.get("fully_exited_within_10s_fraction") or 0) >= 0.80
            and (primary_metrics.get("losers_exited_within_5s_fraction") or 0) >= 0.80
            and (primary_metrics.get("stale_fill_count") or 0)
            <= max(1, int((primary_metrics.get("closed_positions") or 0) * 0.10))
        )
        report = {
            "report_version": "e4-oracle-mirror-v2",
            "generated_at_epoch": time.time(),
            "mode": "HYPOTHETICAL_REPLAY_OF_ACTUAL_E4_TRADES_AND_ACTUAL_ONCHAIN_MARKET_EVENTS",
            "synthetic_coins_used": False,
            "synthetic_price_paths_used": False,
            "mainnet_transactions_sent": 0,
            "sol_usd": price,
            "wallet_fetch": wallet_meta,
            "actual_e4": actual,
            "positions_requested": args.positions,
            "positions_with_timelines": len(timelines),
            "reconstruction": reconstruction,
            "reconstruction_errors": reconstruction_errors,
            "delays_ms": delays,
            "primary_delay_ms": args.primary_delay,
            "oracle_exit_signal_mirror": oracle_scenarios,
            "gambit_guardian_replay": guardian_scenarios,
            "repeated_replay": {
                "rounds": args.replay_rounds,
                "expected_hash": expected_hash,
                "unique_hashes": sorted(set(replay_hashes)),
                "failures": replay_failures,
                "passed": deterministic,
            },
            "rpc": {
                "latency_ms": summary(rpc.latencies_ms),
                "errors": rpc.errors[-100:],
                "success_by_endpoint": dict(rpc.success_by_endpoint),
                "failure_by_endpoint": dict(rpc.failure_by_endpoint),
            },
            "verdict": {
                "sample_sufficient": enough,
                "deterministic": deterministic,
                "position_integrity": integrity,
                "performance_comparable_to_actual_e4": comparable,
                "good_to_go_live": deterministic and integrity and comparable,
                "classification": (
                    "GOOD_TO_GO_LIVE"
                    if deterministic and integrity and comparable
                    else "MECHANICALLY_STABLE_BUT_NOT_E4_COMPARABLE"
                    if deterministic and integrity
                    else "NOT_READY_MECHANICAL_FAILURE"
                ),
            },
            "elapsed_seconds": time.time() - started,
            "limitations": [
                "Every position and price movement comes from actual E4 wallet trades and actual Solana transactions; no synthetic coin or synthetic price path is used.",
                "Intra-slot order is reconstructed from address-history order because public RPC getBlock calls caused the previous harness to time out.",
                "Fills are counterfactual at the next observed real trade after the configured delay; no funds were sent.",
                "A hypothesis-only replay cannot certify authenticated private-route mainnet landing performance.",
            ],
        }
        output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        with timeline_output.open("w", encoding="utf-8") as handle:
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
    value = argparse.ArgumentParser(
        description="Bounded actual-E4 and actual-market replay using production Gambit Guardian"
    )
    value.add_argument("--signatures", type=int, default=350)
    value.add_argument("--positions", type=int, default=18)
    value.add_argument("--delays", default="0,50,100,150,250,400,600,1000")
    value.add_argument("--primary-delay", type=int, default=250)
    value.add_argument("--rpc-timeout", type=float, default=6.0)
    value.add_argument("--rpc-concurrency", type=int, default=16)
    value.add_argument("--wallet-timeout", type=float, default=600.0)
    value.add_argument("--position-timeout", type=float, default=90.0)
    value.add_argument("--position-concurrency", type=int, default=4)
    value.add_argument("--reconstruction-wall-timeout", type=float, default=900.0)
    value.add_argument("--minimum-comparable-positions", type=int, default=12)
    value.add_argument("--replay-rounds", type=int, default=100)
    value.add_argument("--output", type=Path, default=Path("outputs/e4-oracle-mirror-v2.json"))
    return value


def main() -> None:
    args = parser().parse_args()
    args.delays = [int(item.strip()) for item in str(args.delays).split(",") if item.strip()]
    report = asyncio.run(main_async(args))
    primary = report["gambit_guardian_replay"].get(str(report["primary_delay_ms"]), {})
    print(
        json.dumps(
            {
                "output": str(args.output),
                "positions_with_timelines": report["positions_with_timelines"],
                "primary": {
                    "closed_positions": primary.get("closed_positions"),
                    "net_win_rate": primary.get("net_win_rate"),
                    "normalized_net_pnl": primary.get("normalized_net_pnl"),
                    "median_hold_ms": primary.get("median_hold_ms"),
                },
                "classification": report["verdict"]["classification"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
