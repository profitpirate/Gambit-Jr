from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

LOGGER = logging.getLogger("gambit.e4")
LAMPORTS_PER_SOL = 1_000_000_000
E4_ORACLE = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


def _json_dict(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


@dataclass(slots=True)
class Settings:
    live: bool = False
    operational_db: Path = Path("data/memecoin.db")
    execution_db: Path = Path("data/e4.db")
    wallet: str | None = None
    vault: str | None = None
    keypair_path: Path | None = None
    signer_command: tuple[str, ...] = ()
    builder_command: tuple[str, ...] = ()
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    fallback_rpcs: tuple[str, ...] = ()
    route_urls: dict[str, str] = field(default_factory=dict)
    route_headers: dict[str, str] = field(default_factory=dict)
    direct_rpc_route: bool = True
    event_poll_seconds: float = 0.002
    route_stagger_ms: int = 8
    rpc_timeout_seconds: float = 3.0
    confirmation_timeout_seconds: float = 8.0
    buy_slippage_bps: int = 800
    sell_slippage_bps: int = 1000
    max_slippage_bps: int = 2500
    max_priority_fee_sol: float = 0.05
    max_tip_sol: float = 0.05
    max_execution_cost_sol: float = 0.15
    max_position_fraction: float = 0.20
    min_position_sol: float = 0.01
    max_position_sol: float = 5.0
    reserve_sol: float = 0.03
    operating_float_sol: float = 0.30
    sweep_min_sol: float = 0.05
    max_entry_fdv_usd: float = 10_000.0
    target_entry_fdv_usd: float = 4_878.0
    minimum_unique_buyers_1s: int = 2
    minimum_sol_inflow_1s: float = 0.10
    minimum_buy_sell_ratio: float = 1.25
    failure_window_ms: int = 5_000
    max_hold_ms: int = 60_000
    failure_markout_bps: int = -350
    flow_break_markout_bps: int = -100
    normal_partial_markout_bps: int = 900
    acceleration_partial_markout_bps: int = 1500
    runner_drawdown_bps: int = 1200
    model_path: Path = Path("models/e4/e4-observed-v1.json")
    oracle_wallet: str = E4_ORACLE

    @classmethod
    def from_env(cls) -> "Settings":
        keypair = os.getenv("E4_KEYPAIR_PATH")
        return cls(
            live=_bool("E4_LIVE"),
            operational_db=Path(os.getenv("DATABASE_PATH", "data/memecoin.db")),
            execution_db=Path(os.getenv("E4_DATABASE_PATH", "data/e4.db")),
            wallet=os.getenv("E4_WALLET_PUBLIC_KEY") or None,
            vault=os.getenv("E4_VAULT_PUBLIC_KEY") or None,
            keypair_path=Path(keypair) if keypair else None,
            signer_command=tuple(shlex.split(os.getenv("E4_SIGNER_COMMAND", ""))),
            builder_command=tuple(shlex.split(os.getenv("E4_BUILDER_COMMAND", "node tools/e4-builder/index.mjs"))),
            rpc_url=os.getenv("E4_PRIMARY_RPC_URL", os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")),
            fallback_rpcs=_csv("E4_FALLBACK_RPC_URLS"),
            route_urls=_json_dict("E4_ROUTE_URLS_JSON"),
            route_headers=_json_dict("E4_ROUTE_HEADERS_JSON"),
            direct_rpc_route=_bool("E4_DIRECT_RPC_ROUTE", True),
            event_poll_seconds=_float("E4_EVENT_POLL_SECONDS", 0.002),
            route_stagger_ms=_int("E4_ROUTE_RACE_DELAY_MS", 8),
            rpc_timeout_seconds=_float("E4_RPC_TIMEOUT_SECONDS", 3.0),
            confirmation_timeout_seconds=_float("E4_CONFIRMATION_TIMEOUT_SECONDS", 8.0),
            buy_slippage_bps=_int("E4_BUY_SLIPPAGE_BPS", 800),
            sell_slippage_bps=_int("E4_SELL_SLIPPAGE_BPS", 1000),
            max_slippage_bps=_int("E4_MAX_SLIPPAGE_BPS", 2500),
            max_priority_fee_sol=_float("E4_MAX_PRIORITY_FEE_SOL", 0.05),
            max_tip_sol=_float("E4_MAX_TIP_SOL", 0.05),
            max_execution_cost_sol=_float("E4_MAX_TOTAL_EXECUTION_COST_SOL", 0.15),
            max_position_fraction=_float("E4_MAX_POSITION_FRACTION", 0.20),
            min_position_sol=_float("E4_MIN_POSITION_SOL", 0.01),
            max_position_sol=_float("E4_MAX_POSITION_SOL", 5.0),
            reserve_sol=_float("E4_EXECUTION_RESERVE_SOL", 0.03),
            operating_float_sol=_float("E4_OPERATING_FLOAT_SOL", 0.30),
            sweep_min_sol=_float("E4_SWEEP_MIN_SOL", 0.05),
            max_entry_fdv_usd=_float("E4_MAX_ENTRY_FDV_USD", 10_000),
            target_entry_fdv_usd=_float("E4_TARGET_ENTRY_FDV_USD", 4_878),
            minimum_unique_buyers_1s=_int("E4_MINIMUM_UNIQUE_BUYERS_1S", 2),
            minimum_sol_inflow_1s=_float("E4_MINIMUM_SOL_INFLOW_1S", 0.10),
            minimum_buy_sell_ratio=_float("E4_MINIMUM_BUY_SELL_RATIO", 1.25),
            failure_window_ms=_int("E4_FAILURE_WINDOW_MS", 5_000),
            max_hold_ms=_int("E4_MAX_HOLD_MS", 60_000),
            failure_markout_bps=_int("E4_FAILURE_MARKOUT_BPS", -350),
            flow_break_markout_bps=_int("E4_FLOW_BREAK_MARKOUT_BPS", -100),
            normal_partial_markout_bps=_int("E4_NORMAL_PARTIAL_MARKOUT_BPS", 900),
            acceleration_partial_markout_bps=_int("E4_ACCELERATION_PARTIAL_MARKOUT_BPS", 1500),
            runner_drawdown_bps=_int("E4_RUNNER_DRAWDOWN_BPS", 1200),
            model_path=Path(os.getenv("E4_MODEL_PATH", "models/e4/e4-observed-v1.json")),
            oracle_wallet=os.getenv("E4_ORACLE_WALLET", E4_ORACLE),
        )

    def validate(self) -> None:
        # These are observed E4 invariants, not configurable Gambit strategy opinions.
        if _int("E4_MAX_ENTRIES_PER_MINT", 1) != 1:
            raise ValueError("E4_MAX_ENTRIES_PER_MINT must equal 1")
        if _int("E4_MAX_CONCURRENT_POSITIONS", 2) != 2:
            raise ValueError("E4_MAX_CONCURRENT_POSITIONS must equal 2")
        if self.buy_slippage_bps > self.max_slippage_bps or self.sell_slippage_bps > self.max_slippage_bps:
            raise ValueError("configured E4 slippage exceeds E4_MAX_SLIPPAGE_BPS")
        if not 0 < self.max_position_fraction <= 1:
            raise ValueError("E4_MAX_POSITION_FRACTION must be in (0,1]")
        if self.live:
            missing = []
            if not self.wallet:
                missing.append("E4_WALLET_PUBLIC_KEY")
            if not self.keypair_path and not self.signer_command:
                missing.append("E4_KEYPAIR_PATH or E4_SIGNER_COMMAND")
            if not self.builder_command:
                missing.append("E4_BUILDER_COMMAND")
            if not self.route_urls and not self.direct_rpc_route:
                missing.append("transaction route")
            if missing:
                raise ValueError("incomplete live E4 configuration: " + ", ".join(missing))


class EventKind(StrEnum):
    CREATE = "CREATE"
    BUY = "BUY"
    SELL = "SELL"
    CURVE = "CURVE"
    MIGRATION = "MIGRATION"
    PUMPSWAP_BUY = "PUMPSWAP_BUY"
    PUMPSWAP_SELL = "PUMPSWAP_SELL"
    UNKNOWN = "UNKNOWN"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


@dataclass(slots=True, frozen=True)
class Event:
    event_id: int
    kind: EventKind
    mint: str
    source_ns: int
    received_ns: int
    slot: int | None = None
    tx_index: int | None = None
    signature: str | None = None
    trader: str | None = None
    sol_amount: float = 0.0
    token_amount: float = 0.0
    price_sol: float | None = None
    fdv_usd: float | None = None
    virtual_sol: float | None = None
    virtual_tokens: float | None = None
    real_sol: float | None = None
    real_tokens: float | None = None
    complete: bool = False
    creator: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Event":
        lowered = {str(key).lower(): value for key, value in row.items()}

        def first(*keys: str, default: Any = None) -> Any:
            for key in keys:
                if key.lower() in lowered and lowered[key.lower()] not in {None, ""}:
                    return lowered[key.lower()]
            return default

        aliases = {
            "TOKEN_CREATE": EventKind.CREATE,
            "CREATE_TOKEN": EventKind.CREATE,
            "PUMP_CREATE": EventKind.CREATE,
            "CREATE": EventKind.CREATE,
            "TOKEN_BUY": EventKind.BUY,
            "PUMP_BUY": EventKind.BUY,
            "BUY": EventKind.BUY,
            "TOKEN_SELL": EventKind.SELL,
            "PUMP_SELL": EventKind.SELL,
            "SELL": EventKind.SELL,
            "CURVE_STATE": EventKind.CURVE,
            "RESERVE": EventKind.CURVE,
            "CURVE": EventKind.CURVE,
            "MIGRATION": EventKind.MIGRATION,
            "MIGRATION_COMPLETED": EventKind.MIGRATION,
            "PUMPSWAP_BUY": EventKind.PUMPSWAP_BUY,
            "PUMPSWAP_SELL": EventKind.PUMPSWAP_SELL,
        }
        kind = aliases.get(str(first("event_type", "kind", "type", "action", default="UNKNOWN")).upper(), EventKind.UNKNOWN)
        mint = str(first("mint", "token_address", "token", "address", default=""))
        if not mint:
            raise ValueError("canonical event missing mint")
        source_ns = first("source_timestamp_ns", "source_time_ns", "observed_at_ns")
        if source_ns is None:
            seconds = float(first("block_time", "timestamp", "observed_at", default=time.time()))
            source_ns = int(seconds * 1_000_000_000)

        def number(*keys: str) -> float | None:
            value = first(*keys)
            if value is None:
                return None
            try:
                result = float(value)
                return result if math.isfinite(result) else None
            except (TypeError, ValueError):
                return None

        return cls(
            event_id=int(first("id", "event_id", "sequence", default=0)),
            kind=kind,
            mint=mint,
            source_ns=int(source_ns),
            received_ns=int(first("received_timestamp_ns", "source_received_at_ns", default=time.time_ns())),
            slot=int(first("slot", "block_slot")) if first("slot", "block_slot") is not None else None,
            tx_index=int(first("transaction_index", "tx_index")) if first("transaction_index", "tx_index") is not None else None,
            signature=first("signature", "tx_signature", "tx_hash"),
            trader=first("trader", "wallet", "owner", "user", "actor"),
            sol_amount=number("sol_amount", "quote_amount", "amount_sol") or 0.0,
            token_amount=number("token_amount", "base_amount", "amount_token") or 0.0,
            price_sol=number("price_sol", "token_price_sol"),
            fdv_usd=number("fdv_usd", "market_cap_usd", "market_cap", "fdv"),
            virtual_sol=number("virtual_sol_reserves", "virtual_quote_reserves"),
            virtual_tokens=number("virtual_token_reserves", "virtual_base_reserves"),
            real_sol=number("real_sol_reserves", "real_quote_reserves"),
            real_tokens=number("real_token_reserves", "real_base_reserves"),
            complete=str(first("complete", "curve_complete", default="false")).lower() in {"1", "true", "yes"},
            creator=first("creator", "creator_wallet", "deployer"),
        )


@dataclass(slots=True)
class Flow:
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)

    @property
    def net(self) -> float:
        return self.buy_sol - self.sell_sol

    @property
    def ratio(self) -> float:
        if self.sell_sol <= 0:
            return float("inf") if self.buy_sol > 0 else 0.0
        return self.buy_sol / self.sell_sol


@dataclass(slots=True)
class TokenState:
    mint: str
    events: deque[Event] = field(default_factory=deque)
    created_ns: int | None = None
    latest_ns: int = 0
    price_sol: float | None = None
    fdv_usd: float | None = None
    complete: bool = False
    migrated: bool = False
    creator: str | None = None
    wallet_touched: bool = False

    def apply(self, event: Event, wallet: str | None) -> None:
        self.latest_ns = max(self.latest_ns, event.source_ns)
        if event.kind == EventKind.CREATE and self.created_ns is None:
            self.created_ns = event.source_ns
        self.price_sol = event.price_sol or self.price_sol
        if self.price_sol is None and event.virtual_sol and event.virtual_tokens:
            self.price_sol = event.virtual_sol / event.virtual_tokens
        self.fdv_usd = event.fdv_usd or self.fdv_usd
        self.creator = event.creator or self.creator
        self.complete = self.complete or event.complete
        self.migrated = self.migrated or event.kind == EventKind.MIGRATION
        self.wallet_touched = self.wallet_touched or bool(wallet and event.trader == wallet)
        self.events.append(event)
        cutoff = self.latest_ns - 10_000_000_000
        while self.events and self.events[0].source_ns < cutoff:
            self.events.popleft()

    def flow(self, milliseconds: int) -> Flow:
        result = Flow()
        cutoff = self.latest_ns - milliseconds * 1_000_000
        for event in reversed(self.events):
            if event.source_ns < cutoff:
                break
            if event.kind in {EventKind.BUY, EventKind.PUMPSWAP_BUY}:
                result.buy_sol += event.sol_amount
                if event.trader:
                    result.buyers.add(event.trader)
            elif event.kind in {EventKind.SELL, EventKind.PUMPSWAP_SELL}:
                result.sell_sol += event.sol_amount
                if event.trader:
                    result.sellers.add(event.trader)
        return result

    def features(self) -> dict[str, float]:
        data: dict[str, float] = {"fdv_usd": self.fdv_usd or 0.0, "price_sol": self.price_sol or 0.0}
        for window in (100, 250, 500, 1000, 2000, 3000, 5000):
            flow = self.flow(window)
            data[f"buy_sol_{window}ms"] = flow.buy_sol
            data[f"sell_sol_{window}ms"] = flow.sell_sol
            data[f"net_sol_{window}ms"] = flow.net
            data[f"buyers_{window}ms"] = float(len(flow.buyers))
            data[f"sellers_{window}ms"] = float(len(flow.sellers))
            data[f"ratio_{window}ms"] = min(flow.ratio, 1000.0)
        return data


@dataclass(slots=True)
class Position:
    position_id: str
    mint: str
    status: PositionStatus
    opened_ns: int
    entry_sol: float
    tokens: float
    remaining: float
    entry_price: float
    max_price: float
    last_price: float
    entry_signature: str
    first_partial_done: bool = False
    first_partial_fraction: float | None = None
    realized_sol: float = 0.0
    close_signature: str | None = None
    route: str | None = None

    @property
    def age_ms(self) -> int:
        return max(0, int((time.time_ns() - self.opened_ns) / 1_000_000))

    def markout_bps(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 10_000 if self.entry_price > 0 else 0.0

    def drawdown_bps(self, price: float) -> float:
        return (1.0 - price / self.max_price) * 10_000 if self.max_price > 0 else 0.0


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS e4_seen_mints(
 mint TEXT PRIMARY KEY, first_seen_ns INTEGER NOT NULL, entry_count INTEGER NOT NULL DEFAULT 0 CHECK(entry_count<=1),
 last_action TEXT NOT NULL, last_reason TEXT NOT NULL, last_score REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS e4_positions(
 position_id TEXT PRIMARY KEY, mint TEXT NOT NULL UNIQUE, status TEXT NOT NULL, opened_ns INTEGER NOT NULL,
 entry_sol REAL NOT NULL, tokens REAL NOT NULL, remaining REAL NOT NULL, entry_price REAL NOT NULL,
 max_price REAL NOT NULL, last_price REAL NOT NULL, entry_signature TEXT NOT NULL,
 first_partial_done INTEGER NOT NULL, first_partial_fraction REAL, realized_sol REAL NOT NULL,
 close_signature TEXT, route TEXT, updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS e4_orders(
 request_id TEXT PRIMARY KEY, mint TEXT, side TEXT NOT NULL, amount REAL NOT NULL, fraction REAL,
 reason TEXT NOT NULL, created_ns INTEGER NOT NULL, signature TEXT, route TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
 confirmation_slot INTEGER, error TEXT, route_results_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS e4_decisions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, event_id INTEGER, action TEXT NOT NULL,
 score REAL, reason TEXT NOT NULL, payload_json TEXT NOT NULL, created_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS e4_oracle_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, side TEXT NOT NULL, signature TEXT,
 slot INTEGER, source_ns INTEGER NOT NULL, state_json TEXT NOT NULL, UNIQUE(signature,side)
);
CREATE TABLE IF NOT EXISTS e4_route_metrics(
 id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, route TEXT NOT NULL,
 submitted_ns INTEGER NOT NULL, completed_ns INTEGER NOT NULL, result TEXT NOT NULL, error TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    def has_entered(self, mint: str) -> bool:
        row = self.conn.execute("SELECT entry_count FROM e4_seen_mints WHERE mint=?", (mint,)).fetchone()
        return bool(row and int(row[0]) >= 1)

    def mark_entry(self, mint: str, score: float, reason: str) -> bool:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT entry_count FROM e4_seen_mints WHERE mint=?", (mint,)).fetchone()
            if row and int(row[0]) >= 1:
                self.conn.execute("ROLLBACK")
                return False
            if row:
                self.conn.execute(
                    "UPDATE e4_seen_mints SET entry_count=1,last_action='BUY',last_reason=?,last_score=? WHERE mint=?",
                    (reason, score, mint),
                )
            else:
                self.conn.execute(
                    "INSERT INTO e4_seen_mints VALUES(?,?,?,?,?,?)",
                    (mint, time.time_ns(), 1, "BUY", reason, score),
                )
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def record_terminal_skip(self, mint: str, score: float, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO e4_seen_mints VALUES(?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET last_action='SKIP',last_reason=excluded.last_reason,last_score=excluded.last_score",
            (mint, time.time_ns(), 0, "SKIP", reason, score),
        )

    def decision(self, mint: str, event_id: int | None, action: str, score: float | None, reason: str, payload: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO e4_decisions(mint,event_id,action,score,reason,payload_json,created_ns) VALUES(?,?,?,?,?,?,?)",
            (mint, event_id, action, score, reason, json.dumps(dict(payload), separators=(",", ":"), default=str), time.time_ns()),
        )

    def save_position(self, position: Position) -> None:
        self.conn.execute(
            """INSERT INTO e4_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET status=excluded.status,remaining=excluded.remaining,max_price=excluded.max_price,
            last_price=excluded.last_price,first_partial_done=excluded.first_partial_done,
            first_partial_fraction=excluded.first_partial_fraction,realized_sol=excluded.realized_sol,
            close_signature=excluded.close_signature,route=excluded.route,updated_ns=excluded.updated_ns""",
            (
                position.position_id, position.mint, position.status.value, position.opened_ns, position.entry_sol,
                position.tokens, position.remaining, position.entry_price, position.max_price, position.last_price,
                position.entry_signature, int(position.first_partial_done), position.first_partial_fraction,
                position.realized_sol, position.close_signature, position.route, time.time_ns(), time.time_ns(),
            ),
        )

    def load_open_positions(self) -> dict[str, Position]:
        result: dict[str, Position] = {}
        for row in self.conn.execute("SELECT * FROM e4_positions WHERE status IN ('OPEN','PARTIAL','EXITING')"):
            result[row["mint"]] = Position(
                position_id=row["position_id"], mint=row["mint"], status=PositionStatus(row["status"]),
                opened_ns=row["opened_ns"], entry_sol=row["entry_sol"], tokens=row["tokens"],
                remaining=row["remaining"], entry_price=row["entry_price"], max_price=row["max_price"],
                last_price=row["last_price"], entry_signature=row["entry_signature"],
                first_partial_done=bool(row["first_partial_done"]), first_partial_fraction=row["first_partial_fraction"],
                realized_sol=row["realized_sol"], close_signature=row["close_signature"], route=row["route"],
            )
        return result

    def order(self, request_id: str, mint: str | None, side: str, amount: float, fraction: float | None, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO e4_orders(request_id,mint,side,amount,fraction,reason,created_ns) VALUES(?,?,?,?,?,?,?)",
            (request_id, mint, side, amount, fraction, reason, time.time_ns()),
        )

    def receipt(self, request_id: str, signature: str, route: str, confirmed: bool, slot: int | None, error: str | None, results: Mapping[str, str]) -> None:
        self.conn.execute(
            "UPDATE e4_orders SET signature=?,route=?,confirmed=?,confirmation_slot=?,error=?,route_results_json=? WHERE request_id=?",
            (signature, route, int(confirmed), slot, error, json.dumps(dict(results), separators=(",", ":")), request_id),
        )

    def route_metric(self, request_id: str, route: str, submitted: int, completed: int, result: str, error: str | None) -> None:
        self.conn.execute(
            "INSERT INTO e4_route_metrics(request_id,route,submitted_ns,completed_ns,result,error) VALUES(?,?,?,?,?,?)",
            (request_id, route, submitted, completed, result, error),
        )

    def oracle(self, event: Event, state: Mapping[str, float]) -> None:
        side = "BUY" if event.kind in {EventKind.BUY, EventKind.PUMPSWAP_BUY} else "SELL"
        self.conn.execute(
            "INSERT OR IGNORE INTO e4_oracle_events(mint,side,signature,slot,source_ns,state_json) VALUES(?,?,?,?,?,?)",
            (event.mint, side, event.signature, event.slot, event.source_ns, json.dumps(dict(state), separators=(",", ":"))),
        )

    def status(self) -> dict[str, Any]:
        positions = {row[0]: row[1] for row in self.conn.execute("SELECT status,COUNT(*) FROM e4_positions GROUP BY status")}
        return {
            "positions": positions,
            "orders": self.conn.execute("SELECT COUNT(*) FROM e4_orders").fetchone()[0],
            "entered_mints": self.conn.execute("SELECT COUNT(*) FROM e4_seen_mints WHERE entry_count=1").fetchone()[0],
            "oracle_events": self.conn.execute("SELECT COUNT(*) FROM e4_oracle_events").fetchone()[0],
        }


class SQLiteEventSource:
    MINT = {"mint", "token_address", "token", "address"}
    KIND = {"event_type", "kind", "type", "action"}

    def __init__(self, path: Path, poll: float):
        self.path = path
        self.poll = poll
        self.conn: sqlite3.Connection | None = None
        self.table: str | None = None
        self.id_column: str | None = None
        self.last_id = 0

    def _connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=0.1, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")
            self.conn.execute("PRAGMA busy_timeout=25")
        return self.conn

    def _discover(self) -> tuple[str, str]:
        conn = self._connect()
        candidates: list[tuple[int, str, str]] = []
        for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            table = str(table_row[0])
            columns = [str(row[1]).lower() for row in conn.execute(f'PRAGMA table_info("{table}")')]
            if not set(columns).intersection(self.MINT) or not set(columns).intersection(self.KIND):
                continue
            id_column = next((name for name in ("id", "event_id", "sequence") if name in columns), None)
            if id_column:
                score = (100 if "canonical" in table.lower() else 0) + len(columns)
                candidates.append((score, table, id_column))
        if not candidates:
            raise RuntimeError("No V1.5 canonical event table found")
        _, self.table, self.id_column = max(candidates)
        return self.table, self.id_column

    def _read(self) -> list[sqlite3.Row]:
        conn = self._connect()
        table, id_column = (self.table, self.id_column) if self.table and self.id_column else self._discover()
        assert table and id_column
        return list(conn.execute(f'SELECT * FROM "{table}" WHERE "{id_column}">? ORDER BY "{id_column}" LIMIT 500', (self.last_id,)))

    async def events(self) -> AsyncIterator[Event]:
        while True:
            try:
                rows = await asyncio.to_thread(self._read)
            except (sqlite3.Error, RuntimeError):
                await asyncio.sleep(0.1)
                continue
            if not rows:
                await asyncio.sleep(self.poll)
                continue
            for row in rows:
                event = Event.from_row(dict(row))
                self.last_id = max(self.last_id, event.event_id)
                yield event


async def post_json(url: str, payload: Mapping[str, Any], timeout: float, headers: Mapping[str, str] | None = None) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()

    def send() -> bytes:
        request_headers = {"content-type": "application/json", "accept": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc

    return await asyncio.to_thread(send)


class Rpc:
    def __init__(self, settings: Settings):
        self.urls = tuple(dict.fromkeys((settings.rpc_url, *settings.fallback_rpcs)))
        self.timeout = settings.rpc_timeout_seconds
        self.request_id = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        error: Exception | None = None
        for url in self.urls:
            self.request_id += 1
            try:
                body = await post_json(url, {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}, self.timeout)
                response = json.loads(body)
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
                return response.get("result")
            except Exception as exc:
                error = exc
        raise RuntimeError(f"all Solana RPCs failed for {method}: {error}")

    async def balance(self, wallet: str) -> float:
        result = await self.call("getBalance", [wallet, {"commitment": "processed"}])
        return float(result["value"]) / LAMPORTS_PER_SOL

    async def token_balance(self, wallet: str, mint: str) -> float:
        result = await self.call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "processed"}])
        return sum(float(item["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0) for item in result.get("value", []))

    async def confirm(self, signature: str, timeout: float) -> tuple[bool, int | None, str | None]:
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                result = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
                status = (result.get("value") or [None])[0]
                if status:
                    if status.get("err") is not None:
                        return False, status.get("slot"), json.dumps(status["err"], default=str)
                    if status.get("confirmationStatus") in {"processed", "confirmed", "finalized"}:
                        return True, status.get("slot"), None
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.1)
        return False, None, last_error or "confirmation timeout"


class Signer:
    def __init__(self, settings: Settings):
        self.command = settings.signer_command
        self.wallet = settings.wallet or ""
        self.keypair = None
        if settings.keypair_path:
            try:
                from solders.keypair import Keypair
            except ImportError as exc:
                raise RuntimeError("live E4 signing requires solders") from exc
            raw = settings.keypair_path.read_text().strip()
            self.keypair = Keypair.from_bytes(bytes(json.loads(raw))) if raw.startswith("[") else Keypair.from_base58_string(raw)
            if str(self.keypair.pubkey()) != self.wallet:
                raise ValueError("keypair does not match E4_WALLET_PUBLIC_KEY")

    async def sign(self, transaction_b64: str) -> tuple[str, str]:
        if self.keypair is not None:
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64))
            signed = VersionedTransaction(tx.message, [self.keypair])
            return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])
        process = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        payload = json.dumps({"transaction_base64": transaction_b64, "expected_public_key": self.wallet}).encode() + b"\n"
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=2)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[:1000])
        result = json.loads(stdout)
        return str(result["signed_transaction_base64"]), str(result["signature"])


