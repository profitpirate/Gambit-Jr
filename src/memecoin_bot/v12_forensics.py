"""Structured per-trade forensic timeline for V12."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_forensic_trace(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_key TEXT NOT NULL,
    mint TEXT,
    request_id TEXT,
    phase TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v12_trace_key
ON v12_forensic_trace(trace_key,created_ns);
CREATE INDEX IF NOT EXISTS idx_v12_trace_mint
ON v12_forensic_trace(mint,created_ns);
"""


class ForensicTraceStore:
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

    def record(
        self,
        *,
        trace_key: str,
        phase: str,
        payload: Mapping[str, Any],
        mint: str | None = None,
        request_id: str | None = None,
        created_ns: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO v12_forensic_trace(
                trace_key,mint,request_id,phase,payload_json,created_ns
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                str(trace_key),
                str(mint) if mint else None,
                str(request_id) if request_id else None,
                str(phase),
                json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str),
                int(created_ns or time.time_ns()),
            ),
        )

    def timeline(self, trace_key: str) -> list[dict[str, Any]]:
        return [
            {
                "id": int(row["id"]),
                "phase": str(row["phase"]),
                "mint": row["mint"],
                "request_id": row["request_id"],
                "payload": json.loads(str(row["payload_json"])),
                "created_ns": int(row["created_ns"]),
            }
            for row in self.conn.execute(
                """
                SELECT * FROM v12_forensic_trace
                WHERE trace_key=?
                ORDER BY created_ns,id
                """,
                (str(trace_key),),
            )
        ]

    def prune(self, *, retention_days: float = 90.0) -> int:
        cutoff = time.time_ns() - int(max(7.0, retention_days) * 86_400 * 1e9)
        before = self.conn.total_changes
        self.conn.execute(
            "DELETE FROM v12_forensic_trace WHERE created_ns<?",
            (cutoff,),
        )
        return self.conn.total_changes - before
