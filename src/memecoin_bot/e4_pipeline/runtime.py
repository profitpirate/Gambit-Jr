from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .coordinator import PipelineCoordinator
from .narrative import ActiveNarrativeCache
from .registry import AtomicCreatorRegistry
from .teacher import E4Teacher
from .x_stream import XAccountRegistry, XFilteredStream

LOGGER = logging.getLogger("gambit.e4.pipeline.runtime")


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    at = (len(ordered) - 1) * q
    lower = int(at)
    upper = min(len(ordered) - 1, lower + 1)
    weight = at - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _summary(values: deque[float]) -> dict[str, float | int | None]:
    rows = list(values)
    return {"count": len(rows), "min": min(rows) if rows else None, "median": _percentile(rows, 0.50), "p95": _percentile(rows, 0.95), "p99": _percentile(rows, 0.99), "max": max(rows) if rows else None}


class LatencyRecorder:
    def __init__(self, max_samples: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._request: dict[str, dict[str, Any]] = {}
        self.decision_ms: deque[float] = deque(maxlen=max_samples)
        self.build_ms: deque[float] = deque(maxlen=max_samples)
        self.sign_ms: deque[float] = deque(maxlen=max_samples)
        self.launch_to_submit_ms: deque[float] = deque(maxlen=max_samples)
        self.launch_to_route_return_ms: deque[float] = deque(maxlen=max_samples)
        self.deadline_misses = 0

    def begin(self, request_id: str, *, mint: str, launch_received_ns: int, decision_completed_ns: int) -> None:
        with self._lock:
            self._request[request_id] = {"mint": mint, "launch_received_ns": launch_received_ns, "decision_completed_ns": decision_completed_ns}
            if launch_received_ns and decision_completed_ns >= launch_received_ns:
                self.decision_ms.append((decision_completed_ns - launch_received_ns) / 1_000_000)

    def build_done(self, request_id: str, started_ns: int, completed_ns: int) -> None:
        with self._lock:
            self.build_ms.append(max(0.0, (completed_ns - started_ns) / 1_000_000))
            if request_id in self._request:
                self._request[request_id]["build_completed_ns"] = completed_ns

    def sign_done(self, request_id: str, started_ns: int, completed_ns: int) -> None:
        with self._lock:
            self.sign_ms.append(max(0.0, (completed_ns - started_ns) / 1_000_000))
            if request_id in self._request:
                self._request[request_id]["sign_completed_ns"] = completed_ns

    def submit_started(self, request_id: str, submitted_ns: int) -> None:
        with self._lock:
            row = self._request.get(request_id)
            if not row:
                return
            launch = int(row.get("launch_received_ns") or 0)
            if launch and submitted_ns >= launch:
                elapsed = (submitted_ns - launch) / 1_000_000
                self.launch_to_submit_ms.append(elapsed)
                if elapsed > 36.0:
                    self.deadline_misses += 1
            row["submit_started_ns"] = submitted_ns

    def route_done(self, request_id: str, completed_ns: int) -> None:
        with self._lock:
            row = self._request.pop(request_id, None)
            if not row:
                return
            launch = int(row.get("launch_received_ns") or 0)
            if launch and completed_ns >= launch:
                self.launch_to_route_return_ms.append((completed_ns - launch) / 1_000_000)

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "budget_definition": "Pump CREATE/event receipt to first route-submit coroutine invocation; validator landing is not claimed within 36ms",
                "target_ms": 36.0,
                "decision_ms": _summary(self.decision_ms),
                "builder_roundtrip_ms": _summary(self.build_ms),
                "signing_ms": _summary(self.sign_ms),
                "launch_to_submit_start_ms": _summary(self.launch_to_submit_ms),
                "launch_to_route_return_ms": _summary(self.launch_to_route_return_ms),
                "deadline_misses": self.deadline_misses,
                "tracked_inflight": len(self._request),
            }


class WalletBalanceCache:
    def __init__(self, rpc: Any, wallet: str, refresh_ms: float = 100.0) -> None:
        self.rpc = rpc
        self.wallet = wallet
        self.refresh_seconds = max(0.025, refresh_ms / 1000.0)
        self.balance: float | None = None
        self.updated_ns = 0
        self.last_error: str | None = None
        self.refreshes = 0

    async def refresh(self) -> float:
        value = await self.rpc.balance(self.wallet)
        self.balance = max(0.0, float(value))
        self.updated_ns = time.time_ns()
        self.refreshes += 1
        self.last_error = None
        return self.balance

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    async def available(self, max_staleness_ms: float = 1_000.0) -> float:
        now = time.time_ns()
        if self.balance is None or now - self.updated_ns > max_staleness_ms * 1_000_000:
            return await self.refresh()
        return self.balance

    def apply_estimated_delta(self, delta_sol: float) -> None:
        if self.balance is not None:
            self.balance = max(0.0, self.balance + delta_sol)
            self.updated_ns = time.time_ns()

    def stats(self) -> dict[str, Any]:
        return {"balance": self.balance, "age_ms": (time.time_ns() - self.updated_ns) / 1_000_000 if self.updated_ns else None, "refreshes": self.refreshes, "last_error": self.last_error}