class Builder:
    def __init__(self, command: tuple[str, ...]):
        self.command = command

    async def build(self, request: Mapping[str, Any]) -> str:
        process = await asyncio.create_subprocess_exec(*self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(dict(request), separators=(",", ":")).encode() + b"\n"), timeout=2)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[:1000])
        result = json.loads(stdout)
        encoded = str(result["transaction_base64"])
        base64.b64decode(encoded, validate=True)
        return encoded


@dataclass(slots=True)
class RouteResult:
    name: str
    submitted_ns: int
    completed_ns: int
    accepted: bool
    result: str
    error: str | None = None


class RouteSender:
    def __init__(self, settings: Settings, rpc: Rpc):
        self.settings = settings
        self.rpc = rpc
        self.routes = list(settings.route_urls.items())
        if settings.direct_rpc_route:
            self.routes.append(("direct_rpc", settings.rpc_url))
        if not self.routes:
            raise ValueError("E4 requires at least one route")

    def _headers(self, name: str) -> dict[str, str]:
        value = self.settings.route_headers.get(name, "").strip()
        if not value:
            return {}
        if value.startswith("{"):
            return {str(k): str(v) for k, v in json.loads(value).items()}
        key, item = value.split(":", 1)
        return {key.strip(): item.strip()}

    async def _send(self, index: int, name: str, url: str, tx: str, expected_signature: str) -> RouteResult:
        if index:
            await asyncio.sleep(index * self.settings.route_stagger_ms / 1000)
        started = time.time_ns()
        try:
            body = await post_json(
                url,
                {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [tx, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]},
                1.5,
                self._headers(name),
            )
            response = json.loads(body)
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return RouteResult(name, started, time.time_ns(), True, str(response.get("result") or expected_signature))
        except Exception as exc:
            return RouteResult(name, started, time.time_ns(), False, "rejected", str(exc))

    async def submit(self, tx: str, signature: str) -> tuple[str, bool, int | None, str | None, list[RouteResult]]:
        results = await asyncio.gather(*(self._send(index, name, url, tx, signature) for index, (name, url) in enumerate(self.routes)))
        accepted = [item for item in results if item.accepted]
        if not accepted:
            return "NONE", False, None, "; ".join(f"{item.name}:{item.error}" for item in results), results
        winner = min(accepted, key=lambda item: item.completed_ns)
        confirmed, slot, error = await self.rpc.confirm(signature, self.settings.confirmation_timeout_seconds)
        return winner.name, confirmed, slot, error, results


