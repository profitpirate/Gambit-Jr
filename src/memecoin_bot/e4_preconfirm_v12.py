from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import aiohttp

from . import e4_direct_copy_v12 as direct
from . import e4_pipeline_runtime_v10 as pipeline_runtime
from . import e4_role_model_v12 as role_model

core = role_model.core
v6 = role_model.v6
PIPELINES = role_model.PIPELINES
LOGGER = logging.getLogger("gambit.e4.preconfirm.v12")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
NATIVE_MINT = "So11111111111111111111111111111111111111112"

# Official Pump IDL discriminators. Exact-input variants are the only
# instruction families that can be hard-authority sized before execution: the
# spend is encoded in the transaction itself rather than inferred from the
# post-trade balance delta.
BUY = bytes((102, 6, 61, 18, 1, 218, 235, 234))
BUY_EXACT_SOL_IN = bytes((56, 252, 116, 8, 158, 223, 205, 95))
BUY_EXACT_QUOTE_IN_V2 = bytes((194, 171, 28, 70, 104, 77, 91, 47))
BUY_V2 = bytes((184, 23, 238, 97, 103, 197, 211, 61))

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {character: index for index, character in enumerate(_B58_ALPHABET)}


def policy_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assert_policy_fingerprint(expected: str) -> None:
    expected = str(expected or "").strip().lower()
    actual = policy_fingerprint()
    if not expected or expected != actual:
        raise RuntimeError(
            "E4 V12 preconfirm policy fingerprint mismatch "
            f"expected={expected or '<missing>'} actual={actual}"
        )


def _b58decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    number = 0
    for character in text:
        if character not in _B58_INDEX:
            raise ValueError("invalid base58")
        number = number * 58 + _B58_INDEX[character]
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + decoded


