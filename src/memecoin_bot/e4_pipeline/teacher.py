from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import CopySignal, OracleTrade
from .registry import AtomicCreatorRegistry

LOGGER = logging.getLogger("gambit.e4.pipeline.teacher")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS e4_teacher_trades(
  mint TEXT PRIMARY KEY, creator TEXT, opened_ns INTEGER NOT NULL,
  entry_sol REAL NOT NULL, entry_tokens REAL NOT NULL,
  sold_sol REAL NOT NULL DEFAULT 0, sold_tokens REAL NOT NULL DEFAULT 0,
  signature TEXT, last_event_ns INTEGER NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0, gross_pnl_sol REAL
);
CREATE TABLE IF NOT EXISTS e4_creator_scan_queue(
  creator TEXT PRIMARY KEY, first_seen_ns INTEGER NOT NULL,
  last_seen_ns INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'PENDING', last_error TEXT
);
CREATE TABLE IF NOT EXISTS e4_teacher_outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, creator TEXT NOT NULL,
  mint TEXT NOT NULL, won INTEGER NOT NULL, gross_pnl_sol REAL NOT NULL,
  closed_ns INTEGER NOT NULL, UNIQUE(creator,mint)
);
"""


def _kind(event: Any) -> str:
    value = getattr(event, "kind", "")
    return str(getattr(value, "value", value)).upper()


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and abs(result) != float("inf") else 0.0


class E4Teacher:
    """Turns observed E4 trades into future creator intelligence."""

    def __init__(self, *, registry: AtomicCreatorRegistry, database_path: Path, oracle_wallet: str, scan_command: str = "", copy_ttl_ms: float = 120.0) -> None:
        self.registry = registry
        self.oracle_wallet = oracle_wallet
        self.scan_command = tuple(shlex.split(scan_command))
        self.copy_ttl_ns = max(1, int(copy_ttl_ms * 1_000_000))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(database_path, timeout=2.0, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=2000")
        self.open_trades: dict[str, OracleTrade] = {}
        self.copy_signals: dict[str, CopySignal] = {}
        self._load_open()

    def _load_open(self) -> None:
        for row in self.conn.execute("SELECT * FROM e4_teacher_trades WHERE closed=0"):
            self.open_trades[str(row["mint"])] = OracleTrade(
                mint=str(row["mint"]), creator=str(row["creator"]) if row["creator"] else None,
                opened_ns=int(row["opened_ns"]), entry_sol=float(row["entry_sol"]),
                entry_tokens=float(row["entry_tokens"]), signature=str(row["signature"]) if row["signature"] else None,
                sold_sol=float(row["sold_sol"]), sold_tokens=float(row["sold_tokens"]),
                last_event_ns=int(row["last_event_ns"]), closed=False,
            )

    def close(self) -> None:
        self.conn.close()

    def pre_signal(self, event: Any, creator: str | None = None) -> CopySignal | None:
        if str(getattr(event, "trader", "") or "") != self.oracle_wallet or _kind(event) not in {"BUY", "PUMPSWAP_BUY"}:
            return None
        mint = str(getattr(event, "mint", "") or "")
        if not mint:
            return None
        observed_ns = int(getattr(event, "received_ns", 0) or time.time_ns())
        signal = CopySignal(
            mint=mint, creator=creator or getattr(event, "creator", None), observed_ns=observed_ns,
            e4_entry_price_sol=_number(getattr(event, "price_sol", None)) or None,
            e4_entry_sol=max(0.0, _number(getattr(event, "sol_amount", 0.0))),
            signature=getattr(event, "signature", None),
        )
        self.copy_signals[mint] = signal
        return signal

    def observe(self, event: Any, state: Any) -> None:
        if str(getattr(event, "trader", "") or "") != self.oracle_wallet:
            return
        kind = _kind(event)
        if kind not in {"BUY", "PUMPSWAP_BUY", "SELL", "PUMPSWAP_SELL"}:
            return
        mint = str(getattr(event, "mint", "") or "")
        if not mint:
            return
        now_ns = int(getattr(event, "received_ns", 0) or time.time_ns())
        creator = getattr(state, "creator", None) or getattr(event, "creator", None)
        sol = max(0.0, _number(getattr(event, "sol_amount", 0.0)))
        tokens = max(0.0, _number(getattr(event, "token_amount", 0.0)))
        if kind in {"BUY", "PUMPSWAP_BUY"}:
            if mint in self.open_trades:
                return
            trade = OracleTrade(mint=mint, creator=str(creator) if creator else None, opened_ns=now_ns, entry_sol=sol, entry_tokens=tokens, signature=getattr(event, "signature", None), last_event_ns=now_ns)
            self.open_trades[mint] = trade
            self.conn.execute(
                """INSERT INTO e4_teacher_trades(mint,creator,opened_ns,entry_sol,entry_tokens,signature,last_event_ns)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(mint) DO NOTHING""",
                (mint, trade.creator, trade.opened_ns, trade.entry_sol, trade.entry_tokens, trade.signature, trade.last_event_ns),
            )
            if trade.creator and self.registry.get(trade.creator) is None:
                self.enqueue_scan(trade.creator, now_ns)
            return
        trade = self.open_trades.get(mint)
        if trade is None:
            return
        trade.sold_sol += sol
        trade.sold_tokens += tokens
        trade.last_event_ns = now_ns
        self.conn.execute("UPDATE e4_teacher_trades SET sold_sol=?,sold_tokens=?,last_event_ns=? WHERE mint=?", (trade.sold_sol, trade.sold_tokens, trade.last_event_ns, mint))
        if trade.sold_fraction >= 0.975:
            self._finalize(trade, now_ns)

    def reap(self, now_ns: int | None = None) -> int:
        now = now_ns or time.time_ns()
        closed = 0
        for trade in tuple(self.open_trades.values()):
            if trade.sold_fraction >= 0.90 and trade.sold_tokens > 0 and now - trade.last_event_ns >= 2_000_000_000:
                self._finalize(trade, now)
                closed += 1
        for mint, signal in tuple(self.copy_signals.items()):
            if now - signal.observed_ns > self.copy_ttl_ns:
                self.copy_signals.pop(mint, None)
        return closed

    def _finalize(self, trade: OracleTrade, closed_ns: int) -> None:
        if trade.closed:
            return
        trade.closed = True
        pnl = trade.gross_pnl_sol
        won = pnl > 0
        if trade.creator:
            self.registry.record_outcome(trade.creator, won=won, gross_pnl_sol=pnl)
            self.conn.execute("INSERT OR IGNORE INTO e4_teacher_outcomes(creator,mint,won,gross_pnl_sol,closed_ns) VALUES(?,?,?,?,?)", (trade.creator, trade.mint, int(won), pnl, closed_ns))
        self.conn.execute("UPDATE e4_teacher_trades SET closed=1,gross_pnl_sol=?,last_event_ns=? WHERE mint=?", (pnl, closed_ns, trade.mint))
        self.open_trades.pop(trade.mint, None)

    def copy_signal(self, mint: str, *, now_ns: int, current_price_sol: float | None, max_age_ms: float, max_drift_bps: float) -> CopySignal | None:
        signal = self.copy_signals.get(mint)
        if signal is None:
            return None
        age_ns = now_ns - signal.observed_ns
        if age_ns < 0 or age_ns > max_age_ms * 1_000_000:
            return None
        if current_price_sol and signal.e4_entry_price_sol and signal.e4_entry_price_sol > 0:
            drift = (current_price_sol / signal.e4_entry_price_sol - 1.0) * 10_000
            if drift > max_drift_bps:
                return None
        return signal

    def enqueue_scan(self, creator: str, now_ns: int | None = None) -> None:
        now = now_ns or time.time_ns()
        self.conn.execute(
            """INSERT INTO e4_creator_scan_queue(creator,first_seen_ns,last_seen_ns,status)
               VALUES(?,?,?,'PENDING') ON CONFLICT(creator) DO UPDATE SET
               last_seen_ns=excluded.last_seen_ns,
               status=CASE WHEN e4_creator_scan_queue.status='COMPLETE' THEN 'COMPLETE' ELSE 'PENDING' END""",
            (creator, now, now),
        )

    def pending_scans(self, limit: int = 20) -> list[str]:
        return [str(row[0]) for row in self.conn.execute("SELECT creator FROM e4_creator_scan_queue WHERE status IN ('PENDING','RETRY') ORDER BY last_seen_ns LIMIT ?", (limit,))]

    async def scan_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.scan_command:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            creators = self.pending_scans(limit=8)
            if not creators:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                continue
            for creator in creators:
                if stop.is_set():
                    break
                self.conn.execute("UPDATE e4_creator_scan_queue SET status='RUNNING',attempts=attempts+1 WHERE creator=?", (creator,))
                try:
                    process = await asyncio.create_subprocess_exec(*self.scan_command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps({"creator": creator}).encode() + b"\n"), timeout=float(os.getenv("E4_HISTORY_SCANNER_TIMEOUT_SECONDS", "120")))
                    if process.returncode:
                        raise RuntimeError(stderr.decode(errors="replace")[-1000:])
                    result = json.loads(stdout)
                    self.registry.apply_scanner_profile(creator, launch_count=int(result.get("launch_count") or 0), runner_count=int(result.get("runner_count") or 0), score=float(result.get("score") or 0.0), social_handles=result.get("social_handles") or (), evidence=result)
                    self.conn.execute("UPDATE e4_creator_scan_queue SET status='COMPLETE',last_error=NULL WHERE creator=?", (creator,))
                except Exception as exc:
                    LOGGER.exception("E4 creator history scan failed creator=%s", creator)
                    self.conn.execute("UPDATE e4_creator_scan_queue SET status='RETRY',last_error=? WHERE creator=?", (str(exc)[:1000], creator))
                    await asyncio.sleep(0.1)

    def stats(self) -> dict[str, Any]:
        return {
            "open_oracle_trades": len(self.open_trades),
            "active_copy_signals": len(self.copy_signals),
            "pending_scans": self.conn.execute("SELECT COUNT(*) FROM e4_creator_scan_queue WHERE status IN ('PENDING','RETRY')").fetchone()[0],
            "completed_scans": self.conn.execute("SELECT COUNT(*) FROM e4_creator_scan_queue WHERE status='COMPLETE'").fetchone()[0],
            "outcomes": self.conn.execute("SELECT COUNT(*) FROM e4_teacher_outcomes").fetchone()[0],
        }
