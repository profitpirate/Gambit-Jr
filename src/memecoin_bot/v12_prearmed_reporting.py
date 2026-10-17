"""Receipt-based reporting and durable notification delivery; no trading APIs."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
CHANNELS = ("discord", "phone")


class NoticeOutbox:
    def __init__(self, conn):
        self.conn = conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prearmed_notice_messages(
                id TEXT PRIMARY KEY, created_ns INTEGER NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS prearmed_notice_delivery(
                id TEXT NOT NULL, channel TEXT NOT NULL, delivered_ns INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0, next_ns INTEGER NOT NULL DEFAULT 0,
                error_type TEXT, PRIMARY KEY(id,channel));
        """)

    def enqueue(self, identity, message, now_ns):
        # All messages fit both Discord and Pushover without cutting off a receipt.
        if len(message) > 1000:
            raise ValueError("NOTICE_TOO_LONG")
        own = not self.conn.in_transaction
        if own:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT OR IGNORE INTO prearmed_notice_messages VALUES(?,?,?)",
                              (identity, now_ns, message))
            for channel in CHANNELS:
                self.conn.execute("INSERT OR IGNORE INTO prearmed_notice_delivery(id,channel) VALUES(?,?)",
                                  (identity, channel))
            if own:
                self.conn.commit()
        except Exception:
            if own:
                self.conn.rollback()
            raise

    def pending(self):
        return self.conn.execute("SELECT COUNT(*) FROM prearmed_notice_delivery WHERE delivered_ns IS NULL").fetchone()[0]

    async def flush(self, notices, *, now_ns=None, limit=20):
        now_ns = time.time_ns() if now_ns is None else now_ns
        rows = [row for channel in CHANNELS for row in self.conn.execute("""SELECT d.id,d.channel,d.attempts,m.message,d.next_ns
            FROM prearmed_notice_delivery d JOIN prearmed_notice_messages m ON d.id=m.id
            WHERE d.delivered_ns IS NULL AND d.channel=? ORDER BY m.created_ns,m.rowid LIMIT ?""",
            (channel, limit)).fetchall()]
        failed = []
        async def deliver(row):
            identity, channel, attempts, message, _next_ns = row
            try:
                await getattr(notices, "send_discord" if channel == "discord" else "send_pushover")(message)
            except Exception as error:
                self.conn.execute("""UPDATE prearmed_notice_delivery SET attempts=attempts+1,
                    next_ns=?,error_type=? WHERE id=? AND channel=?""",
                    (now_ns + min(300, 2 ** min(8, attempts + 1)) * 10**9,
                     type(error).__name__, identity, channel))
                failed.append(channel)
                return False
            else:
                self.conn.execute("""UPDATE prearmed_notice_delivery SET delivered_ns=?,
                    attempts=attempts+1,error_type=NULL WHERE id=? AND channel=?""",
                    (now_ns, identity, channel))
                return True
        async def channel_batch(channel):
            for row in rows:
                if row[1] == channel:
                    if row[4] > now_ns or not await deliver(row):
                        break
        # Preserve buy-before-sell order within each channel, without making a
        # slow phone endpoint block Discord. Completed channels are not retried.
        await asyncio.gather(*(channel_batch(channel) for channel in CHANNELS))
        return sorted(set(failed))