class V10Runtime:
    def __init__(self, *, oracle_wallet: str, fraction_resolver: Any, execution_db: Path) -> None:
        self.registry = AtomicCreatorRegistry(Path(os.getenv("E4_CREATOR_EXPECTANCY_PATH", "models/e4/e4-creator-expectancy.json")), Path(os.getenv("E4_DISCOVERED_CREATORS_PATH", "models/e4/e4-discovered-creators.json")))
        self.narratives = ActiveNarrativeCache(
            ttl_seconds=float(os.getenv("E4_NARRATIVE_TTL_SECONDS", "1800")),
            minimum_authority=float(os.getenv("E4_SOCIAL_MIN_AUTHORITY", "0.55")),
            exact_single_source_authority=float(os.getenv("E4_SOCIAL_EXACT_SINGLE_SOURCE_AUTHORITY", "0.88")),
            decision_threshold=float(os.getenv("E4_SOCIAL_DECISION_THRESHOLD", "0.76")),
        )
        teacher_path = Path(os.getenv("E4_TEACHER_DATABASE_PATH", str(execution_db.with_name(execution_db.stem + "-teacher.db"))))
        self.teacher = E4Teacher(registry=self.registry, database_path=teacher_path, oracle_wallet=oracle_wallet, scan_command=os.getenv("E4_HISTORY_SCANNER_COMMAND", ""), copy_ttl_ms=float(os.getenv("E4_COPY_SIGNAL_TTL_MS", "120")))
        self.coordinator = PipelineCoordinator(registry=self.registry, narratives=self.narratives, teacher=self.teacher, fraction_resolver=fraction_resolver)
        account_registry = XAccountRegistry(Path(os.getenv("E4_X_ACCOUNTS_PATH", "models/e4/e4-social-accounts.json")))
        self.x_stream = XFilteredStream(bearer_token=os.getenv("E4_X_BEARER_TOKEN", ""), accounts=account_registry, cache=self.narratives, api_base=os.getenv("E4_X_API_BASE", "https://api.x.com/2"))
        self.latency = LatencyRecorder()
        self.stop = asyncio.Event()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.balance_cache: WalletBalanceCache | None = None
        self.events_seen = 0
        self.launches_seen = 0
        self.oracle_events_seen = 0

    async def start(self, engine: Any) -> None:
        wallet = str(getattr(getattr(engine, "signer", None), "wallet", "") or "")
        if wallet:
            self.balance_cache = WalletBalanceCache(engine.rpc, wallet, refresh_ms=float(os.getenv("E4_BALANCE_CACHE_REFRESH_MS", "100")))
            try:
                await self.balance_cache.refresh()
            except Exception:
                LOGGER.exception("Initial E4 wallet-balance cache refresh failed")
            self._spawn(self.balance_cache.run(self.stop), "e4-v10-balance-cache")
        self._spawn(self.teacher.scan_worker(self.stop), "e4-v10-history-scanner")
        if self.x_stream.enabled:
            self._spawn(self.x_stream.run(self.stop), "e4-v10-x-stream")
        self._spawn(self._maintenance(), "e4-v10-maintenance")

    async def close(self) -> None:
        self.stop.set()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.teacher.close()

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _maintenance(self) -> None:
        while not self.stop.is_set():
            self.teacher.reap()
            self.narratives.prune()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def pre_event(self, event: Any, context: dict[str, Any]) -> None:
        self.events_seen += 1
        context["last_received_ns"] = int(getattr(event, "received_ns", 0) or time.time_ns())
        kind = str(getattr(getattr(event, "kind", None), "value", getattr(event, "kind", ""))).upper()
        if kind == "CREATE":
            self.launches_seen += 1
            context.setdefault("create_received_ns", context["last_received_ns"])
        signal = self.teacher.pre_signal(event, creator=str(context.get("creator") or getattr(event, "creator", "") or "") or None)
        if signal is not None:
            self.oracle_events_seen += 1
            context["e4_copy_signal_ns"] = signal.observed_ns
            context["e4_copy_entry_price_sol"] = signal.e4_entry_price_sol

    def post_event(self, event: Any, state: Any) -> None:
        if str(getattr(event, "trader", "") or "") == self.teacher.oracle_wallet:
            self.oracle_events_seen += 1
            self.teacher.observe(event, state)

    def stats(self) -> dict[str, Any]:
        return {"events_seen": self.events_seen, "launches_seen": self.launches_seen, "oracle_events_seen": self.oracle_events_seen, "creator_registry": self.registry.counts(), "active_narratives": self.narratives.size(), "teacher": self.teacher.stats(), "x_stream": self.x_stream.stats(), "wallet_cache": self.balance_cache.stats() if self.balance_cache else None, "latency": self.latency.report()}

    def write_status(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.stats(), indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
