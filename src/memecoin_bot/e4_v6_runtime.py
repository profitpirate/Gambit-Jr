from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from typing import Any, Mapping

from . import e4_v6_state as s

core = s.core
final = s.final

_previous_init = core.Engine.__init__


def engine_init(self: Any, settings: Any) -> None:
    settings.max_position_fraction = min(settings.max_position_fraction, float(os.getenv("E4_OBSERVED_MAX_POSITION_FRACTION", str(s.MAX_POSITION_FRACTION))))
    settings.max_hold_ms = max(settings.max_hold_ms, int(os.getenv("E4_RUNNER_EMERGENCY_HORIZON_MS", str(s.RUNNER_EMERGENCY_HORIZON_MS))))
    s.load_identity_cache(settings.operational_db)
    _previous_init(self, settings)
    with self.store.conn:
        self.store.conn.execute(
            "CREATE TABLE IF NOT EXISTS e4_position_policy_meta("
            "mint TEXT PRIMARY KEY,score REAL NOT NULL,tier TEXT NOT NULL,target_fraction REAL NOT NULL,"
            "first_partial_fraction REAL NOT NULL,family TEXT NOT NULL,updated_ns INTEGER NOT NULL)"
        )
    for row in self.store.conn.execute("SELECT * FROM e4_position_policy_meta"):
        s.POLICY_BY_MINT[str(row["mint"])] = {
            "score": float(row["score"]), "tier": str(row["tier"]),
            "target_fraction": float(row["target_fraction"]),
            "first_partial_fraction": float(row["first_partial_fraction"]),
            "family": str(row["family"]), "decided_ns": int(row["updated_ns"]),
        }


core.Engine.__init__ = engine_init

# E4 spends aggressively to win entries but exits through very cheap fixed-tip
# transactions. Keep the decision task-local so two simultaneous positions do
# not contaminate each other's fee policy.
_fee_side: contextvars.ContextVar[str] = contextvars.ContextVar("e4_fee_side", default="BUY")
_previous_fee_bid = core.Engine.fee_bid


def fee_bid(self: Any, amount: float, score: float, urgent: bool = False) -> tuple[float, float]:
    side = _fee_side.get()
    if side == "SELL":
        # Observed exits: Helius/Jito/AsTZ commonly total ~0.000565 SOL;
        # Nozomi uses the higher ~0.001 min-tip family. Urgent liquidation gets
        # the latter without inheriting E4's extremely expensive buy bid.
        priority = 0.00050 if urgent else 0.00030
        tip = 0.00100 if urgent else 0.00020
        return min(self.settings.max_priority_fee_sol, priority), min(self.settings.max_tip_sol, tip)
    if side == "SWEEP":
        return min(self.settings.max_priority_fee_sol, 0.00020), min(self.settings.max_tip_sol, 0.00020)
    confidence = max(0.0, min(float(score), 1.0))
    total = min(
        self.settings.max_execution_cost_sol,
        max(0.0, float(amount)) * (0.035 + 0.030 * confidence),
    )
    priority = min(self.settings.max_priority_fee_sol, total * 0.55)
    tip = min(self.settings.max_tip_sol, max(0.0, total - priority))
    return priority, tip


core.Engine.fee_bid = fee_bid

_previous_execute_buy = core.Engine.execute_buy


async def execute_buy(self: Any, state: Any, score: float, fraction: float, reason: str) -> None:
    policy = s.POLICY_BY_MINT.get(state.mint)
    if policy is None:
        tier, target = s.size_tier(score)
        policy = {"score": score, "tier": tier, "target_fraction": target,
                  "first_partial_fraction": 0.20 if tier in s.HIGH_CONVICTION else 0.30,
                  "family": "RECOVERED_SCORE_TIER", "decided_ns": time.time_ns()}
        s.POLICY_BY_MINT[state.mint] = policy
    with self.store.conn:
        self.store.conn.execute(
            "INSERT INTO e4_position_policy_meta VALUES(?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET "
            "score=excluded.score,tier=excluded.tier,target_fraction=excluded.target_fraction,"
            "first_partial_fraction=excluded.first_partial_fraction,family=excluded.family,updated_ns=excluded.updated_ns",
            (state.mint, float(policy["score"]), str(policy["tier"]), float(policy["target_fraction"]),
             float(policy["first_partial_fraction"]), str(policy["family"]), int(policy["decided_ns"])),
        )
    token = _fee_side.set("BUY")
    try:
        await _previous_execute_buy(self, state, score, min(float(policy["target_fraction"]), self.settings.max_position_fraction), reason)
    finally:
        _fee_side.reset(token)


core.Engine.execute_buy = execute_buy

_previous_execute_sell = core.Engine.execute_sell


async def execute_sell(self: Any, position: Any, fraction: float, reason: str) -> None:
    token = _fee_side.set("SELL")
    try:
        await _previous_execute_sell(self, position, fraction, reason)
    finally:
        _fee_side.reset(token)


core.Engine.execute_sell = execute_sell

_previous_execute = core.Engine.execute