class TradeReports:
    def __init__(self, conn, state, *, now_ns=None):
        self.conn = conn
        self.outbox = NoticeOutbox(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prearmed_report_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS prearmed_trade_reports(
                request_id TEXT PRIMARY KEY, observed_ns INTEGER NOT NULL, kind TEXT NOT NULL,
                test INTEGER NOT NULL, closed INTEGER NOT NULL, pnl_lamports INTEGER NOT NULL,
                failed_buy_fee INTEGER NOT NULL, payload TEXT NOT NULL);
        """)
        baseline = {"started_ns": time.time_ns() if now_ns is None else now_ns,
                    "closed": state.get("closed", 0), "realized_lamports": state.get("realized_lamports", 0)}
        conn.execute("INSERT OR IGNORE INTO prearmed_report_meta VALUES('baseline',?)", (json.dumps(baseline),))

    def record_result(self, request_id, order, result, previous, current, event, now_ns):
        """Called inside the existing fill-state transaction; rollback/recovery is shared."""
        from .v12_prearmed_notices import format_notice
        name, details = event
        payload = dict(details)
        mint, side = order["payload"]["mint"], order["payload"]["side"]
        old_position = previous.get("positions", {}).get(mint, {})
        context = order.get("context", {})
        test = bool(context.get("test_operation_id") or old_position.get("test_operation_id"))
        payload.update(observed_ns=now_ns, test_operation=test,
                       trading_mode=current.get("mode"), exit_reason=order.get("reason"),
                       creator=context.get("creator") or old_position.get("creator"),
                       all_time_closed=current.get("closed", 0),
                       all_time_pnl_lamports=current.get("realized_lamports", 0))
        closed = False
        pnl = 0
        failed_buy_fee = 0
        if name == "FILL_CONFIRMED":
            delta = result["sol_delta_lamports"]
            position = current.get("positions", {}).get(mint, {})
            entry = self.conn.execute("SELECT payload FROM prearmed_trade_reports WHERE kind='BUY' ORDER BY observed_ns DESC").fetchall()
            entry = next((json.loads(r[0]) for r in entry if json.loads(r[0]).get("mint") == mint), {})
            entry_debit = -delta if side == "BUY" else entry.get("entry_debit_lamports")
            closed = side == "SELL" and mint not in current.get("positions", {})
            pnl = old_position.get("pnl_lamports", 0) + delta if closed else 0
            remaining = position.get("remaining", 0)
            original = position.get("original", old_position.get("original", 0))
            cashflow = position.get("pnl_lamports", pnl)
            allocated_pnl = None
            if side == "SELL" and entry_debit is not None and original:
                # Informational realised amount for the sold portion. Final P/L uses
                # the runtime's exact accumulated cash flow, including failed sells.
                allocated_pnl = delta - entry_debit * result["tokens_raw"] // original
            payload.update(sol_delta_lamports=delta, entry_debit_lamports=entry_debit,
                           position_closed=closed, remaining_tokens_raw=remaining,
                           original_tokens_raw=original, position_cashflow_lamports=cashflow,
                           trade_pnl_lamports=pnl if closed else None,
                           sale_pnl_lamports=allocated_pnl,
                           hold_ms=max(0, now_ns-old_position.get("exit", {}).get("fill_ns", now_ns))/1e6)
        elif name == "ORDER_FAILED" and side == "BUY":
            failed_buy_fee = result.get("fee_lamports", 0)
        payload["mint"] = mint
        self.conn.execute("INSERT INTO prearmed_trade_reports VALUES(?,?,?,?,?,?,?,?)",
                          (request_id, now_ns, side if name == "FILL_CONFIRMED" else "FAILED_"+side,
                           int(test), int(closed), pnl, failed_buy_fee, json.dumps(payload)))
        if current.get("mode") == "live":
            text, _ = format_notice(name, payload, ts_ns=now_ns)
            self.outbox.enqueue("result:"+request_id, text, now_ns)
        return name, payload


def day_bounds(day):
    start = datetime.combine(day, datetime.min.time(), UK)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), UK)
    return int(start.timestamp()*1e9), int(end.timestamp()*1e9)


def daily_summary(live_path, day):
    """Closed trades are booked on their UK close date; open assets are not valued."""
    start, end = day_bounds(day)
    with sqlite3.connect(Path(live_path).resolve().as_uri()+"?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        state = json.loads(conn.execute("SELECT data FROM frozen_state WHERE id=1").fetchone()[0])
        baseline = json.loads(conn.execute("SELECT value FROM prearmed_report_meta WHERE key='baseline'").fetchone()[0])
        rows = conn.execute("""SELECT test,closed,pnl_lamports,failed_buy_fee,payload
            FROM prearmed_trade_reports WHERE observed_ns>=? AND observed_ns<? ORDER BY observed_ns""", (start,end)).fetchall()
        totals = conn.execute("SELECT COALESCE(SUM(closed),0),COALESCE(SUM(pnl_lamports),0) FROM prearmed_trade_reports WHERE test=0").fetchone()
        if (baseline['closed'] + totals[0] != state['closed']
                or baseline['realized_lamports'] + totals[1] != state['realized_lamports']):
            raise ValueError("REPORTING_TOTALS_DO_NOT_RECONCILE")
    organic = [r for r in rows if not r[0]]
    closed = [r for r in organic if r[1]]
    fees = sum(r[3] for r in organic)
    return {"day": day.isoformat(), "timezone": "Europe/London", "closed": len(closed),
            "wins": sum(r[2]>0 for r in closed), "losses": sum(r[2]<0 for r in closed),
            "breakeven": sum(r[2]==0 for r in closed),
            "closed_pnl_lamports": sum(r[2] for r in closed), "failed_buy_fees_lamports": fees,
            "net_pnl_lamports": sum(r[2] for r in closed)-fees,
            "test_closed": sum(r[1] for r in rows if r[0]),
            "test_pnl_lamports": sum(r[2]-r[3] for r in rows if r[0]),
            "all_time_closed": state["closed"], "all_time_pnl_lamports": state["realized_lamports"],
            "open_positions_now": len(state["positions"]), "pending_orders_now": len(state["pending"]),
            "coverage_started_ns": baseline["started_ns"], "partial_day": baseline["started_ns"]>start,
            "best_lamports": max((r[2] for r in closed),default=None),
            "worst_lamports": min((r[2] for r in closed),default=None)}


class DailyReporter:
    """Runs in the independent operator service, including while trading is stopped."""
    def __init__(self, stage):
        self.stage = Path(stage)
        self.conn = sqlite3.connect(self.stage/'operator-data/report-notices.sqlite', isolation_level=None)
        self.outbox = NoticeOutbox(self.conn)
        self.conn.execute("CREATE TABLE IF NOT EXISTS daily_report_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")

    async def tick(self, notices, *, now_ns=None):
        from .v12_prearmed_notices import format_daily
        now_ns = time.time_ns() if now_ns is None else now_ns
        now = datetime.fromtimestamp(now_ns/1e9, UK)
        self.conn.execute("INSERT OR IGNORE INTO daily_report_meta VALUES('first_day',?)", (now.date().isoformat(),))
        first = datetime.fromisoformat(self.conn.execute("SELECT value FROM daily_report_meta WHERE key='first_day'").fetchone()[0]).date()
        # 00:05 UK reports the complete prior local calendar day, across DST.
        last = now.date() - timedelta(days=1 if (now.hour,now.minute)>=(0,5) else 2)
        try:
            day = first
            while day <= last:
                identity = "daily:"+day.isoformat()
                if not self.conn.execute("SELECT 1 FROM prearmed_notice_messages WHERE id=?", (identity,)).fetchone():
                    report = daily_summary(self.stage/'prearmed-data/live.sqlite', day)
                    self.outbox.enqueue(identity, format_daily(report, now_ns=now_ns), now_ns)
                day += timedelta(days=1)
        finally:
            # A new report error must not strand previously queued messages.
            failed = await self.outbox.flush(notices, now_ns=now_ns)
        return failed
