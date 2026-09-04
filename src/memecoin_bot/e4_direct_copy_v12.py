from __future__ import annotations

import hashlib
import logging
import math
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import e4_hardening_v10 as v10
from . import e4_pipeline_runtime_v10 as pipeline_runtime
from . import e4_role_model_v12 as role_model

core = role_model.core
v6 = role_model.v6
PIPELINES = role_model.PIPELINES
LOGGER = logging.getLogger("gambit.e4.direct-copy.v12")

DIRECT_COPY_FAMILY = role_model.ROLE_MODEL_FAMILY
DEFAULT_DIRECT_COPY_SLIPPAGE_BPS = 9000


def policy_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assert_policy_fingerprint(expected: str) -> None:
    expected = str(expected or "").strip().lower()
    actual = policy_fingerprint()
    if not expected or expected != actual:
        raise RuntimeError(
            "E4 V12 direct-copy policy fingerprint mismatch "
            f"expected={expected or '<missing>'} actual={actual}"
        )


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def direct_copy_slippage_bps(settings: Any) -> int:
    """Return V12's direct-copy execution tolerance.

    E4's private slippage setting is not observable. V12 therefore keeps the
    local builder ceiling as a hard-copy tolerance. With the V12 exact-SOL
    builder this percentage is applied to minimum token output; it no longer
    shrinks the SOL input / economic position before the transaction is built.
    """
    raw = os.getenv(
        "E4_DIRECT_COPY_SLIPPAGE_BPS",
        str(DEFAULT_DIRECT_COPY_SLIPPAGE_BPS),
    )
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        requested = DEFAULT_DIRECT_COPY_SLIPPAGE_BPS
    return max(
        int(getattr(settings, "buy_slippage_bps", 0) or 0),
        min(9000, max(0, requested)),
    )


def direct_copy_amount_sol(
    observed_e4_sol: float,
    *,
    balance_sol: float,
    reserve_sol: float,
    reserved_sol: float,
    priority_fee_sol: float,
    tip_sol: float,
) -> tuple[float, bool]:
    """Match E4's observed SOL stake whenever the wallet can physically do so."""
    observed = max(0.0, _finite(observed_e4_sol))
    deployable = max(
        0.0,
        _finite(balance_sol)
        - max(0.0, _finite(reserve_sol))
        - max(0.0, _finite(reserved_sol))
        - max(0.0, _finite(priority_fee_sol))
        - max(0.0, _finite(tip_sol)),
    )
    amount = min(observed, deployable)
    exact = observed > 0 and abs(amount - observed) <= max(1e-9, observed * 1e-9)
    return max(0.0, amount), exact


def _is_direct_copy(mint: str) -> bool:
    profile = v6._PROFILE_BY_MINT.get(str(mint))
    return bool(
        profile is not None
        and str(getattr(profile, "family", "") or "") == DIRECT_COPY_FAMILY
    )


def _record_copy_terms(
    mint: str,
    *,
    observed_sol: float,
    submitted_sol: float,
    slippage_bps: int,
    exact_amount: bool,
    source_signature: str,
) -> None:
    context = v6._CONTEXT_BY_MINT.setdefault(str(mint), {})
    context.update(
        {
            "v12_direct_copy_observed_sol": max(0.0, _finite(observed_sol)),
            "v12_direct_copy_submitted_sol": max(0.0, _finite(submitted_sol)),
            "v12_direct_copy_slippage_bps": int(slippage_bps),
            "v12_direct_copy_exact_amount": bool(exact_amount),
            "v12_direct_copy_source_signature": str(source_signature or ""),
            "v12_direct_copy_forced_execution": True,
        }
    )


_PREVIOUS_EXECUTE_BUY = core.Engine.execute_buy


