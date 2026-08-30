from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from memecoin_bot.database import Store
from memecoin_bot.models import iso
from memecoin_bot.realtime import CanonicalEvent, CanonicalEventFabric, CanonicalEventType


def _store(path: Path) -> Store:
    store = Store(path)
    store.migrate()
    return store


def test_outbox_claim_uses_isolated_connection_when_primary_has_read_transaction(tmp_path) -> None:
    store = _store(tmp_path / "outbox.db")
    try:
        with store.conn:
            store.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                ("isolation-test", "TEST", "{}", iso()),
            )
        store.conn.execute("BEGIN")
        store.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()

        claimed = store.claim_outbox(limit=10)

        assert len(claimed) == 1
        assert claimed[0]["event_key"] == "isolation-test"
        assert claimed[0]["claim_token"]
        assert store.conn.in_transaction is True
    finally:
        if store.conn.in_transaction:
            store.conn.rollback()
        store.close()


def test_canonical_claim_uses_isolated_connection_when_primary_has_read_transaction(tmp_path) -> None:
    store = _store(tmp_path / "canonical.db")
    fabric = CanonicalEventFabric(store)
    event = CanonicalEvent.create(
        CanonicalEventType.TOKEN_CREATED,
        "IsolationMint111111111111111111111111111111111",
        "solana",
        "pumpfun",
        "test",
        iso(),
        source_event_id="isolation-event",
    )
    try:
        fabric.publish(event)
        store.conn.execute("BEGIN")
        store.conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()

        claimed = fabric.claim_pending(10)

        assert [item.event_id for item in claimed] == [event.event_id]
        assert store.conn.in_transaction is True
    finally:
        if store.conn.in_transaction:
            store.conn.rollback()
        store.close()


def test_status_snapshot_does_not_reuse_or_end_primary_transaction(tmp_path) -> None:
    store = _store(tmp_path / "status.db")
    try:
        store.conn.execute("BEGIN")
        store.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()

        report = store.status_stats(iso())

        assert report["database"]["healthy"] is True
        assert report["state_reconciliation"]["reconciled"] is True
        assert store.conn.in_transaction is True
    finally:
        if store.conn.in_transaction:
            store.conn.rollback()
        store.close()


def test_status_and_leases_survive_repeated_concurrent_pressure(tmp_path) -> None:
    path = tmp_path / "pressure.db"
    seed = _store(path)
    try:
        for index in range(75):
            with seed.conn:
                seed.conn.execute(
                    "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (f"pressure:{index}", "TEST", json.dumps({"index": index}), iso()),
                )
    finally:
        seed.close()

    def read_status() -> bool:
        store = _store(path)
        try:
            report = store.status_stats(iso())
            return bool(report["database"]["healthy"])
        finally:
            store.close()

    def claim_once() -> int:
        store = _store(path)
        try:
            return len(store.claim_outbox(limit=1))
        finally:
            store.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        futures = [executor.submit(read_status) for _ in range(120)]
        futures.extend(executor.submit(claim_once) for _ in range(75))
        results = [future.result(timeout=30) for future in futures]

    assert all(results[:120])
    assert sum(int(value) for value in results[120:]) == 75

    final = _store(path)
    try:
        duplicate_claims = final.conn.execute(
            "SELECT claim_token,COUNT(*) FROM outbox WHERE claim_token IS NOT NULL "
            "GROUP BY claim_token HAVING COUNT(*)>1"
        ).fetchall()
        unclaimed = final.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE claim_token IS NULL"
        ).fetchone()[0]
    finally:
        final.close()

    assert duplicate_claims == []
    assert unclaimed == 0
