"""Durable exactly-once execution journal for V12 live transactions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_execution_journal(
    idempotency_key TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    side TEXT NOT NULL,
    mint TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    signed_tx_b64 TEXT,
    signature TEXT,
    route TEXT,
    slot INTEGER,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v12_execution_state
ON v12_execution_journal(state,updated_ns);
"""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    idempotency_key: str
    request_id: str
    side: str
    mint: str | None
    payload_hash: str
    payload: dict[str, Any]
    state: str
    signed_tx_b64: str | None
    signature: str | None
    route: str | None
    slot: int | None
    error: str | None
    attempts: int


def canonical_payload(request: Mapping[str, Any]) -> str:
    return json.dumps(dict(request), sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(request).encode()).hexdigest()


def idempotency_key(request_id: str, request: Mapping[str, Any]) -> str:
    side = str(request.get("side") or "")
    mint = str(request.get("mint") or "")
    return hashlib.sha256(f"{request_id}|{side}|{mint}".encode()).hexdigest()


class ExecutionJournal:
    def __init__(self, database: Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(
                database,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    def _row(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM v12_execution_journal WHERE idempotency_key=?",
            (key,),
        ).fetchone()

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            idempotency_key=str(row["idempotency_key"]),
            request_id=str(row["request_id"]),
            side=str(row["side"]),
            mint=str(row["mint"]) if row["mint"] is not None else None,
            payload_hash=str(row["payload_hash"]),
            payload=json.loads(str(row["payload_json"])),
            state=str(row["state"]),
            signed_tx_b64=str(row["signed_tx_b64"]) if row["signed_tx_b64"] else None,
            signature=str(row["signature"]) if row["signature"] else None,
            route=str(row["route"]) if row["route"] else None,
            slot=int(row["slot"]) if row["slot"] is not None else None,
            error=str(row["error"]) if row["error"] else None,
            attempts=int(row["attempts"]),
        )

    def prepare(self, request_id: str, request: Mapping[str, Any]) -> JournalEntry:
        key = idempotency_key(request_id, request)
        digest = payload_hash(request)
        now = time.time_ns()
        encoded = canonical_payload(request)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._row(key)
            if existing is not None:
                if str(existing["payload_hash"]) != digest:
                    raise RuntimeError("idempotency key reused with different payload")
                self.conn.execute("COMMIT")
                return self._entry(existing)
            self.conn.execute(
                """
                INSERT INTO v12_execution_journal(
                    idempotency_key,request_id,side,mint,payload_hash,payload_json,
                    state,created_ns,updated_ns
                ) VALUES(?,?,?,?,?,?,'PREPARED',?,?)
                """,
                (
                    key,
                    request_id,
                    str(request.get("side") or ""),
                    request.get("mint"),
                    digest,
                    encoded,
                    now,
                    now,
                ),
            )
            row = self._row(key)
            self.conn.execute("COMMIT")
            if row is None:
                raise RuntimeError("execution journal insert committed without a readable row")
            return self._entry(row)
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def mark_signed(
        self,
        key: str,
        *,
        signed_tx_b64: str,
        signature: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE v12_execution_journal
            SET state='SIGNED',signed_tx_b64=?,signature=?,updated_ns=?
            WHERE idempotency_key=? AND state IN ('PREPARED','SIGNED')
            """,
            (signed_tx_b64, signature, time.time_ns(), key),
        )

    def mark_submitted(self, key: str) -> None:
        self.conn.execute(
            """
            UPDATE v12_execution_journal
            SET state='SUBMITTED',attempts=attempts+1,updated_ns=?
            WHERE idempotency_key=? AND state IN ('SIGNED','SUBMITTED','UNCERTAIN')
            """,
            (time.time_ns(), key),
        )

    def mark_confirmed(self, key: str, *, route: str, slot: int | None) -> None:
        self.conn.execute(
            """
            UPDATE v12_execution_journal
            SET state='CONFIRMED',route=?,slot=?,error=NULL,updated_ns=?
            WHERE idempotency_key=?
            """,
            (route, slot, time.time_ns(), key),
        )

    def mark_failed(self, key: str, error: str, *, terminal: bool = False) -> None:
        self.conn.execute(
            """
            UPDATE v12_execution_journal
            SET state=?,error=?,updated_ns=?
            WHERE idempotency_key=? AND state NOT IN ('CONFIRMED','FAILED_TERMINAL')
            """,
            (
                "FAILED_TERMINAL" if terminal else "UNCERTAIN",
                str(error)[:2000],
                time.time_ns(),
                key,
            ),
        )

    def get(self, key: str) -> JournalEntry | None:
        row = self._row(key)
        return self._entry(row) if row is not None else None

    def by_request(self, request_id: str) -> JournalEntry | None:
        row = self.conn.execute(
            "SELECT * FROM v12_execution_journal WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return self._entry(row) if row is not None else None

    def recoverable(
        self,
        states: tuple[str, ...] = ("SIGNED", "SUBMITTED", "UNCERTAIN"),
    ) -> list[JournalEntry]:
        allowed = {"SIGNED", "SUBMITTED", "UNCERTAIN"}
        selected = {state for state in states if state in allowed}
        if not selected:
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM v12_execution_journal
            WHERE state IN ('SIGNED','SUBMITTED','UNCERTAIN')
            ORDER BY created_ns
            """
        ).fetchall()
        return [
            self._entry(row)
            for row in rows
            if str(row["state"]) in selected
        ]

    def unresolved_for(self, side: str, mint: str | None) -> list[JournalEntry]:
        rows = self.conn.execute(
            """
            SELECT * FROM v12_execution_journal
            WHERE state IN ('SIGNED','SUBMITTED','UNCERTAIN')
              AND side=?
              AND ((? IS NULL AND mint IS NULL) OR mint=?)
            ORDER BY created_ns
            """,
            (str(side), mint, mint),
        ).fetchall()
        return [self._entry(row) for row in rows]

    def state_counts(self) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in self.conn.execute(
                "SELECT state,COUNT(*) FROM v12_execution_journal GROUP BY state"
            )
        }
