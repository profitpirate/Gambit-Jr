"""Encrypted, atomic SQLite backups for V12 production."""
from __future__ import annotations

import base64
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"V12DB1"
NONCE_BYTES = 12
TAG_BYTES = 16


def decode_aes_key(value: str) -> bytes:
    raw = value.strip()
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("backup key must be urlsafe-base64") from exc
    if len(key) != 32:
        raise ValueError("backup key must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True, slots=True)
class BackupResult:
    source: str
    destination: str
    plaintext_bytes: int
    encrypted_bytes: int
    created_ns: int


class EncryptedBackupManager:
    def __init__(
        self,
        backup_dir: Path,
        key: bytes,
        *,
        retain: int = 24,
    ):
        if len(key) != 32:
            raise ValueError("AES-256 backup key must be 32 bytes")
        self.backup_dir = backup_dir
        self.key = key
        self.retain = max(2, int(retain))
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)

    def _sqlite_snapshot(self, source: Path, destination: Path) -> None:
        src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(destination, timeout=10)
        try:
            src.backup(dst, pages=256, sleep=0.01)
            dst.execute("PRAGMA wal_checkpoint(FULL)")
            dst.commit()
        finally:
            dst.close()
            src.close()

    def _encrypt(self, source: Path, destination: Path) -> tuple[int, int]:
        nonce = os.urandom(NONCE_BYTES)
        encryptor = Cipher(algorithms.AES(self.key), modes.GCM(nonce)).encryptor()
        plaintext_bytes = 0
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        with source.open("rb") as reader, tmp.open("wb") as writer:
            writer.write(MAGIC)
            writer.write(nonce)
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                plaintext_bytes += len(chunk)
                writer.write(encryptor.update(chunk))
            writer.write(encryptor.finalize())
            writer.write(encryptor.tag)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(destination)
        return plaintext_bytes, destination.stat().st_size

    def backup(self, source: Path, *, label: str) -> BackupResult:
        if not source.exists():
            raise FileNotFoundError(source)
        now = time.time_ns()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = self.backup_dir / f"{label}-{stamp}-{now}.sqlite.aesgcm"
        with tempfile.TemporaryDirectory(dir=self.backup_dir) as folder:
            snapshot = Path(folder) / "snapshot.sqlite"
            self._sqlite_snapshot(source, snapshot)
            plaintext, encrypted = self._encrypt(snapshot, destination)
        self.prune(label)
        return BackupResult(str(source), str(destination), plaintext, encrypted, now)

    def decrypt_to(self, encrypted: Path, destination: Path) -> None:
        size = encrypted.stat().st_size
        if size < len(MAGIC) + NONCE_BYTES + TAG_BYTES:
            raise ValueError("encrypted backup is truncated")
        with encrypted.open("rb") as reader:
            if reader.read(len(MAGIC)) != MAGIC:
                raise ValueError("invalid backup magic")
            nonce = reader.read(NONCE_BYTES)
            reader.seek(-TAG_BYTES, os.SEEK_END)
            tag = reader.read(TAG_BYTES)
            ciphertext_start = len(MAGIC) + NONCE_BYTES
            ciphertext_end = size - TAG_BYTES
            reader.seek(ciphertext_start)
            decryptor = Cipher(algorithms.AES(self.key), modes.GCM(nonce, tag)).decryptor()
            tmp = destination.with_suffix(destination.suffix + ".tmp")
            with tmp.open("wb") as writer:
                remaining = ciphertext_end - ciphertext_start
                while remaining:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup ended unexpectedly")
                    remaining -= len(chunk)
                    writer.write(decryptor.update(chunk))
                writer.write(decryptor.finalize())
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(tmp, 0o600)
            tmp.replace(destination)
        conn = sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise RuntimeError(f"restored database integrity failure: {row}")
        finally:
            conn.close()

    def prune(self, label: str) -> None:
        files = sorted(
            self.backup_dir.glob(f"{label}-*.sqlite.aesgcm"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in files[self.retain :]:
            stale.unlink(missing_ok=True)