def _instruction_data(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        try:
            return bytes(int(item) & 0xFF for item in value)
        except (TypeError, ValueError):
            return b""
    text = str(value or "").strip()
    if not text:
        return b""
    try:
        return _b58decode(text)
    except ValueError:
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            return b""


def _message(transaction: Mapping[str, Any]) -> Mapping[str, Any]:
    direct_message = transaction.get("message")
    if isinstance(direct_message, Mapping):
        return direct_message
    nested = transaction.get("transaction")
    if isinstance(nested, Mapping) and isinstance(nested.get("message"), Mapping):
        return nested["message"]
    return {}


def _account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = _message(transaction)
    result: list[str] = []
    for item in message.get("accountKeys") or []:
        result.append(str(item.get("pubkey") or "") if isinstance(item, Mapping) else str(item))
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    result.extend(str(item) for item in loaded.get("writable") or [])
    result.extend(str(item) for item in loaded.get("readonly") or [])
    return result


def _resolve_account(value: Any, keys: Sequence[str]) -> str:
    if isinstance(value, int):
        return str(keys[value]) if 0 <= value < len(keys) else ""
    if isinstance(value, Mapping):
        if value.get("pubkey"):
            return str(value["pubkey"])
        value = value.get("index")
        if isinstance(value, int) and 0 <= value < len(keys):
            return str(keys[value])
        return ""
    text = str(value or "")
    if text.isdigit():
        index = int(text)
        if 0 <= index < len(keys):
            return str(keys[index])
    return text


def _instruction_accounts(instruction: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    return [_resolve_account(item, keys) for item in instruction.get("accounts") or []]


def _program_id(instruction: Mapping[str, Any], keys: Sequence[str]) -> str:
    value = instruction.get("programId") or instruction.get("program_id")
    if value:
        return str(value)
    index = instruction.get("programIdIndex")
    return _resolve_account(index, keys) if index is not None else ""


def _u64(data: bytes, offset: int) -> int:
    if len(data) < offset + 8:
        return 0
    return int(struct.unpack_from("<Q", data, offset)[0])


@dataclass(frozen=True, slots=True)
class E4InstructionIntent:
    mint: str
    signature: str
    received_ns: int
    instruction_family: str
    spend_sol: float
    exact_spend: bool
    token_target: float
    spend_ceiling_sol: float
    source: str = "e4-preconfirm-instruction-v12"


def _decode_instruction(
    instruction: Mapping[str, Any],
    keys: Sequence[str],
    *,
    signature: str,
    received_ns: int,
) -> E4InstructionIntent | None:
    if _program_id(instruction, keys) != PUMP_PROGRAM:
        return None
    data = _instruction_data(instruction.get("data"))
    if len(data) < 24:
        return None
    discriminator = data[:8]
    accounts = _instruction_accounts(instruction, keys)

    if discriminator in {BUY, BUY_EXACT_SOL_IN}:
        if len(accounts) <= 6 or accounts[6] != role_model.E4_WALLET:
            return None
        mint = accounts[2]
        if not mint:
            return None
        first = _u64(data, 8)
        second = _u64(data, 16)
        if discriminator == BUY_EXACT_SOL_IN:
            return E4InstructionIntent(
                mint=mint,
                signature=signature,
                received_ns=received_ns,
                instruction_family="buy_exact_sol_in",
                spend_sol=first / core.LAMPORTS_PER_SOL,
                exact_spend=True,
                token_target=second / 1_000_000.0,
                spend_ceiling_sol=first / core.LAMPORTS_PER_SOL,
            )
        return E4InstructionIntent(
            mint=mint,
            signature=signature,
            received_ns=received_ns,
            instruction_family="buy",
            spend_sol=0.0,
            exact_spend=False,
            token_target=first / 1_000_000.0,
            spend_ceiling_sol=second / core.LAMPORTS_PER_SOL,
        )

    if discriminator in {BUY_V2, BUY_EXACT_QUOTE_IN_V2}:
        # buy_v2 / buy_exact_quote_in_v2 use base mint at 1, quote mint at 2,
        # and signer/user at 13 in the current unified Pump V2 interface.
        if len(accounts) <= 13 or accounts[13] != role_model.E4_WALLET:
            return None
        mint = accounts[1]
        quote_mint = accounts[2]
        if not mint or quote_mint != NATIVE_MINT:
            return None
        first = _u64(data, 8)
        second = _u64(data, 16)
        if discriminator == BUY_EXACT_QUOTE_IN_V2:
            return E4InstructionIntent(
                mint=mint,
                signature=signature,
                received_ns=received_ns,
                instruction_family="buy_exact_quote_in_v2",
                spend_sol=first / core.LAMPORTS_PER_SOL,
                exact_spend=True,
                token_target=second / 1_000_000.0,
                spend_ceiling_sol=first / core.LAMPORTS_PER_SOL,
            )
        return E4InstructionIntent(
            mint=mint,
            signature=signature,
            received_ns=received_ns,
            instruction_family="buy_v2",
            spend_sol=0.0,
            exact_spend=False,
            token_target=first / 1_000_000.0,
            spend_ceiling_sol=second / core.LAMPORTS_PER_SOL,
        )
    return None


def decode_e4_buy_intents(
    signature: str,
    transaction: Mapping[str, Any],
    *,
    received_ns: int | None = None,
) -> list[E4InstructionIntent]:
    keys = _account_keys(transaction)
    if role_model.E4_WALLET not in keys:
        return []
    now = int(received_ns or time.time_ns())
    message = _message(transaction)
    result: list[E4InstructionIntent] = []
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, Mapping):
            continue
        decoded = _decode_instruction(
            instruction,
            keys,
            signature=str(signature or ""),
            received_ns=now,
        )
        if decoded is not None:
            result.append(decoded)
    return result


def _context_for_intent(intent: E4InstructionIntent) -> dict[str, Any]:
    context = v6._CONTEXT_BY_MINT.setdefault(intent.mint, {})
    context.update(
        {
            "v12_preconfirm_seen_ns": intent.received_ns,
            "v12_preconfirm_signature": intent.signature,
            "v12_preconfirm_instruction_family": intent.instruction_family,
            "v12_preconfirm_exact_spend": intent.exact_spend,
            "v12_preconfirm_spend_sol": intent.spend_sol,
            "v12_preconfirm_spend_ceiling_sol": intent.spend_ceiling_sol,
            "v12_preconfirm_token_target": intent.token_target,
        }
    )
    return context


def observe_intent(intent: E4InstructionIntent, *, logical_ns: int | None = None):
    context = _context_for_intent(intent)
    if not intent.exact_spend or intent.spend_sol <= 0:
        context["v12_preconfirm_observational_only"] = True
        return None
    observed_ns = int(
        logical_ns
        or context.get("last_received_ns")
        or context.get("create_received_ns")
        or intent.received_ns
    )
    learning = getattr(PIPELINES, "_learning", {}).get(intent.mint)
    creator = str(getattr(learning, "creator", "") or context.get("creator") or "")
    price = float(getattr(learning, "latest_price_sol", 0.0) or context.get("price_sol") or 0.0)
    signal = PIPELINES.observe_e4_entry(
        {
            "kind": "e4_buy",
            "mint": intent.mint,
            "creator": creator,
            "observed_ns": observed_ns,
            "entry_price_sol": price,
            "entry_sol": intent.spend_sol,
            "signature": intent.signature,
            "source": intent.source,
        }
    )
    context["v12_role_model_entry_ns"] = observed_ns
    context["v12_preconfirm_authorized"] = signal is not None
    return signal


class PreconfirmDispatcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen: set[tuple[str, str]] = set()
        self.dispatched = 0
        self.executed = 0
        self.rejected = 0
        self.no_state = 0

    def bind(self, engine: Any, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._engine = engine
            self._loop = loop

    def unbind(self, engine: Any) -> None:
        with self._lock:
            if self._engine is engine:
                self._engine = None
                self._loop = None

    def submit(self, intent: E4InstructionIntent) -> bool:
        key = (intent.signature, intent.mint)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > 4096:
                self._seen = set(tuple(self._seen)[-2048:])
            engine = self._engine
            loop = self._loop
        if engine is None or loop is None or not loop.is_running():
            observe_intent(intent)
            return False
        asyncio.run_coroutine_threadsafe(self._execute(engine, intent), loop)
        self.dispatched += 1
        return True

    async def _execute(self, engine: Any, intent: E4InstructionIntent) -> None:
        state = engine.tokens.get(intent.mint)
        if state is None or state.created_ns is None:
            self.no_state += 1
            observe_intent(intent)
            return
        observe_intent(intent, logical_ns=int(state.latest_ns or state.created_ns))
        if not intent.exact_spend:
            self.rejected += 1
            return
        if intent.mint in engine.pending_entries or engine.store.has_entered(intent.mint):
            self.rejected += 1
            return
        if len(engine.positions) + len(engine.pending_entries) >= 2:
            self.rejected += 1
            return
        accepted, score, fraction, reason, features = engine.policy.entry(state)
        profile = v6._PROFILE_BY_MINT.get(intent.mint)
        if (
            not accepted
            or profile is None
            or str(getattr(profile, "family", "") or "") != direct.DIRECT_COPY_FAMILY
        ):
            self.rejected += 1
            return
        reason = f"{reason}; V12 preconfirm {intent.instruction_family}"
        engine.store.decision(
            intent.mint,
            None,
            "BUY",
            score,
            reason,
            {
                "fraction": fraction,
                "features": features,
                "preconfirm": True,
                "source_signature": intent.signature,
                "source_spend_sol": intent.spend_sol,
            },
        )
        engine.pending_entries.add(intent.mint)
        self.executed += 1
        await engine.execute_buy(state, score, fraction, reason)


DISPATCHER = PreconfirmDispatcher()


def submit_intents(signature: str, transaction: Mapping[str, Any], *, received_ns: int | None = None) -> int:
    count = 0
    for intent in decode_e4_buy_intents(signature, transaction, received_ns=received_ns):
        _context_for_intent(intent)
        if intent.exact_spend and intent.spend_sol > 0:
            DISPATCHER.submit(intent)
            count += 1
    return count


# Preparse any full transaction before the post-balance observer runs. On an
# enhanced processed stream this removes local post-state parsing latency; on a
# true pre-confirm/shred bridge the same function runs before inclusion.
_PREVIOUS_PROCESS_E4_TRANSACTION = pipeline_runtime.PipelineRuntime.process_e4_transaction


def _token_totals(rows: Sequence[Mapping[str, Any]], owner: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows or []:
        if str(row.get("owner") or "") != owner:
            continue
        mint = str(row.get("mint") or "")
        amount = row.get("uiTokenAmount") or {}
        value = amount.get("uiAmountString", amount.get("uiAmount", 0))
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0.0
        if mint:
            totals[mint] = totals.get(mint, 0.0) + number
    return totals


def _process_e4_transaction_preconfirm_v12(self: Any, signature: str, tx: Mapping[str, Any]) -> None:
    submit_intents(signature, tx, received_ns=time.time_ns())
    _PREVIOUS_PROCESS_E4_TRANSACTION(self, signature, tx)

    # If a pre-confirm exact-input signal existed before post balances were
    # available, enrich it with the real token delta after execution so later
    # partial exits mirror cumulative E4 token percentages exactly.
    meta = tx.get("meta") or {}
    pre = _token_totals(list(meta.get("preTokenBalances") or []), role_model.E4_WALLET)
    post = _token_totals(list(meta.get("postTokenBalances") or []), role_model.E4_WALLET)
    updates: dict[str, float] = {}
    for mint in set(pre) | set(post):
        delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if delta > max(1e-8, abs(pre.get(mint, 0.0)) * 1e-10):
            updates[mint] = delta
    if not updates:
        return
    lock = getattr(PIPELINES, "_lock", None)
    if lock is None:
        current = dict(getattr(PIPELINES, "_e4_entries", {}))
        changed = False
        for mint, tokens in updates.items():
            signal = current.get(mint)
            if (
                signal is not None
                and str(getattr(signal, "signature", "") or "") == str(signature)
                and float(getattr(signal, "entry_tokens", 0.0) or 0.0) <= 0
            ):
                current[mint] = replace(signal, entry_tokens=tokens, remaining_tokens=tokens)
                changed = True
        if changed:
            PIPELINES._e4_entries = MappingProxyType(current)
        return
    with lock:
        current = dict(getattr(PIPELINES, "_e4_entries", {}))
        changed = False
        for mint, tokens in updates.items():
            signal = current.get(mint)
            if (
                signal is not None
                and str(getattr(signal, "signature", "") or "") == str(signature)
                and float(getattr(signal, "entry_tokens", 0.0) or 0.0) <= 0
            ):
                current[mint] = replace(signal, entry_tokens=tokens, remaining_tokens=tokens)
                changed = True
        if changed:
            PIPELINES._e4_entries = MappingProxyType(current)


pipeline_runtime.PipelineRuntime.process_e4_transaction = _process_e4_transaction_preconfirm_v12


# Canonical JSON bridge for a provider/ShredStream decoder. The raw AllenHark
# shred protocol is proprietary; V12 therefore consumes a decoded transaction
# bridge rather than embedding an unverifiable private wire format. Each URL may
# be a passive WS feed. If E4_PRECONFIRM_SUBSCRIBE_JSON is set, that JSON object
# is sent immediately after connect for provider-specific subscriptions.
async def _preconfirm_ws_worker(self: Any, url: str) -> None:
    assert self.session is not None
    subscribe_raw = os.getenv("E4_PRECONFIRM_SUBSCRIBE_JSON", "").strip()
    while not self.stop_event.is_set():
        try:
            async with self.session.ws_connect(url, heartbeat=10, max_msg_size=16 * 1024 * 1024) as ws:
                if subscribe_raw:
                    await ws.send_json(json.loads(subscribe_raw))
                async for message in ws:
                    if self.stop_event.is_set():
                        break
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        envelope = json.loads(message.data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(envelope, Mapping):
                        continue
                    value = ((envelope.get("params") or {}).get("result") or {}).get("value")
                    if not isinstance(value, Mapping):
                        value = envelope.get("value") if isinstance(envelope.get("value"), Mapping) else envelope
                    transaction = value.get("transaction") if isinstance(value.get("transaction"), Mapping) else value
                    if not isinstance(transaction, Mapping):
                        continue
                    signature = str(
                        value.get("signature")
                        or transaction.get("signature")
                        or (transaction.get("signatures") or [""])[0]
                        or ""
                    )
                    if signature:
                        submit_intents(signature, transaction, received_ns=time.time_ns())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record_error(exc)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.10)
            except asyncio.TimeoutError:
                pass


_PREVIOUS_RUNTIME_RUN = pipeline_runtime.PipelineRuntime.run


async def _runtime_run_preconfirm_v12(self: Any) -> None:
    # Keep the original supervisor untouched when no pre-confirm bridge is
    # configured. With a bridge, run it alongside the normal runtime using a
    # small companion task injected through a temporary worker-list hook.
    urls = tuple(
        dict.fromkeys(
            part.strip()
            for part in os.getenv("E4_PRECONFIRM_TRANSACTION_WS_URLS", "").split(",")
            if part.strip()
        )
    )
    if not urls:
        await _PREVIOUS_RUNTIME_RUN(self)
        return

    original_transaction_urls = tuple(getattr(self, "transaction_ws_urls", ()))
    # The original run owns the ClientSession. Add bridge URLs as transaction
    # workers only via a wrapper that launches our passive/custom subscriber
    # once the session exists.
    async def companion() -> None:
        while self.session is None and not self.stop_event.is_set():
            await asyncio.sleep(0.002)
        if self.session is None:
            return
        tasks = [
            asyncio.create_task(_preconfirm_ws_worker(self, url), name=f"e4-v12-preconfirm-{index}")
            for index, url in enumerate(urls)
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    companion_task = asyncio.create_task(companion(), name="e4-v12-preconfirm-companion")
    try:
        await _PREVIOUS_RUNTIME_RUN(self)
    finally:
        companion_task.cancel()
        await asyncio.gather(companion_task, return_exceptions=True)
        self.transaction_ws_urls = original_transaction_urls


pipeline_runtime.PipelineRuntime.run = _runtime_run_preconfirm_v12


# External decoded ShredStream bridges can also send a canonical E4 preconfirm
# payload to the existing UDP ingress. This keeps vendor-specific decoding out
# of the funded process while preserving immediate engine dispatch.
_PREVIOUS_ACCEPT_SIGNAL = pipeline_runtime.PipelineRuntime.accept_signal


def _accept_signal_preconfirm_v12(self: Any, payload: Mapping[str, Any]) -> None:
    kind = str(payload.get("kind") or payload.get("type") or "").lower()
    if bool(payload.get("preconfirm")) and "e4" in kind and any(word in kind for word in ("buy", "entry", "trade")):
        try:
            spend = float(payload.get("entry_sol") or payload.get("spend_sol") or payload.get("sol_amount") or 0.0)
        except (TypeError, ValueError):
            spend = 0.0
        mint = str(payload.get("mint") or "")
        signature = str(payload.get("signature") or "")
        if mint and signature and spend > 0:
            intent = E4InstructionIntent(
                mint=mint,
                signature=signature,
                received_ns=int(payload.get("observed_ns") or payload.get("received_ns") or time.time_ns()),
                instruction_family=str(payload.get("instruction_family") or "decoded_bridge_exact_input"),
                spend_sol=spend,
                exact_spend=bool(payload.get("exact_spend", True)),
                token_target=float(payload.get("token_amount") or 0.0),
                spend_ceiling_sol=spend,
                source=str(payload.get("source") or "e4-preconfirm-decoded-bridge-v12"),
            )
            DISPATCHER.submit(intent)
            return
    _PREVIOUS_ACCEPT_SIGNAL(self, payload)


pipeline_runtime.PipelineRuntime.accept_signal = _accept_signal_preconfirm_v12


_PREVIOUS_ENGINE_RUN = core.Engine.run


async def _engine_run_preconfirm_v12(self: Any) -> None:
    DISPATCHER.bind(self, asyncio.get_running_loop())
    try:
        await _PREVIOUS_ENGINE_RUN(self)
    finally:
        DISPATCHER.unbind(self)


core.Engine.run = _engine_run_preconfirm_v12
