"""Crash-safe execution and position reconciliation for V12 live trading."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .v12_execution_journal import ExecutionJournal
from .v12_safety import CircuitBreaker

LOGGER = logging.getLogger("gambit.v12.recovery")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    journal_checked: int
    journal_confirmed: int
    journal_retried: int
    journal_uncertain: int
    positions_checked: int
    positions_closed: int
    positions_reconstructed: int


async def signature_status(rpc: Any, signature: str) -> tuple[str, int | None, str | None]:
    try:
        result = await rpc.call(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}],
        )
    except RuntimeError as exc:
        return "RPC_ERROR", None, str(exc)
    status = (result.get("value") or [None])[0] if result else None
    if status is None:
        return "NOT_FOUND", None, None
    if status.get("err") is not None:
        return "FAILED", status.get("slot"), json.dumps(status["err"], default=str)
    if status.get("confirmationStatus") in {"processed", "confirmed", "finalized"}:
        return "CONFIRMED", status.get("slot"), None
    return "PENDING", status.get("slot"), None


async def reconcile_journal(
    engine: Any,
    journal: ExecutionJournal,
    breaker: CircuitBreaker,
    *,
    states: tuple[str, ...] = ("SIGNED", "SUBMITTED", "UNCERTAIN"),
) -> tuple[int, int, int, int]:
    checked = confirmed = retried = uncertain = 0
    for entry in journal.recoverable(states):
        checked += 1
        if not entry.signature:
            journal.mark_failed(entry.idempotency_key, "recoverable entry has no signature", terminal=True)
            breaker.exit_only("journal_entry_missing_signature")
            uncertain += 1
            continue
        status, slot, error = await signature_status(engine.rpc, entry.signature)
        if status == "CONFIRMED":
            journal.mark_confirmed(entry.idempotency_key, route="recovery_chain", slot=slot)
            confirmed += 1
            continue
        if status == "FAILED":
            journal.mark_failed(entry.idempotency_key, error or "chain transaction failed", terminal=True)
            continue
        if status == "RPC_ERROR":
            breaker.exit_only("recovery_rpc_unavailable")
            uncertain += 1
            continue

        # Re-sending the exact same signed Solana transaction preserves the same
        # signature and is idempotent on chain. Never rebuild/re-sign during recovery.
        if entry.signed_tx_b64 and entry.attempts < 2:
            journal.mark_submitted(entry.idempotency_key)
            route, landed, landed_slot, submit_error, _results = await engine.sender.submit(
                entry.signed_tx_b64,
                entry.signature,
            )
            retried += 1
            if landed:
                journal.mark_confirmed(entry.idempotency_key, route=route, slot=landed_slot)
                confirmed += 1
            else:
                journal.mark_failed(
                    entry.idempotency_key,
                    submit_error or "recovery resubmit unconfirmed",
                    terminal=False,
                )
                uncertain += 1
            continue

        journal.mark_failed(
            entry.idempotency_key,
            f"transaction state unresolved during recovery: {status}",
            terminal=False,
        )
        breaker.exit_only("unresolved_transaction_requires_reconciliation")
        uncertain += 1
    return checked, confirmed, retried, uncertain


async def reconcile_positions(
    engine: Any,
    journal: ExecutionJournal,
    breaker: CircuitBreaker,
) -> tuple[int, int, int]:
    checked = closed = reconstructed = 0
    for mint, position in list(engine.positions.items()):
        checked += 1
        try:
            live_tokens = await engine.rpc.token_balance(engine.signer.wallet, mint)
        except RuntimeError:
            breaker.exit_only("position_recovery_rpc_failure")
            continue
        if live_tokens <= max(1e-9, float(position.tokens) * 1e-8):
            position.remaining = 0.0
            position.status = engine.position_status_closed
            engine.positions.pop(mint, None)
            engine.store.save_position(position)
            closed += 1
        else:
            position.remaining = min(float(position.tokens), live_tokens)
            engine.store.save_position(position)

    existing = set(engine.positions)
    confirmed_buys = journal.conn.execute(
        """
        SELECT * FROM v12_execution_journal
        WHERE state='CONFIRMED' AND side='BUY'
        ORDER BY created_ns
        """
    ).fetchall()
    for row in confirmed_buys:
        mint = str(row["mint"] or "")
        if not mint or mint in existing:
            continue
        try:
            live_tokens = await engine.rpc.token_balance(engine.signer.wallet, mint)
        except RuntimeError:
            breaker.exit_only("orphan_buy_recovery_rpc_failure")
            continue
        if live_tokens <= 0:
            continue
        payload = json.loads(str(row["payload_json"]))
        amount = float(payload.get("amount") or 0.0)
        if amount <= 0:
            breaker.exit_only("orphan_buy_has_no_cost_basis")
            continue
        signature = str(row["signature"] or "")
        position = engine.position_factory(
            position_id=f"recovered-{uuid.uuid4()}",
            mint=mint,
            status=engine.position_status_open,
            opened_ns=int(row["updated_ns"] or time.time_ns()),
            entry_sol=amount,
            tokens=live_tokens,
            remaining=live_tokens,
            entry_price=amount / live_tokens,
            max_price=amount / live_tokens,
            last_price=amount / live_tokens,
            entry_signature=signature,
        )
        engine.positions[mint] = position
        engine.store.save_position(position)
        existing.add(mint)
        reconstructed += 1

    if reconstructed:
        breaker.store.event(
            "RECOVERED_POSITIONS",
            "WARN",
            {"count": reconstructed},
        )
    return checked, closed, reconstructed


async def reconcile_engine(
    engine: Any,
    journal: ExecutionJournal,
    breaker: CircuitBreaker,
    *,
    journal_states: tuple[str, ...] = ("SIGNED", "SUBMITTED", "UNCERTAIN"),
) -> RecoveryReport:
    journal_result = await reconcile_journal(
        engine,
        journal,
        breaker,
        states=journal_states,
    )
    position_result = await reconcile_positions(engine, journal, breaker)
    return RecoveryReport(
        journal_checked=journal_result[0],
        journal_confirmed=journal_result[1],
        journal_retried=journal_result[2],
        journal_uncertain=journal_result[3],
        positions_checked=position_result[0],
        positions_closed=position_result[1],
        positions_reconstructed=position_result[2],
    )
