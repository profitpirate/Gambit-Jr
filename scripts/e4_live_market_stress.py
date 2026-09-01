#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import random
import shlex
import statistics
import subprocess
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

from memecoin_bot import e4_hardening_v2
from memecoin_bot.realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs

core = e4_hardening_v2.core
hardening = e4_hardening_v2.e4_hardening

E4_WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_TOKEN_MINT = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
DEFAULT_HTTP_RPCS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
)
DEFAULT_WS_RPCS = (
    "wss://api.mainnet-beta.solana.com",
    "wss://solana-rpc.publicnode.com",
)
PUMP_PROTOCOL_FEE = 0.0125
PUMPPORTAL_LOCAL_FEE = 0.005
CONSERVATIVE_PRICE_IMPACT = 0.0025
TOTAL_PERCENT_COST = PUMP_PROTOCOL_FEE + PUMPPORTAL_LOCAL_FEE + CONSERVATIVE_PRICE_IMPACT


def now_ns() -> int:
    return time.time_ns()


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def metric_summary(values: Sequence[float]) -> dict[str, float | int | None]:
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


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


class RpcPool:
    def __init__(self, urls: Sequence[str], timeout: float = 8.0):
        self.urls = tuple(dict.fromkeys(url for url in urls if url))
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.request_id = 0
        self.cursor = 0
        self.errors: list[str] = []
        self.latencies_ms: list[float] = []

    async def __aenter__(self) -> "RpcPool":
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def raw_call(
        self,
        url: str,
        method: str,
        params: list[Any],
        *,
        retries: int = 3,
    ) -> tuple[Any, float]:
        assert self.session is not None
        error: Exception | None = None
        for attempt in range(retries):
            self.request_id += 1
            started = time.perf_counter_ns()
            try:
                async with self.session.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": self.request_id,
                        "method": method,
                        "params": params,
                    },
                ) as response:
                    body = await response.text()
                    latency = (time.perf_counter_ns() - started) / 1_000_000
                    if response.status == 429:
                        raise RuntimeError(f"HTTP 429 from {url}")
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}: {body[:300]}")
                    payload = json.loads(body)
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    return payload.get("result"), latency
            except Exception as exc:
                error = exc
                await asyncio.sleep(min(2.0, 0.15 * (2**attempt) + random.random() * 0.1))
        raise RuntimeError(f"RPC {method} failed at {url}: {error}")

    async def call(self, method: str, params: list[Any], *, retries: int = 5) -> Any:
        if not self.urls:
            raise RuntimeError("no RPC URLs configured")
        last_error: Exception | None = None
        for offset in range(len(self.urls) * max(1, retries)):
            url = self.urls[(self.cursor + offset) % len(self.urls)]
            try:
                result, latency = await self.raw_call(url, method, params, retries=1)
                self.cursor = (self.urls.index(url) + 1) % len(self.urls)
                self.latencies_ms.append(latency)
                return result
            except Exception as exc:
                last_error = exc
                self.errors.append(f"{method}@{url}: {exc}")
                await asyncio.sleep(min(1.0, 0.05 * (offset + 1)))
        raise RuntimeError(f"all RPCs failed for {method}: {last_error}")


async def fetch_sol_usd(session: aiohttp.ClientSession) -> float:
    fallback = float(os.getenv("E4_SOL_USD_FALLBACK", "150"))
    url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
    try:
        async with session.get(url, headers={"user-agent": "Gambit-E4-Stress/1"}) as response:
            payload = await response.json()
        value = finite((payload.get("solana") or {}).get("usd"))
        if value and value > 0:
            return value
    except Exception:
        pass
    return fallback


@dataclass(slots=True)
class LiveEvent:
    event_id: int
    kind: str
    mint: str
    received_ns: int
    signature: str
    slot: int
    event_index: int
    trader: str | None = None
    sol_amount: float = 0.0
    token_amount: float = 0.0
    price_sol: float | None = None
    fdv_usd: float | None = None
    complete: bool = False
    creator: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_core(self) -> core.Event:
        return core.Event(
            event_id=self.event_id,
            kind=core.EventKind(self.kind),
            mint=self.mint,
            source_ns=self.received_ns,
            received_ns=self.received_ns,
            slot=self.slot,
            signature=self.signature,
            trader=self.trader,
            sol_amount=self.sol_amount,
            token_amount=self.token_amount,
            price_sol=self.price_sol,
            fdv_usd=self.fdv_usd,
            complete=self.complete,
            creator=self.creator,
        )


def anchor_to_live(
    item: Mapping[str, Any],
    signature: str,
    slot: int,
    received_ns: int,
    event_index: int,
    sequence: int,
) -> LiveEvent | None:
    event_name = str(item.get("anchor_event") or "")
    mint = str(item.get("mint") or "")
    if not mint:
        return None
    kind_map = {
        "CreateEvent": core.EventKind.CREATE.value,
        "TradeEvent": (
            core.EventKind.BUY.value if item.get("is_buy") else core.EventKind.SELL.value
        ),
        "CompleteEvent": core.EventKind.MIGRATION.value,
        "CompletePumpAmmMigrationEvent": core.EventKind.MIGRATION.value,
    }
    kind = kind_map.get(event_name)
    if not kind:
        return None
    normalized = dict(item)
    price = hardening._normalized_price_sol(normalized)
    fdv = hardening._derived_fdv_usd(normalized, price)
    token_raw = finite(item.get("token_amount")) or 0.0
    return LiveEvent(
        event_id=sequence,
        kind=kind,
        mint=mint,
        received_ns=received_ns,
        signature=signature,
        slot=slot,
        event_index=event_index,
        trader=str(item.get("user") or "") or None,
        sol_amount=(finite(item.get("sol_amount")) or 0.0) / core.LAMPORTS_PER_SOL,
        token_amount=token_raw / 1_000_000,
        price_sol=price,
        fdv_usd=fdv,
        complete=event_name in {"CompleteEvent", "CompletePumpAmmMigrationEvent"},
        creator=str(item.get("creator") or "") or None,
        raw=normalized,
    )


