from __future__ import annotations

import json
from pathlib import Path

import pytest

from memecoin_bot.v12_security import (
    IntegrityVerifier,
    SingleInstanceLock,
    TamperEvidentAuditLog,
    build_manifest,
    generate_arm_token,
    validate_live_arm_token,
)


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "live.lock")
    second = SingleInstanceLock(tmp_path / "live.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another V12 live instance"):
            second.acquire()
    finally:
        first.release()


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = TamperEvidentAuditLog(path, b"x" * 32)
    audit.append("ONE", {"a": 1})
    audit.append("TWO", {"b": 2})
    assert audit.verify() is True

    rows = path.read_text().splitlines()
    payload = json.loads(rows[0])
    payload["payload"]["a"] = 999
    rows[0] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n")
    assert audit.verify() is False


def test_live_arm_token_hash() -> None:
    token, digest = generate_arm_token()
    validate_live_arm_token(token, digest)
    with pytest.raises(PermissionError):
        validate_live_arm_token("wrong", digest)


def test_integrity_manifest_detects_file_change(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "critical.py"
    target.write_text("safe = True\n")
    manifest = build_manifest(root, ["critical.py"])
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    result = IntegrityVerifier(
        root,
        manifest_path,
        require_signature=False,
    ).verify()
    assert result.verified is True

    target.write_text("safe = False\n")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        IntegrityVerifier(
            root,
            manifest_path,
            require_signature=False,
        ).verify()
