from __future__ import annotations

import asyncio
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
    await _previous_execute_buy(self, state, score, min(float(policy["target_fraction"]), self.settings.max_position_fraction), reason)


core.Engine.execute_buy = execute_buy

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

_previous_run = core.Engine.run


async def run(self: Any) -> None:
    start = getattr(self.builder, "start", None)
    if start is not None:
        await start()
    await _previous_run(self)


core.Engine.run = run
