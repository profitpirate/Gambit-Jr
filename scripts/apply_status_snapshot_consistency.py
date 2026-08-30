from __future__ import annotations

from pathlib import Path


STORE_PATH = Path("src/memecoin_bot/database/store.py")
TEST_PATH = Path("tests/test_status_snapshot_consistency.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


store_text = STORE_PATH.read_text(encoding="utf-8")
old = '''    def state_reconciliation(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])
        states = {
            str(r[0]): int(r[1])
            for r in self.conn.execute(
                "SELECT COALESCE(c.state,'DISCOVERED'),COUNT(*) FROM tokens t LEFT JOIN candidates c "
                "ON c.token_id=t.id GROUP BY COALESCE(c.state,'DISCOVERED')"
            )
        }
        accounted = sum(states.values())
        orphan_candidates = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM candidates c LEFT JOIN tokens t ON t.id=c.token_id "
                "WHERE t.id IS NULL"
            ).fetchone()[0]
        )
        orphan_signals = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM signals s LEFT JOIN tokens t ON t.id=s.token_id "
                "WHERE t.id IS NULL"
            ).fetchone()[0]
        )
        ghost_qualified = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM candidates c LEFT JOIN signals s ON s.id=c.signal_id "
                "WHERE c.authoritative_state='QUALIFIED_SIGNAL' AND s.id IS NULL"
            ).fetchone()[0]
        )
        duplicate_candidates = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT token_id FROM candidates GROUP BY token_id "
                "HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        integrity_difference = (
            total
            - accounted
            + orphan_candidates
            + orphan_signals
            + ghost_qualified
            + duplicate_candidates
        )
        return {
            "total_tracked": total,
            "states": states,
            "accounted": accounted,
            "orphan_candidates": orphan_candidates,
            "orphan_signals": orphan_signals,
            "ghost_qualified": ghost_qualified,
            "duplicate_candidates": duplicate_candidates,
            "difference": integrity_difference,
            "reconciled": integrity_difference == 0,
        }
'''
new = '''    def state_reconciliation(self) -> dict[str, Any]:
        """Read every reconciliation counter from one SQLite snapshot.

        Discord and the health endpoint can query while the scanner is writing.
        Without an explicit read transaction, separate SELECT statements may see
        different committed versions and briefly report a false integrity failure.
        WAL allows the writer to continue while this reader keeps one stable view.
        """
        with self._lock:
            owns_snapshot = not self.conn.in_transaction
            if owns_snapshot:
                self.conn.execute("BEGIN")
            try:
                total = int(self.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])
                states = {
                    str(row[0]): int(row[1])
                    for row in self.conn.execute(
                        "SELECT COALESCE(c.state,'DISCOVERED'),COUNT(*) "
                        "FROM tokens t LEFT JOIN candidates c ON c.token_id=t.id "
                        "GROUP BY COALESCE(c.state,'DISCOVERED')"
                    )
                }
                accounted = sum(states.values())
                orphan_candidates = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM candidates c LEFT JOIN tokens t ON t.id=c.token_id "
                        "WHERE t.id IS NULL"
                    ).fetchone()[0]
                )
                orphan_signals = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM signals s LEFT JOIN tokens t ON t.id=s.token_id "
                        "WHERE t.id IS NULL"
                    ).fetchone()[0]
                )
                ghost_qualified = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM candidates c LEFT JOIN signals s ON s.id=c.signal_id "
                        "WHERE c.authoritative_state='QUALIFIED_SIGNAL' AND s.id IS NULL"
                    ).fetchone()[0]
                )
                duplicate_candidates = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM (SELECT token_id FROM candidates GROUP BY token_id "
                        "HAVING COUNT(*)>1)"
                    ).fetchone()[0]
                )
                integrity_difference = total - accounted
                anomaly_count = (
                    orphan_candidates
                    + orphan_signals
                    + ghost_qualified
                    + duplicate_candidates
                )
                result = {
                    "total_tracked": total,
                    "states": states,
                    "accounted": accounted,
                    "orphan_candidates": orphan_candidates,
                    "orphan_signals": orphan_signals,
                    "ghost_qualified": ghost_qualified,
                    "duplicate_candidates": duplicate_candidates,
                    "difference": integrity_difference,
                    "anomalies": anomaly_count,
                    "reconciled": integrity_difference == 0 and anomaly_count == 0,
                }
            except Exception:
                if owns_snapshot:
                    self.conn.rollback()
                raise
            else:
                if owns_snapshot:
                    self.conn.commit()
                return result
'''
store_text = replace_once(store_text, old, new, "state reconciliation snapshot")
STORE_PATH.write_text(store_text, encoding="utf-8")

TEST_PATH.write_text(
    '''from __future__ import annotations

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
''',
    encoding="utf-8",
)

print("status reconciliation snapshot consistency patch applied")