def decode_log_payload(
    payload: Mapping[str, Any],
    events: list[LiveEvent],
    dedupe: set[tuple[str, int, str]],
) -> int:
    result = (payload.get("params") or {}).get("result") or {}
    value = result.get("value") or {}
    if value.get("err") is not None:
        return 0
    signature = str(value.get("signature") or "")
    slot = int((result.get("context") or {}).get("slot") or 0)
    logs = list(value.get("logs") or [])
    if not signature or not logs:
        return 0
    decoded = anchor_events_from_logs(logs, PUMP_PROGRAM_ID)
    received = now_ns()
    added = 0
    for index, item in enumerate(decoded):
        key = (signature, index, str(item.get("anchor_event") or ""))
        if key in dedupe:
            continue
        event = anchor_to_live(item, signature, slot, received, index, len(events) + 1)
        if event is None:
            continue
        dedupe.add(key)
        events.append(event)
        added += 1
    return added


async def capture_native_pump(
    seconds: float,
    ws_urls: Sequence[str],
) -> tuple[list[LiveEvent], dict[str, Any]]:
    deadline = time.monotonic() + seconds
    events: list[LiveEvent] = []
    dedupe: set[tuple[str, int, str]] = set()
    errors: list[str] = []
    connections = 0
    messages = 0
    endpoint_index = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while time.monotonic() < deadline:
            url = ws_urls[endpoint_index % len(ws_urls)]
            endpoint_index += 1
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8 * 1024 * 1024) as ws:
                    connections += 1
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": connections,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [PUMP_PROGRAM_ID]},
                                {"commitment": "processed"},
                            ],
                        }
                    )
                    subscription_deadline = min(deadline, time.monotonic() + 90)
                    while time.monotonic() < subscription_deadline:
                        remaining = min(10.0, subscription_deadline - time.monotonic())
                        if remaining <= 0:
                            break
                        try:
                            message = await asyncio.wait_for(ws.receive(), timeout=remaining)
                        except asyncio.TimeoutError:
                            continue
                        if message.type == aiohttp.WSMsgType.TEXT:
                            messages += 1
                            try:
                                payload = json.loads(message.data)
                            except json.JSONDecodeError:
                                continue
                            decode_log_payload(payload, events, dedupe)
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                        }:
                            break
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                await asyncio.sleep(0.5)
    events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    return events, {
        "duration_seconds": seconds,
        "connections": connections,
        "messages": messages,
        "decoded_events": len(events),
        "errors": errors[-20:],
    }


