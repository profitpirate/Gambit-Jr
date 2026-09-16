"""Local durable research state; NOT on-chain reconciliation or HA.

Each idempotent input and its resulting account snapshot commit atomically.
SQLITE synchronous=FULL is intentionally not weakened to improve a benchmark.
After an interrupted position, Runtime restores inventory but excludes it from
fully-forward certification. No historical-tail reconstruction is provided.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, initial: Mapping[str, Any] | None = None):
        self.db = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, sha TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, input TEXT NOT NULL,
            previous_sha TEXT NOT NULL, resulting_sha TEXT NOT NULL, event_sha TEXT NOT NULL);
        """)
        if initial is not None:
            payload = encode(initial)
            self.db.execute("INSERT OR IGNORE INTO state VALUES(1,?,?)", (payload, digest(payload)))
        self.read()

    def read(self) -> dict[str, Any]:
        row = self.db.execute("SELECT body,sha FROM state WHERE id=1").fetchone()
        if row is None: raise ValueError("no initialized journal state")
        if digest(row[0]) != row[1]: raise ValueError("corrupt persisted state")
        return json.loads(row[0])

    def apply(self, key: str, event: Mapping[str, Any], transition: Callable[[dict[str, Any]], Mapping[str, Any]],
              *, fault: Callable[[str], None] | None = None) -> bool:
        if not key: raise ValueError("empty idempotency key")
        inp = encode(event)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT input FROM events WHERE key=?", (key,)).fetchone()
            if prior:
                if prior[0] != inp: raise ValueError("same idempotency key has conflicting payload")
                self.db.execute("COMMIT"); return False
            current = self.read()
            row = self.db.execute("SELECT sha FROM state WHERE id=1").fetchone()
            previous = row[0]
            if fault: fault("BEFORE_TRANSITION")
            result = encode(transition(current)); result_sha = digest(result)
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            chained = digest(encode([seq, key, inp, previous, result_sha]))
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (seq,key,inp,previous,result_sha,chained))
            self.db.execute("UPDATE state SET body=?,sha=? WHERE id=1", (result,result_sha))
            if fault: fault("BEFORE_COMMIT")
            self.db.execute("COMMIT")
            if fault: fault("AFTER_COMMIT")
            return True
        except BaseException:
            if self.db.in_transaction: self.db.execute("ROLLBACK")
            raise

    def verify(self) -> dict[str, Any]:
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        previous = None; count = 0
        for seq,key,inp,prev,result,chain in self.db.execute("SELECT * FROM events ORDER BY seq"):
            if seq != count+1: raise ValueError("event gap")
            if previous is not None and previous != prev: raise ValueError("state chain mismatch")
            if digest(encode([seq,key,inp,prev,result])) != chain: raise ValueError("event checksum mismatch")
            previous = result; count += 1
        current = self.read()
        if previous is not None and previous != digest(encode(current)): raise ValueError("head mismatch")
        return {"status": "PASS", "events": count, "actual_chain_reconciliation": False}

    def close(self) -> None:
        self.db.close()
