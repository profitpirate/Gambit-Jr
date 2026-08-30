from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from . import e4_live as core
from .e4_runner import _save_position

# Correct the position persistence path before any engine is constructed.
core.Store.save_position = _save_position

_original_from_row = core.Event.from_row.__func__


def _parse_timestamp_ns(value: Any) -> int | None:
    if value is None or value == "":
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
        try:
            return _parse_timestamp_ns(float(text))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _robust_from_row(cls: type[core.Event], row: Mapping[str, Any]) -> core.Event:
    merged: dict[str, Any] = dict(row)
    # V1.5 has used nested immutable payload columns in addition to flat hot columns.
    for key in ("payload_json", "event_json", "raw_json", "data_json", "payload"):
        raw = merged.get(key)
        if not raw:
            continue
        try:
            nested = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(nested, dict):
            for nested_key, value in nested.items():
                merged.setdefault(str(nested_key), value)

    event_type = str(merged.get("event_type") or merged.get("kind") or merged.get("type") or "").upper()
    side = str(merged.get("side") or merged.get("trade_side") or "").upper()
    if event_type in {"TRADE", "PUMP_TRADE", "TOKEN_TRADE", "WALLET_TRADE"} and side in {"BUY", "SELL"}:
        merged["event_type"] = side
    aliases = {
        "WALLET_BUY": "BUY",
        "PUMP_TRADE_BUY": "BUY",
        "BONDING_CURVE_BUY": "BUY",
        "SWAP_BUY": "BUY",
        "WALLET_SELL": "SELL",
        "PUMP_TRADE_SELL": "SELL",
        "BONDING_CURVE_SELL": "SELL",
        "SWAP_SELL": "SELL",
        "CURVE_RESERVE": "CURVE",
        "CURVE_OBSERVATION": "CURVE",
        "RESERVE_OBSERVATION": "CURVE",
        "MIGRATION_STARTED": "MIGRATION",
        "MIGRATION_COMPLETED": "MIGRATION",
        "PUMPSWAP_POOL_CREATED": "MIGRATION",
    }
    if event_type in aliases:
        merged["event_type"] = aliases[event_type]

    if "source_timestamp_ns" not in merged:
        for key in (
            "source_event_timestamp",
            "observed_at",
            "block_time",
            "event_time",
            "timestamp",
        ):
            parsed = _parse_timestamp_ns(merged.get(key))
            if parsed is not None:
                merged["source_timestamp_ns"] = parsed
                break
    if "received_timestamp_ns" not in merged:
        for key in ("source_received_at", "received_at", "candidate_created_at"):
            parsed = _parse_timestamp_ns(merged.get(key))
            if parsed is not None:
                merged["received_timestamp_ns"] = parsed
                break

    # Common V1.5 normalized names.
    field_aliases = {
        "wallet_address": "trader",
        "actor_address": "trader",
        "token_mint": "mint",
        "quote_amount_sol": "sol_amount",
        "base_amount_tokens": "token_amount",
        "market_cap": "fdv_usd",
        "virtual_quote_reserve": "virtual_sol_reserves",
        "virtual_base_reserve": "virtual_token_reserves",
        "real_quote_reserve": "real_sol_reserves",
        "real_base_reserve": "real_token_reserves",
    }
    for source, target in field_aliases.items():
        if target not in merged and source in merged:
            merged[target] = merged[source]
    return _original_from_row(cls, merged)


core.Event.from_row = classmethod(_robust_from_row)

_original_discover = core.SQLiteEventSource._discover


def _discover_with_override(self: core.SQLiteEventSource) -> tuple[str, str]:
    table = os.getenv("E4_EVENT_TABLE", "").strip()
    if table:
        id_column = os.getenv("E4_EVENT_ID_COLUMN", "id").strip()
        connection = self._connect()
        columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if id_column not in columns:
            raise RuntimeError(f"E4_EVENT_ID_COLUMN {id_column!r} is not present in {table!r}")
        self.table, self.id_column = table, id_column
        return table, id_column
    return _original_discover(self)


core.SQLiteEventSource._discover = _discover_with_override


async def _reconcile(engine: core.Engine) -> None:
    """Reconcile on-chain balances before consuming another event.

    This closes stale positions whose token balance is already zero and rebuilds
    positions for confirmed buys that landed before a crash but were not yet
    persisted. It never submits a second entry.
    """

    for mint, position in list(engine.positions.items()):
        try:
            balance = await engine.rpc.token_balance(engine.signer.wallet, mint)
        except Exception:
            core.LOGGER.exception("E4 restart balance reconciliation failed", extra={"mint": mint})
            continue
        if balance <= max(1e-9, position.tokens * 1e-8):
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            engine.store.save_position(position)
            engine.positions.pop(mint, None)
        else:
            position.remaining = min(position.remaining, balance)
            engine.store.save_position(position)

    rows = engine.store.conn.execute(
        """SELECT o.mint,o.amount,o.signature,o.route
        FROM e4_orders o
        LEFT JOIN e4_positions p ON p.mint=o.mint
        WHERE o.side='BUY' AND o.confirmed=1 AND o.mint IS NOT NULL AND p.mint IS NULL"""
    ).fetchall()
    for row in rows:
        mint = str(row["mint"])
        try:
            token_balance = await engine.rpc.token_balance(engine.signer.wallet, mint)
        except Exception:
            core.LOGGER.exception("E4 orphaned-buy reconciliation failed", extra={"mint": mint})
            continue
        if token_balance <= 0:
            continue
        entry_sol = float(row["amount"])
        position = core.Position(
            position_id=str(__import__("uuid").uuid4()),
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=entry_sol,
            tokens=token_balance,
            remaining=token_balance,
            entry_price=entry_sol / token_balance,
            max_price=entry_sol / token_balance,
            last_price=entry_sol / token_balance,
            entry_signature=str(row["signature"] or "reconciled"),
            route=row["route"],
        )
        engine.positions[mint] = position
        engine.store.save_position(position)
        core.LOGGER.warning("E4 recovered orphaned confirmed buy", extra={"mint": mint})


_original_run = core.Engine.run


async def _production_run(self: core.Engine) -> None:
    await _reconcile(self)
    await _original_run(self)


core.Engine.run = _production_run


def main() -> None:
    core.main()


if __name__ == "__main__":
    main()
