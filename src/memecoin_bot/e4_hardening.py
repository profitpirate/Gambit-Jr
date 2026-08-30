from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Mapping

from . import e4_final as final

core = final.core
LOGGER = logging.getLogger("gambit.e4.hardening")

# Pump.fun standard tokens use six decimals and a one-billion-token supply.
_DEFAULT_TOKEN_DECIMALS = int(os.getenv("E4_PUMP_TOKEN_DECIMALS", "6"))
_DEFAULT_SUPPLY_RAW = int(os.getenv("E4_PUMP_TOKEN_SUPPLY_RAW", "1000000000000000"))
_SOL_USD = max(1.0, float(os.getenv("E4_SOL_USD_FALLBACK", "150")))
_SOL_PRICE_UPDATED_NS = 0


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _timestamp_ns(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e17:
            return int(number)
        if number > 1e14:
            return int(number * 1_000)
        if number > 1e11:
            return int(number * 1_000_000)
        return int(number * 1_000_000_000)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        with suppress(ValueError):
            return _timestamp_ns(float(text))
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _normalized_price_sol(mapping: Mapping[str, Any]) -> float | None:
    explicit = _finite(_first(mapping, "price_sol", "token_price_sol"))
    if explicit and explicit > 0:
        return explicit
    virtual_quote = _finite(
        _first(
            mapping,
            "virtual_sol_reserves",
            "virtual_quote_reserves",
            "virtual_quote_reserve",
        )
    )
    virtual_base = _finite(
        _first(
            mapping,
            "virtual_token_reserves",
            "virtual_base_reserves",
            "virtual_base_reserve",
        )
    )
    if not virtual_quote or not virtual_base or virtual_quote <= 0 or virtual_base <= 0:
        return None
    decimals = int(
        _finite(_first(mapping, "token_decimals", "base_mint_decimals"))
        or _DEFAULT_TOKEN_DECIMALS
    )
    # Native Pump events expose lamports and raw token units. Provider-normalized
    # rows occasionally expose SOL and UI tokens, so detect the unit family.
    if virtual_quote >= 1_000_000 or virtual_base >= 10_000_000_000:
        return (virtual_quote / core.LAMPORTS_PER_SOL) / (virtual_base / (10**decimals))
    return virtual_quote / virtual_base


def _derived_fdv_usd(mapping: Mapping[str, Any], price_sol: float | None) -> float | None:
    explicit = _finite(_first(mapping, "fdv_usd", "market_cap_usd", "market_cap", "fdv"))
    if explicit and explicit > 0:
        return explicit
    if not price_sol or price_sol <= 0:
        return None
    supply = _finite(_first(mapping, "token_total_supply", "base_supply", "total_supply"))
    if not supply or supply <= 0:
        supply = float(_DEFAULT_SUPPLY_RAW)
    decimals = int(
        _finite(_first(mapping, "token_decimals", "base_mint_decimals"))
        or _DEFAULT_TOKEN_DECIMALS
    )
    supply_ui = supply / (10**decimals) if supply >= 10_000_000_000 else supply
    return price_sol * supply_ui * _SOL_USD


# ---------------------------------------------------------------------------
# Real V1.5 canonical journal compatibility
# ---------------------------------------------------------------------------

_previous_from_row = core.Event.from_row.__func__


def _canonical_from_row(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    merged: dict[str, Any] = dict(row)
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload"):
        for nested_key, value in _parse_json_mapping(merged.get(key)).items():
            merged.setdefault(str(nested_key), value)

    if "mint" not in merged:
        mint = _first(merged, "canonical_token", "token_mint", "token_address", "address")
        if mint:
            merged["mint"] = mint
    if "signature" not in merged and merged.get("transaction_signature"):
        merged["signature"] = merged["transaction_signature"]
    if "trader" not in merged:
        trader = _first(merged, "actor", "user", "wallet_address", "actor_address", "owner")
        if trader:
            merged["trader"] = trader

    kind = str(_first(merged, "event_type", "kind", "type", "action") or "").upper()
    side = str(_first(merged, "side", "trade_side") or "").upper()
    aliases = {
        "TOKEN_CREATED": "CREATE",
        "CREATE_EVENT": "CREATE",
        "WALLET_BUY": "BUY",
        "WALLET_SELL": "SELL",
        "TOKEN_TRADE": side if side in {"BUY", "SELL"} else "UNKNOWN",
        "WALLET_TRADE": side if side in {"BUY", "SELL"} else "UNKNOWN",
        "BONDING_CURVE_STATE": "CURVE",
        "BONDING_CURVE_PROGRESS": "CURVE",
        "CURVE_OBSERVATION": "CURVE",
        "MIGRATION_STARTED": "MIGRATION",
        "MIGRATION_COMPLETED": "MIGRATION",
        "POOL_CREATED": "MIGRATION",
        "LIQUIDITY_ADDED": "MIGRATION",
    }
    if kind in aliases:
        merged["event_type"] = aliases[kind]
    elif kind in {"TRADE", "PUMP_TRADE"} and side in {"BUY", "SELL"}:
        merged["event_type"] = side

    cursor = _first(merged, "_e4_cursor", "id", "sequence")
    try:
        merged["id"] = int(cursor)
    except (TypeError, ValueError):
        # Canonical event IDs are SHA-256 text. rowid is supplied by the source,
        # but retain a deterministic numeric fallback for isolated fixtures.
        merged["id"] = abs(hash(str(cursor or merged.get("event_id") or "0"))) % (2**63 - 1)

    if "source_timestamp_ns" not in merged:
        source_ns = _timestamp_ns(
            _first(
                merged,
                "source_timestamp",
                "source_event_timestamp",
                "observed_at",
                "block_time",
                "event_time",
                "timestamp",
            )
        )
        if source_ns is not None:
            merged["source_timestamp_ns"] = source_ns
    if "received_timestamp_ns" not in merged:
        received_ns = _timestamp_ns(
            _first(
                merged,
                "received_timestamp",
                "source_received_at",
                "received_at",
                "available_timestamp",
            )
        )
        if received_ns is not None:
            merged["received_timestamp_ns"] = received_ns

    price_sol = _normalized_price_sol(merged)
    fdv_usd = _derived_fdv_usd(merged, price_sol)
    if price_sol is not None:
        merged["price_sol"] = price_sol
    if fdv_usd is not None:
        merged["fdv_usd"] = fdv_usd
    if "complete" not in merged and "curve_complete" in merged:
        merged["complete"] = merged["curve_complete"]
    return _previous_from_row(cls, merged)


core.Event.from_row = classmethod(_canonical_from_row)


# V1.5 intentionally projects one Pump trade into TOKEN_TRADE plus WALLET_BUY/SELL.
# They are separate analytical facts but the E4 flow state must count the economic
# trade once, otherwise inflow and sell pressure are doubled.
_previous_state_apply = core.TokenState.apply
_STATE_EVENT_KEYS: dict[str, tuple[set[tuple[Any, ...]], list[tuple[Any, ...]]]] = {}


def _deduplicating_state_apply(
    self: core.TokenState, event: core.Event, wallet: str | None
) -> None:
    if event.signature and event.kind in {
        core.EventKind.BUY,
        core.EventKind.SELL,
        core.EventKind.PUMPSWAP_BUY,
        core.EventKind.PUMPSWAP_SELL,
    }:
        seen, order = _STATE_EVENT_KEYS.setdefault(self.mint, (set(), []))
        side = (
            "BUY"
            if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}
            else "SELL"
        )
        key = (
            event.signature,
            side,
            event.trader,
            round(event.sol_amount, 12),
            round(event.token_amount, 6),
        )
        if key in seen:
            return
        seen.add(key)
        order.append(key)
        if len(order) > 2048:
            seen.discard(order.pop(0))
    _previous_state_apply(self, event, wallet)


core.TokenState.apply = _deduplicating_state_apply


def _discover_real_journal(self: core.SQLiteEventSource) -> tuple[str, str]:
    connection = self._connect()
    requested = os.getenv("E4_EVENT_TABLE", "").strip()
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    table = requested or ("canonical_events" if "canonical_events" in tables else "")
    if table and table not in tables:
        raise RuntimeError(f"E4_EVENT_TABLE {table!r} does not exist")
    if not table:
        candidates: list[tuple[int, str]] = []
        for name in tables:
            columns = {
                str(row[1]).lower()
                for row in connection.execute(f'PRAGMA table_info("{name}")')
            }
            has_mint = bool(
                columns.intersection({"mint", "canonical_token", "token_address", "address"})
            )
            has_kind = bool(columns.intersection({"event_type", "kind", "type", "action"}))
            if has_mint and has_kind:
                candidates.append(
                    ((100 if "canonical" in name.lower() else 0) + len(columns), name)
                )
        if not candidates:
            raise RuntimeError("No V1.5 canonical event journal found")
        table = max(candidates)[1]

    columns = {
        str(row[1]).lower(): str(row[2]).upper()
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    requested_id = os.getenv("E4_EVENT_ID_COLUMN", "").strip().lower()
    if requested_id:
        if requested_id not in columns and requested_id != "rowid":
            raise RuntimeError(
                f"E4_EVENT_ID_COLUMN {requested_id!r} is not present in {table!r}"
            )
        cursor_column = requested_id
    elif "id" in columns and "INT" in columns["id"]:
        cursor_column = "id"
    elif "sequence" in columns and "INT" in columns["sequence"]:
        cursor_column = "sequence"
    else:
        cursor_column = "rowid"

    self.table = table
    self.id_column = cursor_column
    if not getattr(self, "_e4_hardened_tail_initialized", False):
        if not core._bool("E4_CONSUME_EXISTING_EVENTS", False):
            row = connection.execute(
                f'SELECT COALESCE(MAX({cursor_column}),0) FROM "{table}"'
            ).fetchone()
            self.last_id = max(self.last_id, int(row[0] or 0))
        self._e4_hardened_tail_initialized = True
    return table, cursor_column


def _read_real_journal(self: core.SQLiteEventSource) -> list[sqlite3.Row]:
    connection = self._connect()
    table, cursor_column = (
        (self.table, self.id_column)
        if self.table and self.id_column
        else self._discover()
    )
    assert table and cursor_column
    if cursor_column == "rowid":
        query = (
            f'SELECT rowid AS _e4_cursor,* FROM "{table}" '
            "WHERE rowid>? ORDER BY rowid LIMIT 500"
        )
    else:
        query = (
            f'SELECT "{cursor_column}" AS _e4_cursor,* FROM "{table}" '
            f'WHERE "{cursor_column}">? ORDER BY "{cursor_column}" LIMIT 500'
        )
    return list(connection.execute(query, (self.last_id,)))


core.SQLiteEventSource._discover = _discover_real_journal
core.SQLiteEventSource._read = _read_real_journal


# ---------------------------------------------------------------------------
# Safe successful-entry accounting
# ---------------------------------------------------------------------------


def _has_entered(self: core.Store, mint: str) -> bool:
    row = self.conn.execute(
        "SELECT entry_count,last_action FROM e4_seen_mints WHERE mint=?", (mint,)
    ).fetchone()
    return bool(row and (int(row[0]) >= 1 or str(row[1]) == "BUY_UNCERTAIN"))


def _reserve_entry(self: core.Store, mint: str, score: float, reason: str) -> bool:
    self.conn.execute("BEGIN IMMEDIATE")
    try:
        row = self.conn.execute(
            "SELECT entry_count,last_action FROM e4_seen_mints WHERE mint=?", (mint,)
        ).fetchone()
        if row and (int(row[0]) >= 1 or str(row[1]) == "BUY_UNCERTAIN"):
            self.conn.execute("ROLLBACK")
            return False
        now = time.time_ns()
        if row:
            self.conn.execute(
                "UPDATE e4_seen_mints SET first_seen_ns=?,entry_count=0,"
                "last_action='BUY_PENDING',last_reason=?,last_score=? WHERE mint=?",
                (now, reason, score, mint),
            )
        else:
            self.conn.execute(
                "INSERT INTO e4_seen_mints VALUES(?,?,?,?,?,?)",
                (mint, now, 0, "BUY_PENDING", reason, score),
            )
        self.conn.execute("COMMIT")
        return True
    except Exception:
        self.conn.execute("ROLLBACK")
        raise


_previous_receipt = core.Store.receipt


def _receipt_with_entry_state(
    self: core.Store,
    request_id: str,
    signature: str,
    route: str,
    confirmed: bool,
    slot: int | None,
    error: str | None,
    results: Mapping[str, str],
) -> None:
    _previous_receipt(self, request_id, signature, route, confirmed, slot, error, results)
    row = self.conn.execute(
        "SELECT mint,side FROM e4_orders WHERE request_id=?", (request_id,)
    ).fetchone()
    if not row or str(row["side"]).upper() != "BUY" or not row["mint"]:
        return
    mint = str(row["mint"])
    if confirmed:
        self.conn.execute(
            "UPDATE e4_seen_mints SET entry_count=1,last_action='BUY_CONFIRMED',"
            "last_reason='confirmed on chain' WHERE mint=?",
            (mint,),
        )
        return
    message = str(error or "").lower()
    any_accepted = any(
        value
        and not any(
            term in str(value).lower()
            for term in ("reject", "error", "http", "failed")
        )
        for value in results.values()
    )
    uncertain = any_accepted or "timeout" in message or "status" in message
    self.conn.execute(
        "UPDATE e4_seen_mints SET entry_count=0,last_action=?,last_reason=? WHERE mint=?",
        (
            "BUY_UNCERTAIN" if uncertain else "BUY_RETRYABLE",
            error or (
                "accepted but confirmation unknown" if uncertain else "submission rejected"
            ),
            mint,
        ),
    )


core.Store.has_entered = _has_entered
core.Store.mark_entry = _reserve_entry
core.Store.receipt = _receipt_with_entry_state


# ---------------------------------------------------------------------------
# Transaction-meta reconciliation removes RPC-observation races
# ---------------------------------------------------------------------------


async def _transaction_meta(
    rpc: core.Rpc, signature: str, timeout: float = 4.0
) -> Mapping[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = await rpc.call(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if isinstance(result, Mapping):
                return result
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return None


def _account_keys(transaction: Mapping[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys: list[str] = []
    for value in message.get("accountKeys") or []:
        keys.append(str(value.get("pubkey")) if isinstance(value, Mapping) else str(value))
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(value) for value in loaded.get("writable") or [])
    keys.extend(str(value) for value in loaded.get("readonly") or [])
    return keys


def _meta_deltas(
    transaction: Mapping[str, Any], wallet: str, mint: str
) -> tuple[float | None, float | None]:
    meta = transaction.get("meta") or {}
    keys = _account_keys(transaction)
    sol_delta: float | None = None
    if wallet in keys:
        index = keys.index(wallet)
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if index < len(pre) and index < len(post):
            sol_delta = (float(post[index]) - float(pre[index])) / core.LAMPORTS_PER_SOL

    def token_total(rows: Any) -> float:
        total = 0.0
        for item in rows or []:
            if str(item.get("mint") or "") != mint:
                continue
            owner = str(item.get("owner") or "")
            if owner != wallet:
                continue
            token = item.get("uiTokenAmount") or {}
            value = token.get("uiAmountString", token.get("uiAmount", 0))
            total += float(value or 0)
        return total

    token_delta = token_total(meta.get("postTokenBalances")) - token_total(
        meta.get("preTokenBalances")
    )
    return sol_delta, token_delta


async def _create_position(
    engine: core.Engine,
    state: core.TokenState,
    amount: float,
    execution_cost: float,
    before_tokens: float,
    signature: str,
    opened_ns: int,
) -> core.Position | None:
    balance_task = asyncio.create_task(
        final._token_balance_after_change(
            engine.rpc,
            engine.signer.wallet,
            state.mint,
            before_tokens,
            "up",
            timeout=2.5,
        )
    )
    transaction = await _transaction_meta(engine.rpc, signature)
    after_tokens = await balance_task
    meta_sol, meta_token = (
        _meta_deltas(transaction, engine.signer.wallet, state.mint)
        if transaction
        else (None, None)
    )
    received = max(0.0, after_tokens - before_tokens, float(meta_token or 0.0))
    if received <= 0:
        return None
    trade_entry_price = amount / received
    actual_spend = (
        -meta_sol if meta_sol is not None and meta_sol < 0 else amount + execution_cost
    )
    position = core.Position(
        position_id=str(uuid.uuid4()),
        mint=state.mint,
        status=core.PositionStatus.OPEN,
        opened_ns=opened_ns,
        entry_sol=max(amount, actual_spend),
        tokens=received,
        remaining=received,
        entry_price=trade_entry_price,
        max_price=state.price_sol or trade_entry_price,
        last_price=state.price_sol or trade_entry_price,
        entry_signature=signature,
    )
    engine.positions[state.mint] = position
    engine.store.save_position(position)
    return position


async def _recover_confirmed_buy(
    engine: core.Engine,
    state: core.TokenState,
    amount: float,
    execution_cost: float,
    before_tokens: float,
    signature: str,
    opened_ns: int,
) -> None:
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and state.mint not in engine.positions:
        position = await _create_position(
            engine,
            state,
            amount,
            execution_cost,
            before_tokens,
            signature,
            opened_ns,
        )
        if position:
            LOGGER.warning("Recovered delayed confirmed E4 buy mint=%s", state.mint)
            return
        await asyncio.sleep(0.1)
    LOGGER.critical(
        "Confirmed E4 buy could not be reconciled mint=%s signature=%s",
        state.mint,
        signature,
    )


async def _execute_buy_hardened(
    self: core.Engine,
    state: core.TokenState,
    score: float,
    fraction: float,
    reason: str,
) -> None:
    mint = state.mint
    reserved = 0.0
    opened_ns = time.time_ns()
    try:
        if self.store.has_entered(mint):
            return
        before_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
        async with self.allocation_lock:
            balance = await self.rpc.balance(self.signer.wallet)
            fraction = min(max(0.0, fraction), self.settings.max_position_fraction)
            priority, tip = self.fee_bid(balance * fraction, score)
            available = (
                balance
                - self.settings.reserve_sol
                - self.reserved_sol
                - priority
                - tip
            )
            amount = min(
                max(0.0, available) * fraction,
                self.settings.max_position_sol,
            )
            if amount < self.settings.min_position_sol:
                return
            if (
                amount
                + priority
                + tip
                + self.settings.reserve_sol
                + self.reserved_sol
                > balance + 1e-12
            ):
                amount = max(
                    0.0,
                    balance
                    - self.settings.reserve_sol
                    - self.reserved_sol
                    - priority
                    - tip,
                )
            if amount < self.settings.min_position_sol:
                return
            reserved = amount + priority + tip
            self.reserved_sol += reserved
            if not self.store.mark_entry(mint, score, reason):
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
                reserved = 0.0
                return

        request_id = str(uuid.uuid4())
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
            },
        }
        self.store.order(request_id, mint, "BUY", amount, None, reason)
        signature, confirmed, _, error = await self.execute(request_id, request)
        if not confirmed:
            LOGGER.error(
                "E4 buy not confirmed mint=%s signature=%s error=%s",
                mint,
                signature,
                error,
            )
            return
        position = await _create_position(
            self,
            state,
            amount,
            priority + tip,
            before_tokens,
            signature,
            opened_ns,
        )
        if position is None:
            self.spawn(
                _recover_confirmed_buy(
                    self,
                    state,
                    amount,
                    priority + tip,
                    before_tokens,
                    signature,
                    opened_ns,
                )
            )
            return
        LOGGER.info(
            "E4 position opened mint=%s amount_sol=%.9f signature=%s",
            mint,
            amount,
            signature,
        )
    except Exception:
        LOGGER.exception("E4 buy execution error mint=%s", mint)
    finally:
        if reserved:
            async with self.allocation_lock:
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
        self.pending_entries.discard(mint)


async def _recover_confirmed_sell(
    engine: core.Engine,
    position: core.Position,
    live_tokens: float,
    fraction: float,
    signature: str,
    before_sol: float,
) -> None:
    mint = position.mint
    try:
        transaction = await _transaction_meta(engine.rpc, signature, timeout=10.0)
        after_tokens = await final._token_balance_after_change(
            engine.rpc,
            engine.signer.wallet,
            mint,
            live_tokens,
            "down",
            timeout=10.0,
        )
        after_sol = await engine.rpc.balance(engine.signer.wallet)
        meta_sol, meta_token = (
            _meta_deltas(transaction, engine.signer.wallet, mint)
            if transaction
            else (None, None)
        )
        sold = max(
            0.0,
            live_tokens - after_tokens,
            -float(meta_token or 0.0),
        )
        if sold <= 0:
            LOGGER.critical(
                "Confirmed E4 sell could not be reconciled mint=%s signature=%s",
                mint,
                signature,
            )
            position.status = core.PositionStatus.EXITING
            engine.store.save_position(position)
            return
        current_balance = min(after_tokens, max(0.0, live_tokens - sold))
        position.remaining = min(
            max(0.0, position.remaining - sold),
            current_balance,
        )
        position.realized_sol += (
            meta_sol if meta_sol is not None else after_sol - before_sol
        )
        position.close_signature = signature
        if not position.first_partial_done and fraction < 0.999:
            position.first_partial_done = True
            position.first_partial_fraction = (
                sold / live_tokens if live_tokens else fraction
            )
        dust = max(1e-9, position.tokens * 1e-8)
        if current_balance <= dust:
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            engine.positions.pop(mint, None)
            engine.store.save_position(position)
            engine.spawn(engine.sweep())
        else:
            position.status = core.PositionStatus.PARTIAL
            engine.store.save_position(position)
            if fraction >= 0.999:
                engine.spawn(final._retry_residual(engine, position))
    finally:
        engine.pending_exits.discard(mint)


async def _execute_sell_hardened(
    self: core.Engine,
    position: core.Position,
    fraction: float,
    reason: str,
) -> None:
    mint = position.mint
    release_pending = True
    try:
        live_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
        amount = min(live_tokens, position.remaining) * min(
            1.0, max(0.0, fraction)
        )
        dust = max(1e-9, position.tokens * 1e-8)
        if live_tokens <= dust or amount <= 0:
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            self.positions.pop(mint, None)
            self.store.save_position(position)
            self.spawn(self.sweep())
            return

        urgent = fraction >= 0.999 or any(
            term in reason.lower()
            for term in ("failure", "broke", "liquidation", "horizon")
        )
        priority, tip = self.fee_bid(position.entry_sol * fraction, 1.0, urgent)
        request_id = str(uuid.uuid4())
        token_state = self.tokens.get(mint)
        venue = (
            "pump-amm"
            if token_state and (token_state.migrated or token_state.complete)
            else "pump"
        )
        request = {
            "request_id": request_id,
            "side": "SELL",
            "mint": mint,
            "public_key": self.signer.wallet,
            "amount": amount,
            "denominated_in_sol": False,
            "slippage_bps": self.settings.sell_slippage_bps,
            "priority_fee_sol": priority,
            "tip_sol": tip,
            "pool": venue,
            "metadata": {
                "fraction": fraction,
                "reason": reason,
                "urgent": urgent,
            },
        }
        self.store.order(request_id, mint, "SELL", amount, fraction, reason)
        before_sol = await self.rpc.balance(self.signer.wallet)
        position.status = core.PositionStatus.EXITING
        self.store.save_position(position)
        signature, confirmed, _, error = await self.execute(request_id, request)
        if not confirmed:
            position.status = (
                core.PositionStatus.PARTIAL
                if position.first_partial_done
                else core.PositionStatus.OPEN
            )
            self.store.save_position(position)
            LOGGER.error(
                "E4 sell not confirmed mint=%s signature=%s error=%s",
                mint,
                signature,
                error,
            )
            return
        release_pending = False
        self.spawn(
            _recover_confirmed_sell(
                self,
                position,
                live_tokens,
                fraction,
                signature,
                before_sol,
            )
        )
    except Exception:
        LOGGER.exception("E4 sell execution error mint=%s", mint)
        position.status = (
            core.PositionStatus.PARTIAL
            if position.first_partial_done
            else core.PositionStatus.OPEN
        )
        self.store.save_position(position)
    finally:
        if release_pending:
            self.pending_exits.discard(mint)


core.Engine.execute_buy = _execute_buy_hardened
core.Engine.execute_sell = _execute_sell_hardened


# ---------------------------------------------------------------------------
# Startup reconciliation and SOL/USD cache
# ---------------------------------------------------------------------------


async def _refresh_sol_price_once() -> None:
    global _SOL_USD, _SOL_PRICE_UPDATED_NS
    url = os.getenv(
        "E4_SOL_PRICE_URL",
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
    )

    def fetch() -> float:
        request = urllib.request.Request(
            url,
            headers={
                "accept": "application/json",
                "user-agent": "Gambit-E4/2",
            },
        )
        with urllib.request.urlopen(request, timeout=2.5) as response:
            payload = json.loads(response.read())
        value = _finite(((payload.get("solana") or {}).get("usd")))
        if not value or value <= 0:
            raise ValueError("SOL/USD response did not contain a positive price")
        return value

    try:
        value = await asyncio.to_thread(fetch)
    except Exception as exc:
        LOGGER.warning(
            "SOL/USD refresh failed; using cached fallback %.4f: %s",
            _SOL_USD,
            exc,
        )
        return
    _SOL_USD = value
    _SOL_PRICE_UPDATED_NS = time.time_ns()


async def _sol_price_loop(stop_event: asyncio.Event) -> None:
    interval = max(
        5.0,
        float(os.getenv("E4_SOL_PRICE_REFRESH_SECONDS", "15")),
    )
    while not stop_event.is_set():
        await _refresh_sol_price_once()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _resolve_uncertain_entries(engine: core.Engine) -> None:
    rows = engine.store.conn.execute(
        "SELECT mint,last_action FROM e4_seen_mints WHERE entry_count=0 "
        "AND last_action IN ('BUY_PENDING','BUY_UNCERTAIN')"
    ).fetchall()
    for row in rows:
        mint = str(row["mint"])
        try:
            balance = await engine.rpc.token_balance(engine.signer.wallet, mint)
        except Exception:
            LOGGER.exception("Could not reconcile uncertain E4 entry mint=%s", mint)
            continue
        if balance <= 0:
            engine.store.conn.execute(
                "UPDATE e4_seen_mints SET last_action='BUY_RETRYABLE',"
                "last_reason='startup reconciliation observed zero token balance' "
                "WHERE mint=?",
                (mint,),
            )
            continue
        order = engine.store.conn.execute(
            "SELECT amount,signature,route FROM e4_orders "
            "WHERE mint=? AND side='BUY' ORDER BY created_ns DESC LIMIT 1",
            (mint,),
        ).fetchone()
        entry_sol = float(order["amount"] if order else 0.0)
        if entry_sol <= 0:
            LOGGER.critical(
                "Uncertain E4 token balance has no recoverable order mint=%s",
                mint,
            )
            continue
        position = core.Position(
            position_id=str(uuid.uuid4()),
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=entry_sol,
            tokens=balance,
            remaining=balance,
            entry_price=entry_sol / balance,
            max_price=entry_sol / balance,
            last_price=entry_sol / balance,
            entry_signature=str(
                order["signature"]
                if order and order["signature"]
                else "reconciled"
            ),
            route=order["route"] if order else None,
        )
        engine.positions[mint] = position
        engine.store.save_position(position)
        engine.store.conn.execute(
            "UPDATE e4_seen_mints SET entry_count=1,last_action='BUY_CONFIRMED',"
            "last_reason='recovered from on-chain token balance' WHERE mint=?",
            (mint,),
        )


_previous_run = core.Engine.run


async def _hardened_run(self: core.Engine) -> None:
    await _refresh_sol_price_once()
    await _resolve_uncertain_entries(self)
    price_task = asyncio.create_task(_sol_price_loop(self.stop_event))
    try:
        await _previous_run(self)
    finally:
        price_task.cancel()
        await asyncio.gather(price_task, return_exceptions=True)


core.Engine.run = _hardened_run
