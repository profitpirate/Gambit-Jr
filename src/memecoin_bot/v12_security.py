"""Security primitives for the V12 production runtime."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

LOGGER = logging.getLogger("gambit.v12.security")
_SECRET_RE = re.compile(
    r"(?i)(private[_-]?key|secret|token|password|authorization|api[_-]?key)"
    r"([\"'=: ]+)([^,\s}\"']+)"
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_RE.sub(r"\1\2[REDACTED]", record.msg)
        if record.args:
            record.args = tuple(
                _SECRET_RE.sub(r"\1\2[REDACTED]", str(item))
                for item in record.args
            )
        return True


def install_redaction_filter() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_private_file(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
    if not path.exists():
        raise FileNotFoundError(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"{path} must not be group/world accessible; mode={oct(mode)}")


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another V12 live instance already owns the lock") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    verified: bool
    manifest_hash: str
    files_checked: int
    signature_verified: bool


class IntegrityVerifier:
    """Verify a critical-file manifest and optional Ed25519 signature.

    Live mode requires a signature by default. The public verification key can
    be embedded in deployment config; the private signing key never belongs on
    the trading host.
    """

    def __init__(
        self,
        root: Path,
        manifest_path: Path,
        *,
        public_key: str = "",
        signature: str = "",
        require_signature: bool = True,
    ):
        self.root = root.resolve()
        self.manifest_path = manifest_path
        self.public_key = public_key.strip()
        self.signature = signature.strip()
        self.require_signature = require_signature

    def verify(self) -> IntegrityResult:
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("integrity manifest has no files")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        manifest_hash = hashlib.sha256(canonical).hexdigest()

        signature_verified = False
        if self.public_key and self.signature:
            try:
                from solders.pubkey import Pubkey
                from solders.signature import Signature
            except ImportError as exc:
                raise RuntimeError("solders is required for signed integrity verification") from exc
            signature_verified = bool(
                Signature.from_string(self.signature).verify(
                    Pubkey.from_string(self.public_key),
                    canonical,
                )
            )
            if not signature_verified:
                raise RuntimeError("critical-code integrity signature verification failed")
        elif self.require_signature:
            raise RuntimeError("live mode requires a signed critical-code manifest")

        checked = 0
        for relative, expected in files.items():
            candidate = (self.root / str(relative)).resolve()
            if self.root not in candidate.parents and candidate != self.root:
                raise ValueError(f"manifest path escapes repository: {relative}")
            if not candidate.is_file():
                raise FileNotFoundError(candidate)
            actual = sha256_file(candidate)
            if not hmac.compare_digest(actual, str(expected)):
                raise RuntimeError(f"critical-code hash mismatch: {relative}")
            checked += 1
        return IntegrityResult(True, manifest_hash, checked, signature_verified)


class TamperEvidentAuditLog:
    """Append-only JSONL log whose records form an HMAC hash chain."""

    def __init__(self, path: Path, key: bytes):
        if len(key) < 32:
            raise ValueError("audit HMAC key must be at least 32 bytes")
        self.path = path
        self.key = key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        os.chmod(path, 0o600)
        self.previous = self._read_last_mac()

    def _read_last_mac(self) -> str:
        try:
            last = ""
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        last = line
            if not last:
                return "0" * 64
            row = json.loads(last)
            return str(row["mac"])
        except (OSError, json.JSONDecodeError, KeyError):
            return "0" * 64

    def append(self, event: str, payload: dict[str, Any]) -> str:
        record = {
            "ts_ns": time.time_ns(),
            "event": str(event),
            "payload": payload,
            "previous_mac": self.previous,
        }
        message = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
        mac = hmac.new(self.key, message, hashlib.sha256).hexdigest()
        record["mac"] = mac
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.previous = mac
        return mac

    def verify(self) -> bool:
        previous = "0" * 64
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                mac = str(row.pop("mac"))
                if row.get("previous_mac") != previous:
                    return False
                message = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()
                expected = hmac.new(self.key, message, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    return False
                previous = mac
        return True


def decode_secret_key(value: str) -> bytes:
    raw = value.strip()
    if not raw:
        raise ValueError("secret key is empty")
    try:
        import base64

        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) >= 32:
            return decoded
    except ValueError:
        pass
    data = raw.encode()
    if len(data) < 32:
        raise ValueError("secret key must decode to at least 32 bytes")
    return data


def validate_live_arm_token(provided: str, expected_sha256: str) -> None:
    if not provided or not expected_sha256:
        raise PermissionError("live arming token/hash is not configured")
    actual = hashlib.sha256(provided.encode()).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
        raise PermissionError("live arming token is invalid")


def generate_arm_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def build_manifest(root: Path, paths: Iterable[str]) -> dict[str, Any]:
    files: dict[str, str] = {}
    for relative in sorted(set(paths)):
        candidate = root / relative
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        files[relative] = sha256_file(candidate)
    return {
        "version": "v12-integrity-manifest-v1",
        "generated_ns": time.time_ns(),
        "files": files,
    }