class E4Policy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        if settings.model_path.exists():
            payload = json.loads(settings.model_path.read_text())
            if payload.get("selection_model", {}).get("kind") == "logistic":
                self.model = payload["selection_model"]

    def entry(self, state: TokenState) -> tuple[bool, float, float, str, dict[str, float]]:
        if state.complete or state.migrated or state.wallet_touched:
            return False, 0.0, 0.0, "not an untouched live Pump curve", {}
        fdv = state.fdv_usd
        if not fdv or fdv > self.settings.max_entry_fdv_usd:
            return False, 0.0, 0.0, "outside observed E4 entry FDV", {}
        flow = state.flow(1000)
        features = state.features()
        if len(flow.buyers) < self.settings.minimum_unique_buyers_1s:
            return False, 0.0, 0.0, "insufficient first-second buyers", features
        if flow.buy_sol < self.settings.minimum_sol_inflow_1s or flow.ratio < self.settings.minimum_buy_sell_ratio:
            return False, 0.0, 0.0, "insufficient first-second buy dominance", features
        if self.model:
            logit = float(self.model.get("intercept", 0)) + sum(float(coef) * features.get(name, 0.0) for name, coef in self.model.get("coefficients", {}).items())
            score = 1 / (1 + math.exp(-max(-30, min(30, logit))))
            if score < float(self.model.get("threshold", 0.5)):
                return False, score, 0.0, "E4 selected-vs-ignored model rejected", features
        else:
            fdv_score = max(0.0, 1.0 - abs(fdv - self.settings.target_entry_fdv_usd) / self.settings.target_entry_fdv_usd)
            score = 0.25 * fdv_score + 0.25 * min(1.0, len(flow.buyers) / 10) + 0.30 * min(1.0, flow.buy_sol / 5) + 0.20 * min(1.0, flow.ratio / 5)
            if score < 0.45:
                return False, score, 0.0, "observed E4 profile rejected", features
        fraction = min(self.settings.max_position_fraction, 0.05 + score * 0.15)
        return True, score, fraction, "E4 entry accepted", features

    def exit(self, position: Position, state: TokenState) -> tuple[str, float, str]:
        price = state.price_sol or position.last_price
        if not price:
            return "HOLD", 0.0, "no price"
        position.last_price = price
        position.max_price = max(position.max_price, price)
        markout = position.markout_bps(price)
        flow250 = state.flow(250)
        flow1s = state.flow(1000)
        broken = flow250.net < 0 or flow1s.ratio < 0.85
        if position.age_ms <= self.settings.failure_window_ms:
            if markout <= self.settings.failure_markout_bps:
                return "SELL_ALL", 1.0, "E4 fast adverse-markout failure"
            if broken and markout <= self.settings.flow_break_markout_bps:
                return "SELL_ALL", 1.0, "E4 fast flow-break failure"
        if not position.first_partial_done:
            if markout >= self.settings.acceleration_partial_markout_bps and flow250.net > 0 and flow250.ratio >= 2:
                return "SELL_PARTIAL", 0.20, "E4 acceleration first partial"
            if markout >= self.settings.normal_partial_markout_bps:
                return "SELL_PARTIAL", 0.30, "E4 normal first partial"
            if position.age_ms >= self.settings.failure_window_ms and broken:
                return "SELL_ALL", 1.0, "E4 confirmation failed"
            return "HOLD", 0.0, "awaiting E4 confirmation"
        if broken and position.drawdown_bps(price) >= 350:
            return "SELL_ALL", 1.0, "E4 runner flow broke"
        if position.drawdown_bps(price) >= self.settings.runner_drawdown_bps:
            return "SELL_ALL", 1.0, "E4 runner peak drawdown"
        if position.age_ms >= self.settings.max_hold_ms:
            return "SELL_ALL", 1.0, "E4 observed hold horizon"
        if markout >= 3000 and flow250.net <= 0:
            return "SELL_PARTIAL", 0.25, "E4 runner distribution"
        return "HOLD", 0.0, "E4 runner confirmed"


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.execution_db)
        self.source = SQLiteEventSource(settings.operational_db, settings.event_poll_seconds)
        self.rpc = Rpc(settings)
        self.signer = Signer(settings)
        self.builder = Builder(settings.builder_command)
        self.sender = RouteSender(settings, self.rpc)
        self.policy = E4Policy(settings)
        self.tokens: dict[str, TokenState] = {}
        self.positions = self.store.load_open_positions()
        self.pending_entries: set[str] = set()
        self.pending_exits: set[str] = set()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.stop_event = asyncio.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def state(self, mint: str) -> TokenState:
        return self.tokens.setdefault(mint, TokenState(mint))

    def fee_bid(self, amount: float, score: float, urgent: bool = False) -> tuple[float, float]:
        total = min(amount * max(0, min(score, 1)) * (0.03 if urgent else 0.015), self.settings.max_execution_cost_sol)
        priority = min(self.settings.max_priority_fee_sol, total * 0.6)
        return priority, min(self.settings.max_tip_sol, max(0.0, total - priority))

    async def run(self) -> None:
        async for event in self.source.events():
            if self.stop_event.is_set():
                break
            try:
                await self.on_event(event)
            except Exception:
                LOGGER.exception("E4 event failure", extra={"mint": event.mint, "event_id": event.event_id})
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def on_event(self, event: Event) -> None:
        state = self.state(event.mint)
        if event.trader == self.settings.oracle_wallet and event.kind in {EventKind.BUY, EventKind.SELL, EventKind.PUMPSWAP_BUY, EventKind.PUMPSWAP_SELL}:
            self.store.oracle(event, state.features())
        state.apply(event, self.settings.wallet)
        position = self.positions.get(event.mint)
        if position:
            if event.mint in self.pending_exits:
                return
            action, fraction, reason = self.policy.exit(position, state)
            self.store.decision(event.mint, event.event_id, action, None, reason, {"fraction": fraction})
            if action.startswith("SELL"):
                self.pending_exits.add(event.mint)
                self.spawn(self.execute_sell(position, fraction, reason))
            return
        if event.kind not in {EventKind.CREATE, EventKind.BUY, EventKind.CURVE}:
            return
        if event.mint in self.pending_entries or self.store.has_entered(event.mint):
            return
        if len(self.positions) + len(self.pending_entries) >= 2:
            return
        accepted, score, fraction, reason, features = self.policy.entry(state)
        self.store.decision(event.mint, event.event_id, "BUY" if accepted else "SKIP", score, reason, {"fraction": fraction, "features": features})
        if not accepted:
            if state.complete or state.migrated or (state.fdv_usd or 0) > self.settings.max_entry_fdv_usd:
                self.store.record_terminal_skip(event.mint, score, reason)
            return
        self.pending_entries.add(event.mint)
        self.spawn(self.execute_buy(state, score, fraction, reason))

    async def execute(self, request_id: str, request: Mapping[str, Any]) -> tuple[str, bool, int | None, str | None]:
        unsigned = await self.builder.build(request)
        signed, signature = await self.signer.sign(unsigned)
        route, confirmed, slot, error, results = await self.sender.submit(signed, signature)
        mapped = {item.name: item.result if item.accepted else (item.error or "rejected") for item in results}
        self.store.receipt(request_id, signature, route, confirmed, slot, error, mapped)
        for item in results:
            self.store.route_metric(request_id, item.name, item.submitted_ns, item.completed_ns, item.result, item.error)
        return signature, confirmed, slot, error

    async def execute_buy(self, state: TokenState, score: float, fraction: float, reason: str) -> None:
        mint = state.mint
        try:
            if self.store.has_entered(mint):
                return
            balance, before_tokens = await asyncio.gather(self.rpc.balance(self.signer.wallet), self.rpc.token_balance(self.signer.wallet, mint))
            priority, tip = self.fee_bid(balance * fraction, score)
            deployable = balance - self.settings.reserve_sol - priority - tip
            amount = min(deployable * min(fraction, self.settings.max_position_fraction), self.settings.max_position_sol)
            if amount < self.settings.min_position_sol:
                return
            if not self.store.mark_entry(mint, score, reason):
                return
            request_id = str(uuid.uuid4())
            request = {
                "request_id": request_id, "side": "BUY", "mint": mint, "public_key": self.signer.wallet,
                "amount": amount, "denominated_in_sol": True, "slippage_bps": self.settings.buy_slippage_bps,
                "priority_fee_sol": priority, "tip_sol": tip, "pool": "pump",
                "metadata": {"score": score, "reason": reason, "fdv_usd": state.fdv_usd},
            }
            self.store.order(request_id, mint, "BUY", amount, None, reason)
            signature, confirmed, _, error = await self.execute(request_id, request)
            if not confirmed:
                LOGGER.error("E4 buy failed", extra={"mint": mint, "signature": signature, "error": error})
                return
            after_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
            received = max(0.0, after_tokens - before_tokens)
            if received <= 0:
                raise RuntimeError("buy confirmed without token balance increase")
            entry_price = amount / received
            position = Position(
                position_id=str(uuid.uuid4()), mint=mint, status=PositionStatus.OPEN, opened_ns=time.time_ns(),
                entry_sol=amount, tokens=received, remaining=received, entry_price=entry_price,
                max_price=state.price_sol or entry_price, last_price=state.price_sol or entry_price,
                entry_signature=signature,
            )
            self.positions[mint] = position
            self.store.save_position(position)
            LOGGER.info("E4 position opened", extra={"mint": mint, "amount_sol": amount, "signature": signature})
        except Exception:
            LOGGER.exception("E4 buy execution error", extra={"mint": mint})
        finally:
            self.pending_entries.discard(mint)

    async def execute_sell(self, position: Position, fraction: float, reason: str) -> None:
        mint = position.mint
        try:
            live_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
            amount = min(live_tokens, position.remaining) * min(1.0, max(0.0, fraction))
            if amount <= 0:
                position.status = PositionStatus.CLOSED
                self.positions.pop(mint, None)
                self.store.save_position(position)
                return
            urgent = fraction >= 0.999 or "failure" in reason.lower() or "broke" in reason.lower()
            priority, tip = self.fee_bid(position.entry_sol * fraction, 1.0, urgent)
            request_id = str(uuid.uuid4())
            request = {
                "request_id": request_id, "side": "SELL", "mint": mint, "public_key": self.signer.wallet,
                "amount": amount, "denominated_in_sol": False, "slippage_bps": self.settings.sell_slippage_bps,
                "priority_fee_sol": priority, "tip_sol": tip, "pool": "auto",
                "metadata": {"fraction": fraction, "reason": reason, "urgent": urgent},
            }
            self.store.order(request_id, mint, "SELL", amount, fraction, reason)
            before_sol = await self.rpc.balance(self.signer.wallet)
            position.status = PositionStatus.EXITING
            self.store.save_position(position)
            signature, confirmed, _, error = await self.execute(request_id, request)
            if not confirmed:
                position.status = PositionStatus.PARTIAL if position.first_partial_done else PositionStatus.OPEN
                self.store.save_position(position)
                LOGGER.error("E4 sell failed", extra={"mint": mint, "signature": signature, "error": error})
                return
            after_tokens, after_sol = await asyncio.gather(self.rpc.token_balance(self.signer.wallet, mint), self.rpc.balance(self.signer.wallet))
            sold = max(0.0, live_tokens - after_tokens)
            position.remaining = max(0.0, min(position.remaining - sold, after_tokens))
            position.realized_sol += max(0.0, after_sol - before_sol)
            position.close_signature = signature
            if not position.first_partial_done and fraction < 0.999:
                position.first_partial_done = True
                position.first_partial_fraction = sold / live_tokens if live_tokens else fraction
            if after_tokens <= max(1e-9, position.tokens * 1e-8) or fraction >= 0.999:
                position.status = PositionStatus.CLOSED
                self.positions.pop(mint, None)
                self.store.save_position(position)
                self.spawn(self.sweep())
            else:
                position.status = PositionStatus.PARTIAL
                self.store.save_position(position)
            LOGGER.info("E4 exit executed", extra={"mint": mint, "fraction": fraction, "signature": signature})
        except Exception:
            LOGGER.exception("E4 sell execution error", extra={"mint": mint})
            position.status = PositionStatus.PARTIAL if position.first_partial_done else PositionStatus.OPEN
            self.store.save_position(position)
        finally:
            self.pending_exits.discard(mint)

    async def sweep(self) -> None:
        if not self.settings.vault or self.positions or self.pending_entries or self.pending_exits:
            return
        balance = await self.rpc.balance(self.signer.wallet)
        amount = balance - self.settings.operating_float_sol - self.settings.reserve_sol
        if amount < self.settings.sweep_min_sol:
            return
        priority, tip = self.fee_bid(amount, 1.0)
        amount -= priority + tip
        if amount < self.settings.sweep_min_sol:
            return
        request_id = str(uuid.uuid4())
        request = {
            "request_id": request_id, "side": "SWEEP", "mint": None, "public_key": self.signer.wallet,
            "amount": amount, "denominated_in_sol": True, "slippage_bps": 0,
            "priority_fee_sol": priority, "tip_sol": tip, "pool": "system",
            "metadata": {"destination": self.settings.vault},
        }
        self.store.order(request_id, None, "SWEEP", amount, None, "automatic excess SOL sweep")
        try:
            signature, confirmed, _, error = await self.execute(request_id, request)
            if not confirmed:
                LOGGER.error("E4 vault sweep failed", extra={"error": error})
            else:
                LOGGER.info("E4 excess SOL swept", extra={"amount_sol": amount, "signature": signature})
        except Exception:
            LOGGER.exception("E4 vault sweep error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gambit Jr autonomous E4 execution engine")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    sub.add_parser("migrate")
    sub.add_parser("status")
    return parser


async def run_engine(settings: Settings) -> None:
    engine = Engine(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await engine.run()
    finally:
        engine.store.close()


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=os.getenv("E4_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "migrate":
        Store(settings.execution_db).close()
        print(json.dumps({"migrated": True, "database": str(settings.execution_db)}))
        return
    if args.command == "status":
        store = Store(settings.execution_db)
        try:
            print(json.dumps(store.status(), indent=2))
        finally:
            store.close()
        return
    if not args.live:
        raise SystemExit("E4 live execution requires --live")
    settings.live = True
    settings.validate()
    asyncio.run(run_engine(settings))


if __name__ == "__main__":
    main()