async def _execute_buy_direct_copy_v12(
    self: Any,
    state: Any,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    mint = str(state.mint)
    source = PIPELINES.e4_signal(mint)
    if not _is_direct_copy(mint) or source is None:
        await _PREVIOUS_EXECUTE_BUY(self, state, score, fraction, reason)
        return

    observed_sol = max(0.0, _finite(getattr(source, "entry_sol", 0.0)))
    if observed_sol <= 0:
        # Exact source stake is part of copy fidelity. Do not manufacture an
        # absolute amount from a score when instruction/post-state accounting
        # cannot recover E4's SOL leg.
        await _PREVIOUS_EXECUTE_BUY(self, state, score, fraction, reason)
        return

    reserved = 0.0
    runtime = v10._runtime_for(self)
    try:
        if self.store.has_entered(mint):
            return

        cache = runtime.balance_cache
        if cache is not None:
            balance = await cache.available(
                float(os.getenv("E4_BALANCE_CACHE_MAX_STALENESS_MS", "1000"))
            )
        else:
            balance = await self.rpc.balance(self.signer.wallet)

        async with self.allocation_lock:
            priority, tip = self.fee_bid(observed_sol, score)
            amount, exact_amount = direct_copy_amount_sol(
                observed_sol,
                balance_sol=balance,
                reserve_sol=self.settings.reserve_sol,
                reserved_sol=self.reserved_sol,
                priority_fee_sol=priority,
                tip_sol=tip,
            )
            if amount < self.settings.min_position_sol:
                LOGGER.warning(
                    "E4 V12 direct copy could not submit: insufficient deployable "
                    "balance mint=%s observed_sol=%.9f available_sol=%.9f",
                    mint,
                    observed_sol,
                    balance,
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
        request_created_ns = time.time_ns()
        preconfirm_seen_ns = int(context.get("v12_preconfirm_seen_ns") or 0)
        preconfirm_dispatch_ns = int(context.get("v12_preconfirm_dispatch_ns") or 0)
        launch_received_ns = int(
            context.get("create_received_ns")
            or context.get("last_received_ns")
            or request_created_ns
        )
        # Existing source/event clocks may be logical/replay timestamps. When a
        # true pre-confirm signal exists, use the real dispatcher wall clock for
        # build/sign/broadcast latency accounting instead.
        decision_completed_ns = int(
            preconfirm_dispatch_ns
            or context.get("v10_decision_completed_ns")
            or context.get("v12_role_model_entry_ns")
            or request_created_ns
        )
        runtime.latency.begin(
            request_id,
            mint=mint,
            launch_received_ns=launch_received_ns,
            decision_completed_ns=decision_completed_ns,
        )

        slippage_bps = direct_copy_slippage_bps(self.settings)
        _record_copy_terms(
            mint,
            observed_sol=observed_sol,
            submitted_sol=amount,
            slippage_bps=slippage_bps,
            exact_amount=exact_amount,
            source_signature=str(getattr(source, "signature", "") or ""),
        )
        context["v12_direct_copy_request_created_ns"] = request_created_ns

        request = {
            "request_id": request_id,
            "side": "BUY",
            "mint": mint,
            "public_key": self.signer.wallet,
            "amount": amount,
            "denominated_in_sol": True,
            "slippage_bps": slippage_bps,
            "priority_fee_sol": priority,
            "tip_sol": tip,
            "pool": "pump",
            "metadata": {
                "score": score,
                "reason": reason,
                "fdv_usd": state.fdv_usd,
                "launch_received_ns": launch_received_ns,
                "decision_completed_ns": decision_completed_ns,
                "e4_direct_copy": True,
                "e4_source_signature": str(getattr(source, "signature", "") or ""),
                "e4_observed_entry_sol": observed_sol,
                "e4_observed_entry_price_sol": max(
                    0.0, _finite(getattr(source, "entry_price_sol", 0.0))
                ),
                "e4_copy_exact_amount": exact_amount,
                "e4_copy_slippage_bps": slippage_bps,
                "e4_source_already_exited": bool(getattr(source, "fully_exited", False)),
                "e4_preconfirm_seen_ns": preconfirm_seen_ns,
                "e4_preconfirm_dispatch_ns": preconfirm_dispatch_ns,
                "e4_preconfirm_instruction_family": str(
                    context.get("v12_preconfirm_instruction_family") or ""
                ),
                "e4_preconfirm_exact_spend": bool(
                    context.get("v12_preconfirm_exact_spend", False)
                ),
                "e4_direct_copy_request_created_ns": request_created_ns,
            },
        }
        self.store.order(request_id, mint, "BUY", amount, None, reason)
        signature, confirmed, _, error = await self.execute(request_id, request)

        if cache is not None:
            cache.apply_estimated_delta(-reserved)
        if not confirmed:
            LOGGER.error(
                "E4 V12 direct-copy submission failed mint=%s source_signature=%s "
                "copy_signature=%s error=%s",
                mint,
                getattr(source, "signature", ""),
                signature,
                error,
            )
            if cache is not None:
                try:
                    await cache.refresh()
                except Exception:
                    pass
            return

        after_tokens = await v10.final._token_balance_after_change(
            self.rpc,
            self.signer.wallet,
            mint,
            0.0,
            "up",
        )
        received = max(0.0, after_tokens)
        if received <= 0:
            raise RuntimeError(
                "E4 V12 direct copy landed but token balance did not become observable"
            )

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
        LOGGER.info(
            "E4 V12 direct copy opened mint=%s e4_sol=%.9f submitted_sol=%.9f "
            "exact_amount=%s slippage_bps=%d preconfirm=%s signature=%s",
            mint,
            observed_sol,
            amount,
            exact_amount,
            slippage_bps,
            bool(preconfirm_seen_ns),
            signature,
        )
    except Exception:
        LOGGER.exception("E4 V12 direct-copy execution error mint=%s", mint)
    finally:
        if reserved:
            async with self.allocation_lock:
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
        self.pending_entries.discard(mint)


core.Engine.execute_buy = _execute_buy_direct_copy_v12


_PREVIOUS_PROCESS_E4_TRANSACTION = pipeline_runtime.PipelineRuntime.process_e4_transaction


def _account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    result: list[str] = []
    for item in message.get("accountKeys") or []:
        result.append(
            str(item.get("pubkey")) if isinstance(item, Mapping) else str(item)
        )
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    result.extend(str(item) for item in loaded.get("writable") or [])
    result.extend(str(item) for item in loaded.get("readonly") or [])
    return result


def _process_e4_transaction_direct_copy_v12(
    self: Any,
    signature: str,
    tx: Mapping[str, Any],
) -> None:
    _PREVIOUS_PROCESS_E4_TRANSACTION(self, signature, tx)

    meta = tx.get("meta") or {}
    keys = _account_keys(tx)
    if role_model.E4_WALLET not in keys:
        return
    wallet_index = keys.index(role_model.E4_WALLET)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if wallet_index >= len(pre) or wallet_index >= len(post):
        return
    sol_cost = max(
        0.0,
        (float(pre[wallet_index]) - float(post[wallet_index]))
        / core.LAMPORTS_PER_SOL,
    )
    if sol_cost <= 0:
        return

    lock = getattr(PIPELINES, "_lock", None)
    if lock is None:
        current = dict(getattr(PIPELINES, "_e4_entries", {}))
        changed = False
        for mint, signal in tuple(current.items()):
            if (
                str(getattr(signal, "signature", "") or "") == str(signature)
                and not bool(getattr(signal, "fully_exited", False))
                and _finite(getattr(signal, "entry_sol", 0.0)) <= 0
            ):
                current[mint] = replace(signal, entry_sol=sol_cost)
                changed = True
        if changed:
            PIPELINES._e4_entries = MappingProxyType(current)
        return

    with lock:
        current = dict(getattr(PIPELINES, "_e4_entries", {}))
        changed = False
        for mint, signal in tuple(current.items()):
            if (
                str(getattr(signal, "signature", "") or "") == str(signature)
                and not bool(getattr(signal, "fully_exited", False))
                and _finite(getattr(signal, "entry_sol", 0.0)) <= 0
            ):
                current[mint] = replace(signal, entry_sol=sol_cost)
                changed = True
        if changed:
            PIPELINES._e4_entries = MappingProxyType(current)


pipeline_runtime.PipelineRuntime.process_e4_transaction = (
    _process_e4_transaction_direct_copy_v12
)
