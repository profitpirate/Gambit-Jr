from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from memecoin_bot.v12_marketdata import _marketdata_db_snapshot


def test_marketdata_snapshot_uses_canonical_processing_status(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "marketdata.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE provider_health(
            provider TEXT PRIMARY KEY,
            healthy INTEGER,
            state TEXT
        );
        CREATE TABLE canonical_events(
            event_id TEXT PRIMARY KEY,
            processing_status TEXT NOT NULL
        );
        INSERT INTO provider_health(provider,healthy,state)
        VALUES('solana_pumpfun_native',1,'CONNECTED');
        INSERT INTO canonical_events(event_id,processing_status)
        VALUES('a','PENDING'),('b','PROCESSING'),('c','PROCESSED');
        """
    )
    store = SimpleNamespace(conn=connection)

    providers, count, max_rowid, pending = _marketdata_db_snapshot(store)

    assert count == 3
    assert max_rowid == 3
    assert pending == 2
    assert providers[0]["provider"] == "solana_pumpfun_native"
    connection.close()