async def execute(self: Any, request_id: str, request: Mapping[str, Any]):
    enriched = dict(request)
    mint = str(enriched.get("mint") or "")
    if mint and str(enriched.get("side") or "").upper() in {"BUY", "SELL"}:
        state = self.tokens.get(mint)
        metadata = dict(enriched.get("metadata") or {})
        curve = s.curve_meta(state) if state is not None else None
        if curve:
            metadata["curve"] = curve
            metadata["token_program"] = s.TOKEN_2022_PROGRAM_ID
            metadata["token_decimals"] = 6
        if mint in s.POLICY_BY_MINT:
            metadata["e4_policy"] = dict(s.POLICY_BY_MINT[mint])
        enriched["metadata"] = metadata
    return await _previous_execute(self, request_id, enriched)


core.Engine.execute = execute


async def worker_prefetch(self: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    async with self.lock:
        for attempt in range(2):
            await self.start()
            assert self.process and self.process.stdin and self.process.stdout
            try:
                self.process.stdin.write(json.dumps(dict(request), separators=(",", ":")).encode() + b"\n")
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout)
                if not line:
                    raise RuntimeError("persistent E4 builder closed stdout during prefetch")
                result = json.loads(line)
                if result.get("error"):
                    raise RuntimeError(str(result["error"]))
                return dict(result)
            except Exception:
                await self.stop()
                if attempt:
                    raise
    raise RuntimeError("E4 builder prefetch retries exhausted")


async def pool_start(self: Any) -> None:
    await asyncio.gather(*(worker.start() for worker in self.workers))
    request = {"request_id": f"warm-{time.time_ns()}", "side": "WARM", "metadata": {}}
    await asyncio.gather(*(worker.prefetch(request) for worker in self.workers))


async def pool_prefetch(self: Any, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(worker.prefetch(request) for worker in self.workers)))


final.BuilderWorker.prefetch = worker_prefetch
final.BuilderPool.start = pool_start
final.BuilderPool.prefetch = pool_prefetch

_previous_on_event = core.Engine.on_event


async def on_event(self: Any, event: Any) -> None:
    await _previous_on_event(self, event)
    if event.kind == core.EventKind.CREATE:
        state = self.tokens.get(event.mint)
        curve = s.curve_meta(state) if state is not None else None
        prefetch = getattr(self.builder, "prefetch", None)
        if curve and prefetch is not None:
            self.spawn(prefetch({
                "request_id": f"prefetch-{event.mint}-{time.time_ns()}", "side": "PREFETCH",
                "mint": event.mint, "public_key": self.signer.wallet,
                "metadata": {"curve": curve, "token_program": s.TOKEN_2022_PROGRAM_ID, "token_decimals": 6},
            }))


core.Engine.on_event = on_event

# Existing rebroadcast waited for the slowest route response before checking
# chain status and also suffixed route names before header lookup, which could
# silently drop per-route authentication. Launch the exact same signed tx on
# all configured routes, preserve base route names for headers, and check
# confirmation as soon as the first route completes.
async def fast_route_submit(self: Any, tx: str, signature: str):
    rounds = max(1, min(8, int(os.getenv("E4_REBROADCAST_ROUNDS", "4"))))
    interval = max(0.025, float(os.getenv("E4_REBROADCAST_INTERVAL_SECONDS", "0.12")))
    poll = max(0.005, min(0.05, float(os.getenv("E4_ROUTE_STATUS_POLL_SECONDS", "0.015"))))
    deadline = time.monotonic() + self.settings.confirmation_timeout_seconds
    all_results: list[Any] = []
    first_route = "NONE"
    last_error: str | None = None
    pending: set[asyncio.Task[Any]] = set()

    async def harvest(wait_seconds: float) -> tuple[bool, int | None, str | None]:
        nonlocal first_route, last_error, pending
        if pending:
            done, remaining = await asyncio.wait(
                pending,
                timeout=max(0.0, wait_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            pending = set(remaining)
            for task in done:
                try:
                    item = task.result()
                except Exception as exc:
                    last_error = str(exc)
                    continue
                all_results.append(item)
                if item.accepted and first_route == "NONE":
                    first_route = item.name
                if item.error:
                    last_error = item.error
        status = getattr(self, "_status", None)
        if status is not None:
            confirmed, slot, error = await status(signature)
            last_error = error or last_error
            return confirmed, slot, error
        return False, None, None

    try:
        for _round in range(rounds):
            for index, (name, url) in enumerate(self.routes):
                # Keep the original route name so `_headers(name)` still finds
                # Helius/Nozomi/Jito credentials.
                pending.add(asyncio.create_task(self._send(index, name, url, tx, signature)))
            round_deadline = min(deadline, time.monotonic() + interval)
            while time.monotonic() < round_deadline:
                confirmed, slot, error = await harvest(min(poll, round_deadline - time.monotonic()))
                if confirmed:
                    return first_route, True, slot, None, all_results
                if error and slot is not None:
                    return first_route, False, slot, error, all_results
            if time.monotonic() >= deadline:
                break
        while time.monotonic() < deadline:
            confirmed, slot, error = await harvest(min(poll, deadline - time.monotonic()))
            if confirmed:
                return first_route, True, slot, None, all_results
            if error and slot is not None:
                return first_route, False, slot, error, all_results
        return first_route, False, None, last_error or "confirmation timeout", all_results
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


core.RouteSender.submit = fast_route_submit

_previous_run = core.Engine.run


async def run(self: Any) -> None:
    start = getattr(self.builder, "start", None)
    if start is not None:
        await start()
    await _previous_run(self)


core.Engine.run = run
