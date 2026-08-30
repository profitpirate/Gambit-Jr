from __future__ import annotations

from typing import Any

from memecoin_bot.database import Store
from memecoin_bot.models import DiscoveryEvent, iso


class _CursorAfterFetch:
    def __init__(self, cursor: Any, callback: Any):
        self._cursor = cursor
        self._callback = callback
        self._called = False

    def fetchone(self):
        row = self._cursor.fetchone()
        if not self._called:
            self._called = True
            self._callback()
        return row

    def __iter__(self):
        return iter(self._cursor)


class _InterleavingConnection:
    """Insert a committed token after the first reconciliation SELECT."""

    def __init__(self, inner: Any, callback: Any):
        self._inner = inner
        self._callback = callback
        self._interleaved = False

    @property
    def in_transaction(self) -> bool:
        return bool(self._inner.in_transaction)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()):
        cursor = self._inner.execute(sql, parameters)
        if sql == "SELECT COUNT(*) FROM tokens" and not self._interleaved:
            self._interleaved = True
            return _CursorAfterFetch(cursor, self._callback)
        return cursor

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()


def test_state_reconciliation_uses_one_snapshot_during_live_writes(tmp_path) -> None:
    path = tmp_path / "live-status.db"
    writer = Store(path)
    reader = Store(path)
    writer.migrate()
    reader.migrate()
    writer.upsert_discovery(
        DiscoveryEvent(
            token_address="SnapshotInitial111111111111111111111111111",
            source="test",
            discovered_at=iso(),
        )
    )

    inner = reader.conn

    def insert_between_selects() -> None:
        writer.upsert_discovery(
            DiscoveryEvent(
                token_address="SnapshotConcurrent222222222222222222222222",
                source="test",
                discovered_at=iso(),
            )
        )

    reader.conn = _InterleavingConnection(inner, insert_between_selects)
    try:
        report = reader.state_reconciliation()
    finally:
        reader.conn = inner
        reader.close()
        writer.close()

    assert report["total_tracked"] == 1
    assert report["accounted"] == 1
    assert report["difference"] == 0
    assert report["anomalies"] == 0
    assert report["reconciled"] is True


def test_state_reconciliation_remains_truthful_without_anomalies(tmp_path) -> None:
    database = Store(tmp_path / "truth.db")
    database.migrate()
    try:
        for index in range(25):
            database.upsert_discovery(
                DiscoveryEvent(
                    token_address=f"SnapshotTruth{index:02d}11111111111111111111111111",
                    source="test",
                    discovered_at=iso(),
                )
            )
        report = database.state_reconciliation()
    finally:
        database.close()

    assert report["total_tracked"] == 25
    assert report["accounted"] == 25
    assert report["difference"] == 0
    assert report["anomalies"] == 0
    assert report["reconciled"] is True
