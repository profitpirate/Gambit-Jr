"""Final V12 production hardening layer.

Imported last by the live entrypoint. It does not replace selection logic; it
makes execution/recovery/sizing/watchdogs fail-safe and durable.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import e4_hardening_v10 as v10
from . import e4_role_model_v12 as role_model
from .v12_backup import EncryptedBackupManager, decode_aes_key
from .v12_capacity import decide_capacity
from .v12_execution_journal import ExecutionJournal
from .v12_recovery import reconcile_engine, signature_status
from .v12_route_health import RouteHealthStore
from .v12_safety import CircuitBreaker, CircuitConfig, SafetyMode, SafetyStore
from .v12_security import TamperEvidentAuditLog, decode_secret_key, install_redaction_filter
from .v12_watchdogs import WatchdogManager

LOGGER = logging.getLogger("gambit.v12.production")
core = role_model.core
v6 = role_model.v6

_PREVIOUS_ENGINE_INIT = core.Engine.__init__
_PREVIOUS_RUN = core.Engine.run
_PREVIOUS_STOP = core.Engine.stop
_PREVIOUS_ON_EVENT = core.Engine.on_event
_PREVIOUS_EXECUTE_BUY = core.Engine.execute_buy
_PREVIOUS_EXECUTE_SELL = core.Engine.execute_sell
_PREVIOUS_STORE_STATUS = core.Store.status


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _engine_init(self: Any, settings: Any) -> None:
    _PREVIOUS_ENGINE_INIT(self, settings)
    install_redaction_filter()

    self.v12_journal = ExecutionJournal(self.store.conn)
    self.v12_safety_store = SafetyStore(self.store.conn)
    self.v12_breaker = CircuitBreaker(
        self.v12_safety_store,
        CircuitConfig(
            max_drawdown_fraction=_float_env("V12_MAX_DRAWDOWN_FRACTION", 0.15),
            reduce_drawdown_fraction=_float_env("V12_EXIT_ONLY_DRAWDOWN_FRACTION", 0.10),
            max_daily_loss_fraction=_float_env("V12_MAX_DAILY_LOSS_FRACTION", 0.10),
            max_consecutive_losses=_int_env("V12_MAX_CONSECUTIVE_LOSSES", 6),
            max_tx_failures_window=_int_env("V12_MAX_TX_FAILURES_60S", 5),
            minimum_equity_sol=_float_env("V12_MINIMUM_EQUITY_SOL", 0.05),
        ),
    )
    self.v12_route_health = RouteHealthStore(self.store.conn)
    self.v12_tx_failures = collections.deque()
    self.v12_closed_recorded: set[str] = set()
    self.v12_watchdog = None
    self.v12_watchdog_task = None
    self.v12_backup_task = None
    self.v12_emergency_exit_lock = asyncio.Lock()

    audit_key = os.getenv("V12_AUDIT_HMAC_KEY", "")
    self.v12_audit = (
        TamperEvidentAuditLog(
            Path(os.getenv("V12_AUDIT_LOG", "data/v12-audit.jsonl")),
            decode_secret_key(audit_key),
        )
        if audit_key
        else None
    )

    backup_key = os.getenv("V12_BACKUP_AES_KEY", "")
    self.v12_backup = (
        EncryptedBackupManager(
            Path(os.getenv("V12_BACKUP_DIR", "backups/v12")),
            decode_aes_key(backup_key),
            retain=_int_env("V12_BACKUP_RETAIN", 24),
        )
        if backup_key
        else None
    )

    # Recovery uses these explicit factories rather than importing engine internals.
    self.position_factory = core.Position
    self.position_status_open = core.PositionStatus.OPEN
    self.position_status_closed = core.PositionStatus.CLOSED


core.Engine.__init__ = _engine_init


def _audit(engine: Any, event: str, payload: dict[str, Any]) -> None:
    if engine.v12_audit is not None:
        engine.v12_audit.append(event, payload)


def _record_tx_result(engine: Any, success: bool) -> None:
    now = time.monotonic()
    window = engine.v12_tx_failures
    while window and now - window[0] > 60.0:
        window.popleft()
    if not success:
        window.append(now)
    engine.v12_safety_store.set_tx_failures(len(window))
    engine.v12_breaker.evaluate_operational()


def _rank_routes(engine: Any, *, side: str) -> None:
    routes = list(getattr(engine.sender, "routes", []))
    if len(routes) <= 1:
        return
    names = [str(name) for name, _url in routes]
    ranked_names = engine.v12_route_health.ranked(names)
    rank = {name: index for index, name in enumerate(ranked_names)}
    routes.sort(key=lambda row: rank.get(str(row[0]), len(rank)))
    # For exits we keep every route. For entries, a route in hard cooldown is
    # placed last but not removed, preserving redundancy if healthy routes fail.
    engine.sender.routes = routes


async def _execute_exactly_once(
    self: Any,
    request_id: str,
    request: Mapping[str, Any],
) -> tuple[str, bool, int | None, str | None]:
    enriched = v10._enrich_request(request)
    runtime = v10._runtime_for(self)
    entry = self.v12_journal.prepare(request_id, enriched)

    if entry.state == "CONFIRMED" and entry.signature:
        return entry.signature, True, entry.slot, None

    signed = entry.signed_tx_b64
    signature = entry.signature
    if signed and signature:
        status, slot, chain_error = await signature_status(self.rpc, signature)
        if status == "CONFIRMED":
            self.v12_journal.mark_confirmed(
                entry.idempotency_key,
                route="pre_submit_recovery",
                slot=slot,
            )
            return signature, True, slot, None
        if status == "FAILED":
            self.v12_journal.mark_failed(
                entry.idempotency_key,
                chain_error or "chain failure",
                terminal=True,
            )
            _record_tx_result(self, False)
            return signature, False, slot, chain_error or "chain transaction failed"
    else:
        build_started = time.time_ns()
        unsigned = await self.builder.build(enriched)
        build_completed = time.time_ns()
        runtime.latency.build_done(request_id, build_started, build_completed)

        sign_started = time.time_ns()
        signed, signature = await self.signer.sign(unsigned)
        sign_completed = time.time_ns()
        runtime.latency.sign_done(request_id, sign_started, sign_completed)
        self.v12_journal.mark_signed(
            entry.idempotency_key,
            signed_tx_b64=signed,
            signature=signature,
        )
        _audit(
            self,
            "TRANSACTION_SIGNED",
            {
                "request_id": request_id,
                "side": enriched.get("side"),
                "mint": enriched.get("mint"),
                "signature": signature,
            },
        )

    assert signed and signature
    side = str(enriched.get("side") or "")
    _rank_routes(self, side=side)
    submit_started = time.time_ns()
    runtime.latency.submit_started(request_id, submit_started)
    self.v12_journal.mark_submitted(entry.idempotency_key)

    route, confirmed, slot, error, results = await self.sender.submit(signed, signature)
    runtime.latency.route_done(request_id, time.time_ns())

    mapped = {
        item.name: item.result if item.accepted else (item.error or "rejected")
        for item in results
    }
    self.store.receipt(request_id, signature, route, confirmed, slot, error, mapped)
    for item in results:
        self.store.route_metric(
            request_id,
            item.name,
            item.submitted_ns,
            item.completed_ns,
            item.result,
            item.error,
        )
        self.v12_route_health.record(
            item.name,
            accepted=bool(item.accepted),
            latency_ms=max(0.0, (item.completed_ns - item.submitted_ns) / 1e6),
        )

    if confirmed:
        self.v12_journal.mark_confirmed(entry.idempotency_key, route=route, slot=slot)
    else:
        self.v12_journal.mark_failed(
            entry.idempotency_key,
            error or "transaction unconfirmed",
            terminal=False,
        )
    _record_tx_result(self, confirmed)
    _audit(
        self,
        "TRANSACTION_RESULT",
        {
            "request_id": request_id,
            "side": side,
            "mint": enriched.get("mint"),
            "signature": signature,
            "route": route,
            "confirmed": confirmed,
            "slot": slot,
            "error": error,
        },
    )
    return signature, confirmed, slot, error


core.Engine.execute = _execute_exactly_once


def _capacity_for(self: Any, state: Any, requested: float):
    curve = v6._CURVE_BY_MINT.get(str(state.mint), {})
    virtual_sol = float(
        curve.get("virtual_sol_reserves")
        or getattr(state, "virtual_sol", 0.0)
        or 0.0
    )
    virtual_tokens = float(
        curve.get("virtual_token_reserves")
        or getattr(state, "virtual_tokens", 0.0)
        or 0.0
    )
    return decide_capacity(
        requested_sol=requested,
        virtual_sol=virtual_sol,
        virtual_tokens=virtual_tokens,
        min_virtual_sol=_float_env("V12_MIN_VIRTUAL_SOL_LIQUIDITY", 5.0),
        max_price_impact_bps=_float_env("V12_MAX_ENTRY_PRICE_IMPACT_BPS", 600.0),
        max_virtual_sol_fraction=_float_env("V12_MAX_CURVE_RESERVE_FRACTION", 0.08),
        absolute_cap_sol=min(
            float(self.settings.max_position_sol),
            _float_env("V12_ABSOLUTE_POSITION_CAP_SOL", 20.0),
        ),
        minimum_trade_sol=float(self.settings.min_position_sol),
    )


async def _execute_buy_production(
    self: Any,
    state: Any,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    if not self.v12_breaker.store.snapshot().entries_allowed:
        self.pending_entries.discard(state.mint)
        self.store.decision(
            state.mint,
            None,
            "SKIP",
            score,
            f"V12 safety blocked entry: {self.v12_breaker.store.snapshot().reason}",
            {"fraction": fraction},
        )
        return

    creator = str(
        v6._CONTEXT_BY_MINT.get(str(state.mint), {}).get("creator")
        or getattr(state, "creator", "")
        or ""
    )
    lifecycle = getattr(self, "v12_creator_lifecycle", None)
    if lifecycle is not None and creator and not lifecycle.entry_allowed(creator):
        self.pending_entries.discard(state.mint)
        self.store.decision(
            state.mint,
            None,
            "SKIP",
            score,
            "V12 creator lifecycle quarantine/probation veto",
            {"creator": creator},
        )
        return

    mint = state.mint
    reserved = 0.0
    runtime = v10._runtime_for(self)
    try:
        if self.store.has_entered(mint):
            return
        cache = runtime.balance_cache
        balance = (
            await cache.available(
                _float_env("E4_BALANCE_CACHE_MAX_STALENESS_MS", 1000.0)
            )
            if cache is not None
            else await self.rpc.balance(self.signer.wallet)
        )
        async with self.allocation_lock:
            priority, tip = self.fee_bid(balance * fraction, score)
            deployable = balance - self.settings.reserve_sol - self.reserved_sol - priority - tip
            requested = min(
                max(0.0, balance * min(fraction, self.settings.max_position_fraction)),
                max(0.0, deployable),
                self.settings.max_position_sol,
            )
            capacity = _capacity_for(self, state, requested)
            amount = capacity.allowed_sol
            if capacity.blocked or amount < self.settings.min_position_sol:
                self.store.decision(
                    mint,
                    None,
                    "SKIP",
                    score,
                    f"V12 capacity veto: {capacity.reason}",
                    {
                        "requested_sol": requested,
                        "allowed_sol": amount,
                        "price_impact_bps": capacity.price_impact_bps,
                    },
                )
                return
            reserved = amount + priority + tip
            self.reserved_sol += reserved
            if not self.store.mark_entry(mint, score, reason):
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
                reserved = 0.0
                return

        request_id = str(uuid.uuid4())
        context = v6._CONTEXT_BY_MINT.get(mint, {})
        launch_received_ns = int(
            context.get("create_received_ns")
            or context.get("last_received_ns")
            or time.time_ns()
        )
        decision_completed_ns = int(
            context.get("v10_decision_completed_ns") or time.time_ns()
        )
        runtime.latency.begin(
            request_id,
            mint=mint,
            launch_received_ns=launch_received_ns,
            decision_completed_ns=decision_completed_ns,
        )
        request = {
            "request_id": request_id,
            "side": "BUY",
            "mint": mint,
            "public_key": self.signer.wallet,
            "amount": amount,
            "denominated_in_sol": True,
            "slippage_bps": self.settings.buy_slippage_bps,
            "priority_fee_sol": priority,
            "tip_sol": tip,
            "pool": "pump",
            "metadata": {
                "score": score,
                "reason": reason,
                "fdv_usd": state.fdv_usd,
                "launch_received_ns": launch_received_ns,
                "decision_completed_ns": decision_completed_ns,
                "capacity_requested_sol": requested,
                "capacity_allowed_sol": amount,
                "capacity_price_impact_bps": capacity.price_impact_bps,
            },
        }
        self.store.order(request_id, mint, "BUY", amount, None, reason)
        signature, confirmed, _slot, error = await self.execute(request_id, request)
        if cache is not None:
            cache.apply_estimated_delta(-reserved)
        if not confirmed:
            LOGGER.error(
                "V12 production buy failed mint=%s signature=%s error=%s",
                mint,
                signature,
                error,
            )
            if cache is not None:
                try:
                    await cache.refresh()
                except RuntimeError:
                    pass
            return

        after_tokens = await role_model.final._token_balance_after_change(
            self.rpc,
            self.signer.wallet,
            mint,
            0.0,
            "up",
        )
        received = max(0.0, after_tokens)
        if received <= 0:
            self.v12_breaker.exit_only("confirmed_buy_token_balance_not_visible")
            raise RuntimeError("confirmed V12 buy without observable token balance")

        entry_price = amount / received
        position = core.Position(
            position_id=str(uuid.uuid4()),
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=amount,
            tokens=received,
            remaining=received,
            entry_price=entry_price,
            max_price=state.price_sol or entry_price,
            last_price=state.price_sol or entry_price,
            entry_signature=signature,
        )
        self.positions[mint] = position
        self.store.save_position(position)
        v6._persist_profile(self, mint)
        _audit(
            self,
            "POSITION_OPENED",
            {
                "mint": mint,
                "creator": creator,
                "amount_sol": amount,
                "signature": signature,
                "capacity_price_impact_bps": capacity.price_impact_bps,
            },
        )
    except Exception:
        LOGGER.exception("V12 production buy execution error mint=%s", mint)
        self.v12_breaker.exit_only("buy_execution_exception")
    finally:
        if reserved:
            async with self.allocation_lock:
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
        self.pending_entries.discard(mint)


core.Engine.execute_buy = _execute_buy_production


async def _execute_sell_production(
    self: Any,
    position: Any,
    fraction: float,
    reason: str,
) -> None:
    was_closed = position.status == core.PositionStatus.CLOSED
    await _PREVIOUS_EXECUTE_SELL(self, position, fraction, reason)
    if was_closed or position.status != core.PositionStatus.CLOSED:
        return
    if position.position_id in self.v12_closed_recorded:
        return
    self.v12_closed_recorded.add(position.position_id)
    pnl = float(position.realized_sol) - float(position.entry_sol)
    self.v12_safety_store.record_trade_result(pnl)
    self.v12_breaker.evaluate_operational()
    creator = str(
        v6._CONTEXT_BY_MINT.get(str(position.mint), {}).get("creator") or ""
    )
    lifecycle = getattr(self, "v12_creator_lifecycle", None)
    if lifecycle is not None and creator:
        lifecycle.observe_trade(
            creator,
            won=pnl > 0,
            pnl_sol=pnl,
            observed_ns=time.time_ns(),
        )
    _audit(
        self,
        "POSITION_CLOSED",
        {
            "mint": position.mint,
            "creator": creator,
            "pnl_sol": pnl,
            "entry_sol": position.entry_sol,
            "realized_sol": position.realized_sol,
            "signature": position.close_signature,
            "reason": reason,
        },
    )


core.Engine.execute_sell = _execute_sell_production


async def _emergency_exit_all(self: Any, reason: str) -> None:
    async with self.v12_emergency_exit_lock:
        work = []
        for mint, position in list(self.positions.items()):
            if mint in self.pending_exits:
                continue
            self.pending_exits.add(mint)
            work.append(self.execute_sell(position, 1.0, reason))
        if work:
            await asyncio.gather(*work, return_exceptions=True)


async def _backup_loop(self: Any) -> None:
    interval = max(60.0, _float_env("V12_BACKUP_INTERVAL_SECONDS", 900.0))
    while not self.stop_event.is_set():
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        if self.v12_backup is None:
            continue
        try:
            result = await asyncio.to_thread(
                self.v12_backup.backup,
                Path(self.settings.execution_db),
                label="execution",
            )
            _audit(
                self,
                "ENCRYPTED_BACKUP",
                {
                    "destination": result.destination,
                    "plaintext_bytes": result.plaintext_bytes,
                    "encrypted_bytes": result.encrypted_bytes,
                },
            )
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.exception("V12 encrypted backup failed")
            self.v12_breaker.exit_only(f"backup_failure:{exc}")


async def _run_production(self: Any) -> None:
    report = await reconcile_engine(
        self,
        self.v12_journal,
        self.v12_breaker,
    )
    _audit(self, "STARTUP_RECOVERY", report.__dict__)

    self.v12_watchdog = WatchdogManager(
        self,
        self.v12_breaker,
        self.v12_route_health,
        emergency_exit=lambda reason: _emergency_exit_all(self, reason),
    )
    self.v12_watchdog_task = asyncio.create_task(
        self.v12_watchdog.run(),
        name="v12-watchdogs",
    )
    if self.v12_backup is not None:
        self.v12_backup_task = asyncio.create_task(
            _backup_loop(self),
            name="v12-encrypted-backups",
        )

    try:
        await _PREVIOUS_RUN(self)
    finally:
        if self.v12_watchdog is not None:
            self.v12_watchdog.stop()
        for task in (self.v12_watchdog_task, self.v12_backup_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[
                task
                for task in (self.v12_watchdog_task, self.v12_backup_task)
                if task is not None
            ],
            return_exceptions=True,
        )
        _audit(
            self,
            "ENGINE_STOPPED",
            {
                "safety": self.v12_safety_store.snapshot().mode.value,
                "journal": self.v12_journal.state_counts(),
            },
        )


core.Engine.run = _run_production


async def _on_event_production(self: Any, event: Any) -> None:
    if self.v12_watchdog is not None:
        self.v12_watchdog.touch_event()
    await _PREVIOUS_ON_EVENT(self, event)


core.Engine.on_event = _on_event_production


def _stop_production(self: Any) -> None:
    if self.v12_watchdog is not None:
        self.v12_watchdog.stop()
    _PREVIOUS_STOP(self)


core.Engine.stop = _stop_production


def _status_production(self: Any) -> dict[str, Any]:
    payload = dict(_PREVIOUS_STORE_STATUS(self))
    try:
        safety = SafetyStore(self.conn)
        journal = ExecutionJournal(self.conn)
        route = RouteHealthStore(self.conn)
        payload["v12_production"] = {
            "safety": safety.snapshot().mode.value,
            "safety_reason": safety.snapshot().reason,
            "journal": journal.state_counts(),
            "routes": {
                str(row[0]): {
                    "success": float(row[1]),
                    "latency_ms": float(row[2]),
                    "failures": int(row[3]),
                }
                for row in self.conn.execute(
                    "SELECT route,ewma_success,ewma_latency_ms,consecutive_failures FROM v12_route_health"
                )
            },
        }
    except (sqlite3.Error, ValueError):
        payload["v12_production"] = {"status": "unavailable"}
    return payload


import sqlite3  # noqa: E402 - used by final status wrapper

core.Store.status = _status_production
