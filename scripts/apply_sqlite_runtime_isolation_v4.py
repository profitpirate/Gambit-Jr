from __future__ import annotations

from pathlib import Path

STORE_PATH = Path("src/memecoin_bot/database/store.py")
FABRIC_PATH = Path("src/memecoin_bot/realtime/fabric.py")
RADAR_PATH = Path("src/memecoin_bot/radar_board.py")
TEST_PATH = Path("tests/test_sqlite_runtime_isolation_v4.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


store = STORE_PATH.read_text(encoding="utf-8")

connection_marker = "    def _isolated_connection(self) -> sqlite3.Connection:\n"
if connection_marker not in store:
    store = replace_once(
        store,
        "        self.migrations_dir = Path(migrations_dir) if migrations_dir else self._find_migrations()\n\n"
        "    def _find_migrations(self) -> Path:\n",
        "        self.migrations_dir = Path(migrations_dir) if migrations_dir else self._find_migrations()\n\n"
        "    def _isolated_connection(self) -> sqlite3.Connection:\n"
        "        \"\"\"Open a short-lived WAL connection for atomic leases and report snapshots.\n\n"
        "        The long-lived operational connection is shared by several supervised workers.\n"
        "        Explicit BEGIN statements must never inherit or collide with transaction state\n"
        "        from that connection, so lease claims use their own SQLite connection.\n"
        "        \"\"\"\n"
        "        connection = sqlite3.connect(\n"
        "            self.path,\n"
        "            check_same_thread=False,\n"
        "            timeout=30,\n"
        "            isolation_level=None,\n"
        "        )\n"
        "        connection.row_factory = sqlite3.Row\n"
        "        connection.execute(\"PRAGMA foreign_keys=ON\")\n"
        "        connection.execute(\"PRAGMA busy_timeout=30000\")\n"
        "        connection.execute(\"PRAGMA journal_mode=WAL\")\n"
        "        connection.execute(\"PRAGMA synchronous=NORMAL\")\n"
        "        return connection\n\n"
        "    def _find_migrations(self) -> Path:\n",
        "isolated connection helper",
    )

old_claim = '''    def claim_outbox(self, limit: int = 20, lease_seconds: int = 120) -> list[sqlite3.Row]:
        """Atomically lease rows so concurrent flushers cannot deliver the same event."""
        token = uuid.uuid4().hex
        expired = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [
                    int(row[0])
                    for row in self.conn.execute(
                        "SELECT id FROM outbox WHERE sent_at IS NULL "
                        "AND (claim_token IS NULL OR claimed_at<?) ORDER BY id LIMIT ?",
                        (expired, limit),
                    )
                ]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self.conn.execute(
                        f"UPDATE outbox SET claim_token=?,claimed_at=? WHERE id IN ({placeholders})",
                        (token, iso(), *ids),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return list(
            self.conn.execute(
                "SELECT * FROM outbox WHERE claim_token=? AND sent_at IS NULL ORDER BY id", (token,)
            )
        )
'''
new_claim = '''    def claim_outbox(self, limit: int = 20, lease_seconds: int = 120) -> list[sqlite3.Row]:
        """Atomically lease rows on an isolated connection.

        This prevents a health/report read or another worker's implicit transaction
        on the long-lived connection from causing a nested-BEGIN failure.
        """
        token = uuid.uuid4().hex
        expired = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        connection = self._isolated_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM outbox WHERE sent_at IS NULL "
                    "AND (claim_token IS NULL OR claimed_at<?) ORDER BY id LIMIT ?",
                    (expired, limit),
                )
            ]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE outbox SET claim_token=?,claimed_at=? WHERE id IN ({placeholders})",
                    (token, iso(), *ids),
                )
            rows = list(
                connection.execute(
                    "SELECT * FROM outbox WHERE claim_token=? AND sent_at IS NULL ORDER BY id",
                    (token,),
                )
            )
            connection.commit()
            return rows
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
'''
if old_claim in store:
    store = replace_once(store, old_claim, new_claim, "isolated outbox lease")
elif "Atomically lease rows on an isolated connection" not in store:
    raise RuntimeError("isolated outbox lease: neither old nor new implementation found")

old_status_signature = '''    def status_stats(
        self, started_at: str, candidate_max_age_minutes: float = 180
    ) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
'''
new_status_signature = '''    def status_stats(
        self, started_at: str, candidate_max_age_minutes: float = 180
    ) -> dict[str, Any]:
        """Read one truthful status snapshot without touching the operational connection."""
        reader = Store(self.path, self.migrations_dir)
        try:
            reader.conn.execute("BEGIN")
            try:
                return reader._status_stats_on_connection(
                    started_at,
                    candidate_max_age_minutes,
                )
            finally:
                if reader.conn.in_transaction:
                    reader.conn.rollback()
        finally:
            reader.close()

    def _status_stats_on_connection(
        self, started_at: str, candidate_max_age_minutes: float = 180
    ) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
'''
if old_status_signature in store:
    store = replace_once(
        store,
        old_status_signature,
        new_status_signature,
        "isolated status snapshot",
    )
elif "def _status_stats_on_connection" not in store:
    raise RuntimeError("isolated status snapshot: neither old nor new implementation found")

STORE_PATH.write_text(store, encoding="utf-8")

fabric = FABRIC_PATH.read_text(encoding="utf-8")
old_fabric_claim = '''    def claim_pending(self, limit: int = 100) -> list[CanonicalEvent]:
        if limit <= 0:
            return []
        now = iso()
        with self.store._lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = list(
                    self.store.conn.execute(
                        "SELECT * FROM canonical_events WHERE processing_status='PENDING' "
                        "ORDER BY available_timestamp,event_id LIMIT ?",
                        (limit,),
                    )
                )
                if rows:
                    placeholders = ",".join("?" for _ in rows)
                    self.store.conn.execute(
                        f"UPDATE canonical_events SET processing_status='PROCESSING',claimed_at=?,"
                        f"processing_attempts=processing_attempts+1 WHERE event_id IN ({placeholders})",
                        (now, *(row["event_id"] for row in rows)),
                    )
                self.store.conn.commit()
            except Exception:
                self.store.conn.rollback()
                raise
        return [self._event_from_row(row) for row in rows]
'''
new_fabric_claim = '''    def claim_pending(self, limit: int = 100) -> list[CanonicalEvent]:
        if limit <= 0:
            return []
        now = iso()
        connection = self.store._isolated_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    "SELECT * FROM canonical_events WHERE processing_status='PENDING' "
                    "ORDER BY available_timestamp,event_id LIMIT ?",
                    (limit,),
                )
            )
            if rows:
                placeholders = ",".join("?" for _ in rows)
                connection.execute(
                    f"UPDATE canonical_events SET processing_status='PROCESSING',claimed_at=?,"
                    f"processing_attempts=processing_attempts+1 WHERE event_id IN ({placeholders})",
                    (now, *(row["event_id"] for row in rows)),
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return [self._event_from_row(row) for row in rows]
'''
if old_fabric_claim in fabric:
    fabric = replace_once(
        fabric,
        old_fabric_claim,
        new_fabric_claim,
        "isolated canonical-event lease",
    )
elif "connection = self.store._isolated_connection()" not in fabric:
    raise RuntimeError("isolated canonical-event lease: neither old nor new implementation found")
FABRIC_PATH.write_text(fabric, encoding="utf-8")

radar = RADAR_PATH.read_text(encoding="utf-8")
if "from memecoin_bot.database import Store\n" not in radar:
    radar = replace_once(
        radar,
        "from urllib.parse import parse_qs, urlparse\n\n",
        "from urllib.parse import parse_qs, urlparse\n\nfrom memecoin_bot.database import Store\n\n",
        "radar board Store import",
    )
old_radar_start = '''def start_radar_board(port: int, store: object, started_at: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
'''
new_radar_start = '''def start_radar_board(port: int, store: object, started_at: str) -> ThreadingHTTPServer:
    def read(method_name: str, *args: object) -> object:
        if not isinstance(store, Store):
            return getattr(store, method_name)(*args)
        reader = Store(store.path, store.migrations_dir)
        try:
            return getattr(reader, method_name)(*args)
        finally:
            reader.close()

    class Handler(BaseHTTPRequestHandler):
'''
if old_radar_start in radar:
    radar = replace_once(radar, old_radar_start, new_radar_start, "radar board read isolation")
elif "def read(method_name: str" not in radar:
    raise RuntimeError("radar board read isolation: neither old nor new implementation found")
radar = radar.replace("store.status_stats(started_at)", 'read("status_stats", started_at)')
radar = radar.replace("store.radar_board()", 'read("radar_board")')
radar = radar.replace("store.token_intelligence(address)", 'read("token_intelligence", address)')
RADAR_PATH.write_text(radar, encoding="utf-8")

write_if_changed(
    TEST_PATH,
    '''from __future__ import annotations

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
''',
)

print("SQLite runtime isolation v4 patch applied")
