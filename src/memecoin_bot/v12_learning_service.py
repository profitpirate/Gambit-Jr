"""Continuous shadow launch evidence capture for creator apprenticeship."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_learning_launches(
    mint TEXT PRIMARY KEY,
    creator TEXT NOT NULL,
    create_ns INTEGER NOT NULL,
    initial_price_sol REAL NOT NULL,
    latest_price_sol REAL NOT NULL,
    peak_price_sol REAL NOT NULL,
    latest_ns INTEGER NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0,
    runner_2x INTEGER NOT NULL DEFAULT 0,
    runner_3x INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v12_learning_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    received_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    price_sol REAL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v12_learning_events_mint
ON v12_learning_events(mint,received_ns);
"""


class ContinuousLearningStore:
    """Records every live launch/price path; never grants live entry authority."""

    def __init__(self, database: Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(database, timeout=5, isolation_level=None, check_same_thread=False)
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    def observe(self, event: Any) -> None:
        mint = str(getattr(event, "mint", "") or "")
        if not mint:
            return
        kind_obj = getattr(event, "kind", "")
        kind = str(getattr(kind_obj, "value", kind_obj))
        received_ns = int(
            getattr(event, "received_ns", 0)
            or getattr(event, "source_ns", 0)
            or time.time_ns()
        )
        price = float(getattr(event, "price_sol", 0.0) or 0.0)
        creator = str(getattr(event, "creator", "") or "")
        if kind == "CREATE" and creator and price > 0:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO v12_learning_launches(
                    mint,creator,create_ns,initial_price_sol,latest_price_sol,
                    peak_price_sol,latest_ns
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (mint, creator, received_ns, price, price, price, received_ns),
            )
        if price > 0:
            self.conn.execute(
                """
                UPDATE v12_learning_launches
                SET latest_price_sol=?,peak_price_sol=MAX(peak_price_sol,?),
                    latest_ns=MAX(latest_ns,?)
                WHERE mint=?
                """,
                (price, price, received_ns, mint),
            )
        payload = {
            "signature": getattr(event, "signature", None),
            "trader": getattr(event, "trader", None),
            "sol_amount": getattr(event, "sol_amount", None),
            "token_amount": getattr(event, "token_amount", None),
            "creator": creator or None,
        }
        self.conn.execute(
            "INSERT INTO v12_learning_events(mint,received_ns,kind,price_sol,payload_json) VALUES(?,?,?,?,?)",
            (
                mint,
                received_ns,
                kind,
                price if price > 0 else None,
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
            ),
        )
        self.finalize_ready(received_ns)

    def finalize_ready(self, now_ns: int | None = None, horizon_seconds: float = 60.0) -> int:
        now = int(now_ns or time.time_ns())
        cutoff = now - int(horizon_seconds * 1e9)
        rows = self.conn.execute(
            """
            SELECT * FROM v12_learning_launches
            WHERE finalized=0 AND create_ns<=?
            LIMIT 500
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            initial = float(row["initial_price_sol"])
            multiple = float(row["peak_price_sol"]) / initial if initial > 0 else 0.0
            self.conn.execute(
                """
                UPDATE v12_learning_launches
                SET finalized=1,runner_2x=?,runner_3x=?
                WHERE mint=?
                """,
                (int(multiple >= 2.0), int(multiple >= 3.0), str(row["mint"])),
            )
        return len(rows)

    def maintenance(
        self,
        *,
        now_ns: int | None = None,
        event_retention_days: float = 7.0,
        launch_retention_days: float = 180.0,
    ) -> dict[str, int]:
        now = int(now_ns or time.time_ns())
        event_cutoff = now - int(max(1.0, event_retention_days) * 86_400 * 1e9)
        launch_cutoff = now - int(max(event_retention_days, launch_retention_days) * 86_400 * 1e9)
        before_events = self.conn.total_changes
        self.conn.execute(
            """
            DELETE FROM v12_learning_events
            WHERE received_ns<? AND mint IN(
                SELECT mint FROM v12_learning_launches WHERE finalized=1
            )
            """,
            (event_cutoff,),
        )
        event_deleted = self.conn.total_changes - before_events
        before_launches = self.conn.total_changes
        self.conn.execute(
            "DELETE FROM v12_learning_launches WHERE finalized=1 AND latest_ns<?",
            (launch_cutoff,),
        )
        launch_deleted = self.conn.total_changes - before_launches
        return {
            "events_deleted": event_deleted,
            "launches_deleted": launch_deleted,
        }

    def export_unprocessed(self, limit: int = 10_000) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT * FROM v12_learning_launches
                WHERE finalized=1
                ORDER BY create_ns DESC
                LIMIT ?
                """,
                (int(limit),),
            )
        ]
