"""Owner-only operation intents. No shell interpolation or automatic retries."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

ACTIONS = {
    "start",
    "stop",
    "restart",
    "check",
    "transfer",
    "configure",
    "update",
    "live_test",
    "recover",
    "reset_safety",
    "acknowledge",
}


def lamports(text):
    if not isinstance(text, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,7})(\.[0-9]{1,9})?", text):
        raise ValueError("Enter a positive SOL amount with at most 9 decimal places.")
    try:
        result = int(Decimal(text) * 1_000_000_000)
    except (InvalidOperation, OverflowError):
        raise ValueError("Invalid SOL amount.") from None
    if result <= 0:
        raise ValueError("Amount must be greater than zero.")
    return result


def validate(action, payload):
    if action not in ACTIONS or not isinstance(payload, dict):
        raise ValueError("Unsupported operation.")
    if action == "transfer":
        from solders.pubkey import Pubkey

        if set(payload) != {"destination", "amount_sol"}:
            raise ValueError("Transfer requires an exact destination and SOL amount.")
        try:
            destination = Pubkey.from_string(payload["destination"])
        except (ValueError, TypeError):
            raise ValueError("Invalid Solana destination address.") from None
        if not destination.is_on_curve():
            raise ValueError(
                "Destination must be an ordinary wallet address, not a program address."
            )
        return {"destination": str(destination), "amount_lamports": lamports(payload["amount_sol"])}
    if action == "configure":
        if set(payload) != {"setting", "value"}:
            raise ValueError("Specify one supported setting and value.")
        bounds = {"position_percent": (1, 10), "maximum_positions": (1, 2)}
        key, value = payload["setting"], payload["value"]
        if (
            key not in bounds
            or type(value) is not int
            or not bounds[key][0] <= value <= bounds[key][1]
        ):
            raise ValueError("Supported limits: position_percent 1–10; maximum_positions 1–2.")
        return {"setting": key, "value": value}
    if action == "acknowledge":
        if set(payload) != {"operation_id"} or not re.fullmatch(
            r"[a-f0-9]{32}", payload["operation_id"]
        ):
            raise ValueError("Provide the exact unresolved operation identifier.")
        return dict(payload)
    if action == "update":
        if set(payload) != {"release_id"} or not re.fullmatch(
            r"[a-f0-9]{64}", payload["release_id"]
        ):
            raise ValueError("Choose the exact SHA-256 of a locally staged release.")
        return dict(payload)
    if action == "live_test":
        if payload:
            raise ValueError("The execution test has a fixed 0.01 SOL maximum budget.")
        return {"maximum_total_lamports": 10_000_000}
    if payload:
        raise ValueError("This operation accepts no extra parameters.")
    return {}


class OperationLedger:
    def __init__(self, path, *, owner_id, clock=time.time_ns, recover_interrupted=False):
        if not isinstance(owner_id, str) or not re.fullmatch(r"[0-9]{15,22}", owner_id):
            raise ValueError("An explicit Discord owner user ID is required.")
        self.owner_id, self.clock = owner_id, clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
            "action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created_ns INTEGER NOT NULL, "
            "expires_ns INTEGER NOT NULL, result TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS operation_events (id INTEGER PRIMARY KEY, operation_id TEXT, "
            "event TEXT NOT NULL, observed_ns INTEGER NOT NULL)"
        )
        if recover_interrupted:
            rows = self.db.execute("SELECT id FROM operations WHERE state='RUNNING'").fetchall()
            for row in rows:
                self.db.execute("UPDATE operations SET state='UNKNOWN' WHERE id=?", (row["id"],))
                self.event(row["id"], "GATEWAY_RESTART_REQUIRES_REVIEW")

    def authorize(self, user_id):
        if str(user_id) != self.owner_id:
            raise PermissionError("Only the configured owner can access bot controls.")

    def propose(self, user_id, action, payload):
        self.authorize(user_id)
        safe = validate(action, payload)
        now, identity = self.clock(), secrets.token_hex(16)
        self.db.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
            (
                identity,
                self.owner_id,
                action,
                json.dumps(safe, sort_keys=True),
                "PENDING",
                now,
                now + 120_000_000_000,
                None,
            ),
        )
        self.event(identity, "PROPOSED")
        return {
            "id": identity,
            "action": action,
            "payload": safe,
            "expires_ns": now + 120_000_000_000,
        }

    def event(self, identity, name):
        self.db.execute(
            "INSERT INTO operation_events(operation_id,event,observed_ns) VALUES(?,?,?)",
            (identity, name, self.clock()),
        )

    def claim(self, user_id, identity):
        self.authorize(user_id)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=? AND owner=?", (identity, self.owner_id)
            ).fetchone()
            if (
                not row
                or row["state"] != "PENDING"
                or not row["created_ns"] <= self.clock() <= row["expires_ns"]
            ):
                raise ValueError(
                    "This confirmation expired or has already been used. It will not run again."
                )
            active = self.db.execute(
                "SELECT 1 FROM operations WHERE state IN ('RUNNING','UNKNOWN') LIMIT 1"
            ).fetchone()
            if active and row["action"] not in {"stop", "recover", "acknowledge"}:
                raise RuntimeError("A previous operation is running or requires reconciliation.")
            self.db.execute("UPDATE operations SET state='RUNNING' WHERE id=?", (identity,))
            self.event(identity, "CONFIRMED_AND_CLAIMED")
            self.db.execute("COMMIT")
            return {"id": identity, "action": row["action"], "payload": json.loads(row["payload"])}
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, identity, state, result):
        if state not in ("COMPLETE", "FAILED", "UNKNOWN"):
            raise ValueError("Invalid final operation state.")
        changed = self.db.execute(
            "UPDATE operations SET state=?,result=? WHERE id=? AND state=?",
            (state, json.dumps(result), identity, "RUNNING"),
        )
        if changed.rowcount != 1:
            raise RuntimeError("Operation is not running; its result was not overwritten.")
        self.event(identity, state)

    def history(self, user_id, limit=20):
        self.authorize(user_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,action,state,created_ns,result FROM operations "
                "ORDER BY created_ns DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            )
        ]

    def acknowledge(self, user_id, identity):
        self.authorize(user_id)
        changed = self.db.execute(
            "UPDATE operations SET state='ACKNOWLEDGED' WHERE id=? AND owner=? AND state='UNKNOWN'",
            (identity, self.owner_id),
        )
        if changed.rowcount != 1:
            raise ValueError("Only an unresolved operation can be acknowledged.")
        self.event(identity, "OWNER_REVIEWED_AFTER_FLAT_WALLET_CHECK")