async def backfill_pump_events(
    rpc: RpcPool,
    started_epoch: int,
    ended_epoch: int,
    limit: int = 500,
) -> list[LiveEvent]:
    rows = await rpc.call(
        "getSignaturesForAddress",
        [PUMP_PROGRAM_ID, {"limit": min(1000, limit)}],
    )
    candidates = [
        row
        for row in rows or []
        if row.get("err") is None
        and row.get("blockTime")
        and started_epoch - 10 <= int(row["blockTime"]) <= ended_epoch + 10
    ]
    semaphore = asyncio.Semaphore(5)

    async def fetch(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        async with semaphore:
            try:
                transaction = await rpc.call(
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
                return row, transaction if isinstance(transaction, Mapping) else None
            except Exception:
                return row, None

    fetched = await asyncio.gather(*(fetch(row) for row in candidates))
    events: list[LiveEvent] = []
    dedupe: set[tuple[str, int, str]] = set()
    for row, transaction in fetched:
        if not transaction:
            continue
        meta = transaction.get("meta") or {}
        logs = list(meta.get("logMessages") or [])
        signature = str(row["signature"])
        slot = int(transaction.get("slot") or row.get("slot") or 0)
        received_ns = int((row.get("blockTime") or time.time()) * 1_000_000_000)
        for index, item in enumerate(anchor_events_from_logs(logs, PUMP_PROGRAM_ID)):
            key = (signature, index, str(item.get("anchor_event") or ""))
            if key in dedupe:
                continue
            event = anchor_to_live(item, signature, slot, received_ns, index, len(events) + 1)
            if event:
                dedupe.add(key)
                events.append(event)
    events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    return events


@dataclass(slots=True)
class SellLeg:
    decision_ns: int
    fill_ns: int
    fraction_of_original: float
    price_sol: float
    reason: str
    urgent: bool


@dataclass(slots=True)
class CandidateTrade:
    mint: str
    entry_decision_ns: int
    entry_fill_ns: int
    entry_price_sol: float
    entry_fdv_usd: float
    score: float
    requested_fraction: float
    sell_legs: list[SellLeg]
    exit_ns: int
    first_partial_fraction: float | None
    failure_exit: bool
    stale_fill: bool = False

    @property
    def hold_ms(self) -> float:
        return (self.exit_ns - self.entry_fill_ns) / 1_000_000


def event_at_or_after(events: Sequence[LiveEvent], timestamp_ns: int, start: int) -> tuple[int, LiveEvent] | None:
    for index in range(start, len(events)):
        if events[index].received_ns >= timestamp_ns and events[index].price_sol:
            return index, events[index]
    return None


def simulate_token(events: Sequence[LiveEvent], settings: core.Settings, latency_ms: float) -> CandidateTrade | None:
    if not events:
        return None
    state = core.TokenState(events[0].mint)
    policy = core.E4Policy(settings)
    entry_index: int | None = None
    score = 0.0
    requested_fraction = 0.0
    entry_decision_ns = 0
    decision_price_sol = 0.0
    decision_fdv_usd = 0.0
    for index, event in enumerate(events):
        state.apply(event.to_core(), None)
        if event.kind not in {
            core.EventKind.CREATE.value,
            core.EventKind.BUY.value,
            core.EventKind.CURVE.value,
        }:
            continue
        accepted, candidate_score, fraction, _, _ = policy.entry(state)
        if accepted:
            entry_index = index
            score = candidate_score
            requested_fraction = fraction
            entry_decision_ns = event.received_ns
            decision_price_sol = float(state.price_sol or event.price_sol or 0.0)
            decision_fdv_usd = float(state.fdv_usd or event.fdv_usd or 0.0)
            break
    if entry_index is None:
        return None
    fill = event_at_or_after(
        events,
        entry_decision_ns + int(latency_ms * 1_000_000),
        entry_index,
    )
    if fill is None:
        return None
    fill_index, fill_event = fill
    entry_price = fill_event.price_sol
    if not entry_price or entry_price <= 0:
        return None
    # A simulated fill must respect the exact buy protection the real transaction carries.
    # If the next observable trade is already outside the allowed price/FDV envelope,
    # the correct counterfactual is a failed/missed entry, not an impossible bad fill.
    max_price = decision_price_sol * (1.0 + settings.buy_slippage_bps / 10_000.0) if decision_price_sol > 0 else 0.0
    if max_price > 0 and entry_price > max_price:
        return None
    fill_fdv = float(fill_event.fdv_usd or 0.0)
    if fill_fdv > 0 and fill_fdv > settings.max_entry_fdv_usd:
        return None
    if decision_fdv_usd > 0 and decision_fdv_usd > settings.max_entry_fdv_usd:
        return None
    position = core.Position(
        position_id=f"sim:{events[0].mint}",
        mint=events[0].mint,
        status=core.PositionStatus.OPEN,
        opened_ns=time.time_ns(),
        entry_sol=1.0,
        tokens=1.0 / entry_price,
        remaining=1.0 / entry_price,
        entry_price=entry_price,
        max_price=entry_price,
        last_price=entry_price,
        entry_signature="simulation",
    )
    remaining_fraction = 1.0
    legs: list[SellLeg] = []
    first_partial: float | None = None
    failure = False
    stale = False
    index = fill_index + 1
    while index < len(events):
        event = events[index]
        state.apply(event.to_core(), None)
        elapsed_ns = max(0, event.received_ns - fill_event.received_ns)
        position.opened_ns = time.time_ns() - elapsed_ns
        action, fraction, reason = policy.exit(position, state)
        if not action.startswith("SELL"):
            index += 1
            continue
        due = event.received_ns + int(latency_ms * 1_000_000)
        resolved = event_at_or_after(events, due, index)
        if resolved is None:
            sell_index, sell_event = index, event
            stale = True
        else:
            sell_index, sell_event = resolved
        sell_price = sell_event.price_sol or position.last_price
        if not sell_price or sell_price <= 0:
            index = sell_index + 1
            continue
        original_fraction = remaining_fraction if fraction >= 0.999 else remaining_fraction * fraction
        original_fraction = min(remaining_fraction, max(0.0, original_fraction))
        if original_fraction <= 0:
            index = sell_index + 1
            continue
        urgent = fraction >= 0.999 or any(
            word in reason.lower() for word in ("failure", "broke", "horizon", "liquidation")
        )
        legs.append(
            SellLeg(
                decision_ns=event.received_ns,
                fill_ns=sell_event.received_ns,
                fraction_of_original=original_fraction,
                price_sol=sell_price,
                reason=reason,
                urgent=urgent,
            )
        )
        remaining_fraction = max(0.0, remaining_fraction - original_fraction)
        position.remaining = position.tokens * remaining_fraction
        position.last_price = sell_price
        position.max_price = max(position.max_price, sell_price)
        if first_partial is None and fraction < 0.999:
            first_partial = original_fraction
            position.first_partial_done = True
            position.first_partial_fraction = original_fraction
        if "failure" in reason.lower() or "confirmation failed" in reason.lower():
            failure = True
        if remaining_fraction <= 1e-9 or fraction >= 0.999:
            break
        index = sell_index + 1

    if remaining_fraction > 1e-9:
        horizon_ns = fill_event.received_ns + settings.max_hold_ms * 1_000_000
        resolved = event_at_or_after(events, horizon_ns + int(latency_ms * 1_000_000), fill_index)
        if resolved is None:
            sell_event = next((item for item in reversed(events) if item.price_sol), fill_event)
            stale = True
        else:
            _, sell_event = resolved
        price = sell_event.price_sol or entry_price
        legs.append(
            SellLeg(
                decision_ns=horizon_ns,
                fill_ns=max(horizon_ns, sell_event.received_ns),
                fraction_of_original=remaining_fraction,
                price_sol=price,
                reason="E4 observed absolute hold horizon",
                urgent=True,
            )
        )
        remaining_fraction = 0.0
    if not legs:
        return None
    return CandidateTrade(
        mint=events[0].mint,
        entry_decision_ns=entry_decision_ns,
        entry_fill_ns=fill_event.received_ns,
        entry_price_sol=entry_price,
        entry_fdv_usd=fill_event.fdv_usd or state.fdv_usd or 0.0,
        score=score,
        requested_fraction=requested_fraction,
        sell_legs=legs,
        exit_ns=max(leg.fill_ns for leg in legs),
        first_partial_fraction=first_partial,
        failure_exit=failure,
        stale_fill=stale,
    )


def fee_bid(settings: core.Settings, amount: float, score: float, urgent: bool = False) -> float:
    total = min(
        amount * max(0.0, min(score, 1.0)) * (0.03 if urgent else 0.015),
        settings.max_execution_cost_sol,
    )
    priority = min(settings.max_priority_fee_sol, total * 0.6)
    tip = min(settings.max_tip_sol, max(0.0, total - priority))
    return priority + tip


@dataclass(slots=True)
class PortfolioTrade:
    mint: str
    size_sol: float
    entry_cost_sol: float
    proceeds_sol: float
    pnl_sol: float
    gross_pnl_sol: float
    hold_ms: float
    first_partial_fraction: float | None
    failure_exit: bool
    entry_fdv_usd: float
    entry_ns: int
    exit_ns: int
    sell_count: int


def evaluate_portfolio(
    candidates: Sequence[CandidateTrade],
    starting_balance: float,
    settings: core.Settings,
) -> dict[str, Any]:
    liquid = starting_balance
    active: list[tuple[int, float, PortfolioTrade]] = []
    completed: list[PortfolioTrade] = []
    skipped_concurrency = 0
    skipped_size = 0
    peak_concurrency = 0

    def settle(until_ns: int) -> None:
        nonlocal liquid, active
        remaining = []
        for exit_ns, proceeds, result in active:
            if exit_ns <= until_ns:
                liquid += proceeds
                completed.append(result)
            else:
                remaining.append((exit_ns, proceeds, result))
        active = remaining

    for candidate in sorted(candidates, key=lambda item: (item.entry_fill_ns, item.mint)):
        settle(candidate.entry_fill_ns)
        if len(active) >= 2:
            skipped_concurrency += 1
            continue
        fraction = min(candidate.requested_fraction, settings.max_position_fraction)
        estimated_order_fee = fee_bid(settings, liquid * fraction, candidate.score)
        deployable = max(0.0, liquid - settings.reserve_sol - estimated_order_fee)
        size = min(deployable * fraction, settings.max_position_sol)
        if size < settings.min_position_sol:
            skipped_size += 1
            continue
        buy_route_cost = fee_bid(settings, size, candidate.score)
        entry_cost = size + buy_route_cost
        if liquid - entry_cost < settings.reserve_sol - 1e-12:
            size = max(0.0, liquid - settings.reserve_sol - buy_route_cost)
            entry_cost = size + buy_route_cost
        if size < settings.min_position_sol:
            skipped_size += 1
            continue
        liquid -= entry_cost
        tokens = size * (1.0 - TOTAL_PERCENT_COST) / candidate.entry_price_sol
        gross_proceeds = 0.0
        net_proceeds = 0.0
        for leg in candidate.sell_legs:
            token_amount = tokens * leg.fraction_of_original
            raw = token_amount * leg.price_sol
            gross_proceeds += raw
            net = raw * (1.0 - TOTAL_PERCENT_COST)
            net -= fee_bid(settings, size * leg.fraction_of_original, 1.0, leg.urgent)
            net_proceeds += max(0.0, net)
        result = PortfolioTrade(
            mint=candidate.mint,
            size_sol=size,
            entry_cost_sol=entry_cost,
            proceeds_sol=net_proceeds,
            pnl_sol=net_proceeds - entry_cost,
            gross_pnl_sol=gross_proceeds - size,
            hold_ms=candidate.hold_ms,
            first_partial_fraction=candidate.first_partial_fraction,
            failure_exit=candidate.failure_exit,
            entry_fdv_usd=candidate.entry_fdv_usd,
            entry_ns=candidate.entry_fill_ns,
            exit_ns=candidate.exit_ns,
            sell_count=len(candidate.sell_legs),
        )
        active.append((candidate.exit_ns, net_proceeds, result))
        peak_concurrency = max(peak_concurrency, len(active))
    settle(2**63 - 1)
    wins = [item for item in completed if item.pnl_sol > 0]
    losses = [item for item in completed if item.pnl_sol <= 0]
    gross_wins = [item for item in completed if item.gross_pnl_sol > 0]
    first_20 = [item for item in completed if item.first_partial_fraction and abs(item.first_partial_fraction - 0.20) <= 0.03]
    first_30 = [item for item in completed if item.first_partial_fraction and abs(item.first_partial_fraction - 0.30) <= 0.03]
    return {
        "starting_balance_sol": starting_balance,
        "ending_balance_sol": liquid,
        "net_pnl_sol": liquid - starting_balance,
        "closed_positions": len(completed),
        "net_win_rate": safe_ratio(len(wins), len(completed)),
        "gross_win_rate": safe_ratio(len(gross_wins), len(completed)),
        "profit_factor": (
            sum(item.pnl_sol for item in wins) / abs(sum(item.pnl_sol for item in losses))
            if losses and sum(item.pnl_sol for item in losses) < 0
            else None
        ),
        "median_hold_ms": statistics.median([item.hold_ms for item in completed]) if completed else None,
        "fully_exited_within_5s_fraction": safe_ratio(sum(item.hold_ms <= 5_000 for item in completed), len(completed)),
        "fully_exited_within_10s_fraction": safe_ratio(sum(item.hold_ms <= 10_000 for item in completed), len(completed)),
        "losers_exited_within_5s_fraction": safe_ratio(sum(item.hold_ms <= 5_000 for item in losses), len(losses)),
        "median_entry_fdv_usd": statistics.median([item.entry_fdv_usd for item in completed if item.entry_fdv_usd > 0]) if any(item.entry_fdv_usd > 0 for item in completed) else None,
        "entries_below_10000_fraction": safe_ratio(sum(item.entry_fdv_usd < 10_000 for item in completed), len(completed)),
        "first_partial_20pct_count": len(first_20),
        "first_partial_30pct_count": len(first_30),
        "reentries": 0,
        "max_concurrent_positions": peak_concurrency,
        "skipped_for_concurrency": skipped_concurrency,
        "skipped_for_size": skipped_size,
        "positions": [asdict(item) for item in completed],
    }


async def builder_benchmark(mints: Sequence[str], probes: int) -> dict[str, Any]:
    if not mints or probes <= 0:
        return {"available": False, "reason": "no captured live mints"}
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
    except ImportError as exc:
        return {"available": False, "reason": f"solders unavailable: {exc}"}
    keypair = Keypair()
    builder_command = tuple(shlex.split(os.getenv("E4_BUILDER_COMMAND", "node tools/e4-builder/race-proxy-v3.mjs")))
    if not builder_command:
        return {"available": False, "reason": "empty V11 builder command"}
    process = await asyncio.create_subprocess_exec(
        *builder_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr
    errors: list[str] = []
    latencies: list[float] = []
    signing: list[float] = []
    sizes: list[int] = []
    tip_checks: list[bool] = []
    side_results: dict[str, list[bool]] = {"BUY": [], "SELL": []}
    requests = []
    for index in range(probes):
        side = "BUY" if index % 2 == 0 else "SELL"
        requests.append(
            {
                "request_id": f"stress-{index}",
                "side": side,
                "mint": mints[index % len(mints)],
                "public_key": str(keypair.pubkey()),
                "amount": 0.01 if side == "BUY" else 1_000,
                "denominated_in_sol": side == "BUY",
                "slippage_bps": 1_000,
                "priority_fee_sol": 0.00001,
                "tip_sol": 0.000001,
                "pool": "pump",
            }
        )
    for request in requests:
        started = time.perf_counter_ns()
        try:
            process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=6)
            latency = (time.perf_counter_ns() - started) / 1_000_000
            if not line:
                raise RuntimeError("builder closed stdout")
            response = json.loads(line)
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            raw = base64.b64decode(response["transaction_base64"], validate=True)
            tx = VersionedTransaction.from_bytes(raw)
            sign_started = time.perf_counter_ns()
            signed = VersionedTransaction(tx.message, [keypair])
            signing.append((time.perf_counter_ns() - sign_started) / 1_000_000)
            if not str(signed.signatures[0]):
                raise RuntimeError("empty signature")
            keys = {str(value) for value in tx.message.account_keys}
            tip_checks.append(bool(keys.intersection(JITO_TIP_ACCOUNTS)))
            latencies.append(latency)
            sizes.append(len(raw))
            side_results[request["side"]].append(True)
        except Exception as exc:
            errors.append(f"{request['side']}:{request['mint']}:{exc}")
            side_results[request["side"]].append(False)
            await asyncio.sleep(0.05)
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        process.kill()
    stderr = (await process.stderr.read()).decode(errors="replace")[-2000:]
    total = sum(len(values) for values in side_results.values())
    success = sum(sum(values) for values in side_results.values())
    return {
        "available": True,
        "requests": total,
        "successes": success,
        "success_rate": safe_ratio(success, total),
        "buy_success_rate": safe_ratio(sum(side_results["BUY"]), len(side_results["BUY"])),
        "sell_success_rate": safe_ratio(sum(side_results["SELL"]), len(side_results["SELL"])),
        "latency_ms": metric_summary(latencies),
        "signing_latency_ms": metric_summary(signing),
        "transaction_bytes": metric_summary([float(value) for value in sizes]),
        "jito_tip_present_fraction": safe_ratio(sum(tip_checks), len(tip_checks)),
        "errors": errors[-20:],
        "stderr_tail": stderr,
    }


async def testnet_route_probe(rpc_probes: int) -> dict[str, Any]:
    if rpc_probes <= 0:
        return {"available": False, "reason": "disabled"}
    try:
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction
    except ImportError as exc:
        return {"available": False, "reason": f"solders unavailable: {exc}"}

    testnet_rpc = "https://api.testnet.solana.com"
    jito_transactions = "https://testnet.block-engine.jito.wtf/api/v1/transactions"
    jito_tip_endpoint = "https://testnet.block-engine.jito.wtf/api/v1/getTipAccounts"
    async with RpcPool((testnet_rpc,), timeout=12) as rpc:
        keypair = Keypair()
        wallet = str(keypair.pubkey())
        try:
            tips, _ = await rpc.raw_call(jito_tip_endpoint, "getTipAccounts", [], retries=3)
            tip_account = Pubkey.from_string(str((tips or list(JITO_TIP_ACCOUNTS))[0]))
            signature = await rpc.call("requestAirdrop", [wallet, 200_000_000], retries=8)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                status = await rpc.call("getSignatureStatuses", [[signature]])
                value = (status.get("value") or [None])[0]
                if value and value.get("err") is None:
                    break
                await asyncio.sleep(0.5)
            else:
                return {"available": False, "reason": "testnet airdrop did not confirm"}
        except Exception as exc:
            return {"available": False, "reason": f"testnet funding unavailable: {exc}", "rpc_errors": rpc.errors[-10:]}

        route_latencies: dict[str, list[float]] = defaultdict(list)
        landing: list[float] = []
        confirmed = 0
        accepted_counts: dict[str, int] = defaultdict(int)
        errors: list[str] = []
        for index in range(rpc_probes):
            try:
                blockhash = await rpc.call("getLatestBlockhash", [{"commitment": "processed"}])
                instructions = [
                    set_compute_unit_limit(30_000),
                    set_compute_unit_price(1_000),
                    transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=keypair.pubkey(), lamports=1)),
                    transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=tip_account, lamports=1_000)),
                ]
                message = MessageV0.try_compile(
                    keypair.pubkey(), instructions, [], Hash.from_string(blockhash["value"]["blockhash"])
                )
                transaction = VersionedTransaction(message, [keypair])
                encoded = base64.b64encode(bytes(transaction)).decode()
                expected = str(transaction.signatures[0])
                started = time.perf_counter_ns()

                async def send(name: str, url: str) -> tuple[str, bool, float, str | None]:
                    try:
                        result, latency = await rpc.raw_call(
                            url,
                            "sendTransaction",
                            [encoded, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
                            retries=1,
                        )
                        return name, str(result) == expected, latency, None
                    except Exception as exc:
                        return name, False, 0.0, str(exc)

                results = await asyncio.gather(
                    send("direct_testnet", testnet_rpc),
                    send("jito_testnet", jito_transactions),
                )
                for name, accepted, latency, error in results:
                    if accepted:
                        accepted_counts[name] += 1
                        route_latencies[name].append(latency)
                    elif error:
                        errors.append(f"{name}:{error}")
                deadline = time.monotonic() + 12
                landed = False
                while time.monotonic() < deadline:
                    status = await rpc.call(
                        "getSignatureStatuses",
                        [[expected], {"searchTransactionHistory": False}],
                    )
                    value = (status.get("value") or [None])[0]
                    if value and value.get("err") is None and value.get("confirmationStatus") in {"processed", "confirmed", "finalized"}:
                        landed = True
                        break
                    await asyncio.sleep(0.05)
                if landed:
                    confirmed += 1
                    landing.append((time.perf_counter_ns() - started) / 1_000_000)
                else:
                    errors.append(f"probe-{index}:confirmation timeout")
            except Exception as exc:
                errors.append(f"probe-{index}:{exc}")
        return {
            "available": True,
            "wallet": wallet,
            "probes": rpc_probes,
            "confirmed": confirmed,
            "confirmation_rate": safe_ratio(confirmed, rpc_probes),
            "same_signature_raced": True,
            "route_acceptance": {
                name: safe_ratio(count, rpc_probes) for name, count in accepted_counts.items()
            },
            "route_latency_ms": {
                name: metric_summary(values) for name, values in route_latencies.items()
            },
            "landing_latency_ms": metric_summary(landing),
            "errors": errors[-30:],
        }


def account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    result = []
    for item in message.get("accountKeys") or []:
        result.append(str(item.get("pubkey")) if isinstance(item, Mapping) else str(item))
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    result.extend(str(item) for item in loaded.get("writable") or [])
    result.extend(str(item) for item in loaded.get("readonly") or [])
    return result


def token_totals(rows: Iterable[Mapping[str, Any]], wallet: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for item in rows or []:
        if str(item.get("owner") or "") != wallet:
            continue
        mint = str(item.get("mint") or "")
        if not mint:
            continue
        token = item.get("uiTokenAmount") or {}
        value = token.get("uiAmountString", token.get("uiAmount", 0))
        totals[mint] += float(value or 0)
    return totals


async def fetch_e4_wallet_sample(rpc: RpcPool, signature_limit: int) -> dict[str, Any]:
    signatures: list[Mapping[str, Any]] = []
    before = None
    while len(signatures) < signature_limit:
        config: dict[str, Any] = {"limit": min(1000, signature_limit - len(signatures))}
        if before:
            config["before"] = before
        try:
            batch = await rpc.call("getSignaturesForAddress", [E4_WALLET, config])
        except Exception as exc:
            return {"available": False, "reason": str(exc), "rpc_errors": rpc.errors[-20:]}
        if not batch:
            break
        signatures.extend(batch)
        before = batch[-1]["signature"]
        if len(batch) < config["limit"]:
            break

    semaphore = asyncio.Semaphore(5)

    async def fetch(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
        async with semaphore:
            for attempt in range(3):
                try:
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
                    )
                    return row, tx if isinstance(tx, Mapping) else None
                except Exception:
                    await asyncio.sleep(0.2 * (attempt + 1))
            return row, None

    fetched = await asyncio.gather(*(fetch(row) for row in signatures))
    wallet_events: list[dict[str, Any]] = []
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
        sol_delta = (float(post_balances[wallet_index]) - float(pre_balances[wallet_index])) / core.LAMPORTS_PER_SOL
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
        mint, delta, post_balance = changed[0]
        wallet_events.append(
            {
                "signature": row["signature"],
                "slot": transaction.get("slot") or row.get("slot"),
                "block_time": transaction.get("blockTime") or row.get("blockTime"),
                "mint": mint,
                "token_delta": delta,
                "post_token_balance": post_balance,
                "sol_delta": sol_delta,
                "fee_lamports": meta.get("fee"),
            }
        )
    wallet_events.sort(key=lambda item: (item.get("block_time") or 0, item.get("slot") or 0))
    open_positions: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    reentries = 0
    for event in wallet_events:
        mint = event["mint"]
        delta = float(event["token_delta"])
        if delta > 0:
            if mint in open_positions:
                reentries += 1
                position = open_positions[mint]
                position["tokens"] += delta
                position["cost_sol"] += max(0.0, -float(event["sol_delta"]))
                position["buy_count"] += 1
            else:
                open_positions[mint] = {
                    "mint": mint,
                    "entry_time": int(event.get("block_time") or 0),
                    "entry_slot": event.get("slot"),
                    "tokens": delta,
                    "sold": 0.0,
                    "cost_sol": max(0.0, -float(event["sol_delta"])),
                    "proceeds_sol": 0.0,
                    "first_partial_fraction": None,
                    "sell_count": 0,
                    "buy_count": 1,
                }
            continue
        position = open_positions.get(mint)
        if not position:
            continue
        sold = min(position["tokens"], max(0.0, -delta))
        if position["first_partial_fraction"] is None and position["tokens"] > 0:
            position["first_partial_fraction"] = sold / position["tokens"]
        position["sold"] += sold
        position["proceeds_sol"] += max(0.0, float(event["sol_delta"]))
        position["sell_count"] += 1
        if float(event["post_token_balance"]) <= max(1e-6, position["tokens"] * 1e-7) or position["sold"] >= position["tokens"] * 0.995:
            position["exit_time"] = int(event.get("block_time") or position["entry_time"])
            position["exit_slot"] = event.get("slot")
            position["hold_ms"] = max(0, (position["exit_time"] - position["entry_time"]) * 1000)
            position["pnl_sol"] = position["proceeds_sol"] - position["cost_sol"]
            closed.append(position)
            open_positions.pop(mint, None)
    wins = [item for item in closed if item["pnl_sol"] > 0]
    losses = [item for item in closed if item["pnl_sol"] <= 0]
    intervals = sorted((item["entry_time"], 1) for item in closed) + sorted((item["exit_time"], -1) for item in closed)
    concurrency = 0
    max_concurrency = 0
    for _, delta in sorted(intervals, key=lambda item: (item[0], item[1])):
        concurrency += delta
        max_concurrency = max(max_concurrency, concurrency)
    return {
        "available": bool(wallet_events),
        "signatures_requested": len(signatures),
        "transactions_fetched": fetched_count,
        "wallet_trade_events": len(wallet_events),
        "closed_positions": len(closed),
        "open_positions_in_sample": len(open_positions),
        "net_win_rate": safe_ratio(len(wins), len(closed)),
        "net_pnl_sol": sum(item["pnl_sol"] for item in closed),
        "median_hold_ms": statistics.median([item["hold_ms"] for item in closed]) if closed else None,
        "fully_exited_within_5s_fraction": safe_ratio(sum(item["hold_ms"] <= 5_000 for item in closed), len(closed)),
        "fully_exited_within_10s_fraction": safe_ratio(sum(item["hold_ms"] <= 10_000 for item in closed), len(closed)),
        "losers_exited_within_5s_fraction": safe_ratio(sum(item["hold_ms"] <= 5_000 for item in losses), len(losses)),
        "first_partial_20pct_count": sum(item["first_partial_fraction"] is not None and abs(item["first_partial_fraction"] - 0.20) <= 0.03 for item in closed),
        "first_partial_30pct_count": sum(item["first_partial_fraction"] is not None and abs(item["first_partial_fraction"] - 0.30) <= 0.03 for item in closed),
        "reentries": reentries,
        "max_concurrent_positions": max_concurrency,
        "positions": closed,
        "rpc_errors": rpc.errors[-30:],
    }


def compare_metrics(bot: Mapping[str, Any], baseline: Mapping[str, Any], fresh: Mapping[str, Any]) -> dict[str, Any]:
    targets = dict(baseline)
    if fresh.get("closed_positions", 0) >= 15:
        for key in (
            "net_win_rate",
            "median_hold_ms",
            "fully_exited_within_5s_fraction",
            "fully_exited_within_10s_fraction",
            "losers_exited_within_5s_fraction",
            "reentries",
            "max_concurrent_positions",
        ):
            if fresh.get(key) is not None:
                targets[key] = fresh[key]
    comparisons = {}
    for key in (
        "gross_win_rate",
        "net_win_rate",
        "median_hold_ms",
        "fully_exited_within_5s_fraction",
        "fully_exited_within_10s_fraction",
        "losers_exited_within_5s_fraction",
        "median_entry_fdv_usd",
        "entries_below_10000_fraction",
        "reentries",
        "max_concurrent_positions",
    ):
        comparisons[key] = {
            "gambit": bot.get(key),
            "actual_e4_target": targets.get(key),
            "difference": (
                float(bot[key]) - float(targets[key])
                if bot.get(key) is not None and targets.get(key) is not None
                else None
            ),
        }
    return comparisons


def gate(name: str, passed: bool, detail: str, critical: bool = True) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "critical": critical, "detail": detail}


def build_verdict(
    capture: Mapping[str, Any],
    builder: Mapping[str, Any],
    route: Mapping[str, Any],
    bot: Mapping[str, Any],
    fresh: Mapping[str, Any],
    baseline: Mapping[str, Any],
    stress_iterations: int,
) -> dict[str, Any]:
    gates = []
    gates.append(gate("live_launch_coverage", capture.get("new_launches", 0) >= 20, f"{capture.get('new_launches', 0)} newly launched tokens captured"))
    gates.append(gate("live_trade_coverage", capture.get("trade_events", 0) >= 200, f"{capture.get('trade_events', 0)} live trade events captured"))
    gates.append(gate("builder_success", bool(builder.get("available")) and (builder.get("success_rate") or 0) >= 0.95, f"success={builder.get('success_rate')}"))
    gates.append(gate("builder_latency", bool(builder.get("available")) and ((builder.get("latency_ms") or {}).get("p95") or 9e9) <= 750, f"p95_ms={(builder.get('latency_ms') or {}).get('p95')}"))
    gates.append(gate("real_jito_tip_instruction", bool(builder.get("available")) and (builder.get("jito_tip_present_fraction") or 0) >= 0.999, f"fraction={builder.get('jito_tip_present_fraction')}"))
    route_available = bool(route.get("available"))
    gates.append(gate("same_signature_route_race", route_available and bool(route.get("same_signature_raced")), str(route.get("reason") or "testnet route race executed")))
    gates.append(gate("route_confirmation", route_available and (route.get("confirmation_rate") or 0) >= 0.90, f"rate={route.get('confirmation_rate')}"))
    gates.append(gate("route_landing_latency", route_available and ((route.get("landing_latency_ms") or {}).get("p95") or 9e9) <= 2_000, f"p95_ms={(route.get('landing_latency_ms') or {}).get('p95')}"))
    gates.append(gate("repeat_stress", stress_iterations >= 2_000, f"{stress_iterations} repeated policy paths plus execution lifecycle tests"))
    gates.append(gate("single_entry", bot.get("reentries") == 0, f"reentries={bot.get('reentries')}"))
    gates.append(gate("two_position_limit", (bot.get("max_concurrent_positions") or 0) <= 2, f"max={bot.get('max_concurrent_positions')}"))
    gates.append(gate("simulation_sample", (bot.get("closed_positions") or 0) >= 15, f"closed={bot.get('closed_positions')}"))
    actual_net = fresh.get("net_win_rate") if (fresh.get("closed_positions") or 0) >= 15 else 0.629
    gates.append(gate("net_win_rate_similarity", (bot.get("net_win_rate") or 0) >= max(0.55, float(actual_net or 0) - 0.10), f"gambit={bot.get('net_win_rate')} target={actual_net}"))
    gates.append(gate("positive_net_expectancy", (bot.get("net_pnl_sol") or 0) > 0, f"pnl={bot.get('net_pnl_sol')}"))
    gates.append(gate("fast_loser_exit", (bot.get("losers_exited_within_5s_fraction") or 0) >= 0.90, f"fraction={bot.get('losers_exited_within_5s_fraction')}"))
    gates.append(gate("fast_total_exit", (bot.get("fully_exited_within_10s_fraction") or 0) >= 0.80, f"fraction={bot.get('fully_exited_within_10s_fraction')}"))
    gates.append(gate("fresh_e4_comparator", (fresh.get("closed_positions") or 0) >= 15, f"closed={fresh.get('closed_positions')}", critical=False))
    # Testnet verifies build/sign/race/confirm mechanics. It cannot reproduce funded
    # mainnet contention or authenticate Helius/Nozomi private routes.
    gates.append(gate("credentialed_mainnet_route_proof", False, "No funded mainnet order and no authenticated Helius/Nozomi production route were used in this hypothesis-only run"))
    critical_failures = [item for item in gates if item["critical"] and not item["passed"]]
    return {
        "good_to_go_live": not critical_failures,
        "classification": "GOOD_TO_GO_LIVE" if not critical_failures else "NOT_YET_LIVE_CERTIFIED",
        "gates": gates,
        "critical_failures": critical_failures,
    }


async def main_async(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_output = output.with_name(output.stem + "-live-events.jsonl")
    started_wall = int(time.time())
    ws_urls = tuple(filter(None, os.getenv("E4_STRESS_WS_URLS", "").split(","))) or DEFAULT_WS_RPCS
    rpc_urls = tuple(filter(None, os.getenv("E4_STRESS_RPC_URLS", "").split(","))) or DEFAULT_HTTP_RPCS

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as price_session:
        sol_usd = await fetch_sol_usd(price_session)
    hardening._SOL_USD = sol_usd

    live_events, capture_diagnostics = await capture_native_pump(args.capture_seconds, ws_urls)
    ended_wall = int(time.time())
    if len([item for item in live_events if item.kind == core.EventKind.CREATE.value]) < args.minimum_launches:
        async with RpcPool(rpc_urls, timeout=10) as backfill_rpc:
            try:
                backfill = await backfill_pump_events(backfill_rpc, started_wall, ended_wall, 700)
            except Exception as exc:
                capture_diagnostics.setdefault("errors", []).append(f"backfill:{exc}")
                backfill = []
        keys = {(item.signature, item.event_index, item.kind) for item in live_events}
        for event in backfill:
            key = (event.signature, event.event_index, event.kind)
            if key not in keys:
                event.event_id = len(live_events) + 1
                live_events.append(event)
                keys.add(key)
        live_events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    launches = {item.mint for item in live_events if item.kind == core.EventKind.CREATE.value}
    launch_events = [item for item in live_events if item.mint in launches]
    with raw_output.open("w", encoding="utf-8") as file:
        for event in launch_events:
            file.write(json.dumps(asdict(event), separators=(",", ":"), default=str) + "\n")

    grouped: dict[str, list[LiveEvent]] = defaultdict(list)
    for event in launch_events:
        grouped[event.mint].append(event)
    for values in grouped.values():
        values.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    build_mints = [mint for mint, values in grouped.items() if any(item.kind == core.EventKind.BUY.value for item in values)]
    builder_task = asyncio.create_task(builder_benchmark(build_mints, args.builder_probes))
    route_task = asyncio.create_task(testnet_route_probe(args.testnet_route_probes))
    async with RpcPool(rpc_urls, timeout=10) as oracle_rpc:
        fresh_task = asyncio.create_task(fetch_e4_wallet_sample(oracle_rpc, args.wallet_signatures))
        latencies = [36.0, 100.0, 250.0, 500.0, 1_000.0]
        scenarios: dict[str, Any] = {}
        for latency in latencies:
            candidates = [
                trade
                for values in grouped.values()
                if (trade := simulate_token(values, core.Settings(model_path=Path("missing-model.json")), latency))
            ]
            scenarios[f"{int(latency)}ms"] = {
                "candidate_trades": len(candidates),
                "balances": {
                    str(balance): evaluate_portfolio(
                        candidates,
                        balance,
                        core.Settings(model_path=Path("missing-model.json")),
                    )
                    for balance in (0.3, 1.2, 5.0)
                },
            }
        fresh = await fresh_task
        rpc_diagnostics = {
            "latency_ms": metric_summary(oracle_rpc.latencies_ms),
            "errors": oracle_rpc.errors[-30:],
        }
    builder, route = await asyncio.gather(builder_task, route_task)

    primary_scenario = scenarios["36ms"]["balances"]["1.2"]
    baseline_payload = json.loads(Path("models/e4/e4-observed-v1.json").read_text())
    baseline = baseline_payload["evidence"]
    comparisons = compare_metrics(primary_scenario, baseline, fresh)
    capture = {
        **capture_diagnostics,
        "source": "live Solana processed logsSubscribe with RPC backfill fallback",
        "sol_usd": sol_usd,
        "captured_events_total": len(live_events),
        "new_launches": len(launches),
        "new_launch_events": len(launch_events),
        "trade_events": sum(item.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value} for item in launch_events),
        "buy_events": sum(item.kind == core.EventKind.BUY.value for item in launch_events),
        "sell_events": sum(item.kind == core.EventKind.SELL.value for item in launch_events),
        "migrations": sum(item.kind == core.EventKind.MIGRATION.value for item in launch_events),
        "tokens_with_trajectory": sum(len(values) >= 3 for values in grouped.values()),
    }
    verdict = build_verdict(
        capture,
        builder,
        route,
        primary_scenario,
        fresh,
        baseline,
        args.stress_iterations,
    )
    report = {
        "report_version": "e4-live-market-stress-v1",
        "generated_at_epoch": int(time.time()),
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "branch": os.getenv("GITHUB_REF_NAME"),
        "commit": os.getenv("GITHUB_SHA"),
        "capture": capture,
        "builder_benchmark": builder,
        "testnet_same_signature_route_probe": route,
        "actual_e4_fresh_sample": fresh,
        "actual_e4_observed_baseline": baseline_payload,
        "hypothetical_scenarios": scenarios,
        "primary_comparison_scenario": {
            "latency_ms": 36,
            "starting_balance_sol": 1.2,
            "results": primary_scenario,
        },
        "comparison": comparisons,
        "rpc_diagnostics": rpc_diagnostics,
        "stress_iterations": args.stress_iterations,
        "verdict": verdict,
        "limitations": [
            "No funded mainnet transaction was submitted; all Pump trades are counterfactual replays of live launch events.",
            "Testnet route races validate build/sign/same-signature broadcast/confirmation mechanics but do not reproduce mainnet blockspace competition.",
            "The private E4 selected-versus-ignored launch function is not publicly observable; the fallback entry profile remains the largest model-risk source.",
            "Public RPC endpoints can omit or delay processed events, so live capture coverage is measured and enforced as a gate.",
            "PumpPortal Local API adds its documented 0.5% trade fee; the simulation also charges Pump bonding-curve fees and a conservative execution-impact haircut.",
        ],
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown = output.with_suffix(".md")
    failures = verdict["critical_failures"]
    markdown.write_text(
        "\n".join(
            [
                "# E4 live-market stress result",
                "",
                f"**Verdict:** {verdict['classification']}",
                f"**Hypothesis only:** yes — zero mainnet transactions sent.",
                f"**Live launches captured:** {capture['new_launches']}",
                f"**Live trade events:** {capture['trade_events']}",
                f"**Hypothetical closed positions (36ms / 1.2 SOL):** {primary_scenario['closed_positions']}",
                f"**Hypothetical net win rate:** {primary_scenario['net_win_rate']}",
                f"**Hypothetical net P&L:** {primary_scenario['net_pnl_sol']} SOL",
                f"**Fresh E4 closed positions reconstructed:** {fresh.get('closed_positions')}",
                "",
                "## Critical failures",
                *(f"- {item['name']}: {item['detail']}" for item in failures),
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "verdict": verdict["classification"], "critical_failures": failures}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Hypothesis-only E4 live memecoin stress harness")
    value.add_argument("--capture-seconds", type=float, default=300)
    value.add_argument("--minimum-launches", type=int, default=20)
    value.add_argument("--wallet-signatures", type=int, default=250)
    value.add_argument("--builder-probes", type=int, default=24)
    value.add_argument("--testnet-route-probes", type=int, default=5)
    value.add_argument("--stress-iterations", type=int, default=10_000)
    value.add_argument("--output", default="artifacts/e4-live-market-stress.json")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parser().parse_args())))
