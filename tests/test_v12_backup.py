from __future__ import annotations

import sqlite3
from pathlib import Path

from memecoin_bot.v12_backup import EncryptedBackupManager


def test_encrypted_sqlite_backup_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t(value TEXT)")
    conn.execute("INSERT INTO t VALUES('secret-state')")
    conn.commit()
    conn.close()

    manager = EncryptedBackupManager(tmp_path / "backups", b"k" * 32)
    result = manager.backup(source, label="execution")
    encrypted = Path(result.destination)
    raw = encrypted.read_bytes()
    assert b"secret-state" not in raw

    restored = tmp_path / "restored.db"
    manager.decrypt_to(encrypted, restored)
    check = sqlite3.connect(restored)
    try:
        assert check.execute("SELECT value FROM t").fetchone()[0] == "secret-state"
    finally:
        check.close()
