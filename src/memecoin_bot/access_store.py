"""Persistent multi-user access/control-plane state for Gambit V12."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_login_grants(
    grant_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL,
    consumed_ns INTEGER,
    created_ns INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_sessions(
    session_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL,
    revoked_ns INTEGER,
    created_ns INTEGER NOT NULL,
    csrf_hash TEXT,
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_wallet_challenges(
    challenge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    message TEXT NOT NULL,
    expires_ns INTEGER NOT NULL,
    consumed_ns INTEGER,
    created_ns INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_wallets(
    user_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    verified_ns INTEGER NOT NULL,
    label TEXT,
    PRIMARY KEY(user_id,wallet),
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_execution_connections(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    mode TEXT NOT NULL,
    public_identifier TEXT,
    secret_ref TEXT,
    state TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL,
    UNIQUE(user_id,provider,public_identifier),
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_mandates(
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    max_active_bankroll_sol REAL NOT NULL DEFAULT 0,
    max_position_fraction REAL NOT NULL DEFAULT 0.10,
    max_concurrent_positions INTEGER NOT NULL DEFAULT 2,
    storage_wallet TEXT,
    updated_ns INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES access_users(id)
);
CREATE TABLE IF NOT EXISTS access_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
"""


class AccessStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(access_sessions)")
        }
        if "csrf_hash" not in columns:
            self.conn.execute("ALTER TABLE access_sessions ADD COLUMN csrf_hash TEXT")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    def user_for_discord(self, discord_user_id: str) -> sqlite3.Row:
        value = str(discord_user_id)
        now = time.time_ns()
        self.conn.execute(
            "INSERT OR IGNORE INTO access_users(discord_user_id,created_ns) VALUES(?,?)",
            (value, now),
        )
        row = self.conn.execute(
            "SELECT * FROM access_users WHERE discord_user_id=?",
            (value,),
        ).fetchone()
        assert row is not None
        return row

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM access_users WHERE id=?",
            (int(user_id),),
        ).fetchone()

    def audit(self, user_id: int | None, action: str, details_json: str = "{}") -> None:
        self.conn.execute(
            "INSERT INTO access_audit(user_id,action,details_json,created_ns) VALUES(?,?,?,?)",
            (user_id, action, details_json, time.time_ns()),
        )

    def account_snapshot(self, user_id: int) -> dict[str, Any]:
        user = self.user(user_id)
        if user is None:
            raise KeyError(user_id)
        wallets = [
            dict(row)
            for row in self.conn.execute(
                "SELECT wallet,verified_ns,label FROM access_wallets WHERE user_id=?",
                (user_id,),
            )
        ]
        connections = [
            {
                "id": row["id"],
                "provider": row["provider"],
                "mode": row["mode"],
                "public_identifier": row["public_identifier"],
                "state": row["state"],
            }
            for row in self.conn.execute(
                "SELECT * FROM access_execution_connections WHERE user_id=?",
                (user_id,),
            )
        ]
        mandate = self.conn.execute(
            "SELECT * FROM access_mandates WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return {
            "user": {
                "id": int(user["id"]),
                "discord_user_id": str(user["discord_user_id"]),
                "enabled": bool(user["enabled"]),
            },
            "wallets": wallets,
            "execution_connections": connections,
            "mandate": dict(mandate) if mandate else None,
        }
