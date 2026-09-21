from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_source_reconciliation as subject


def artifact(identifier: int, name: str, *, expired: bool = False) -> dict:
    return {
        "id": identifier,
        "name": name,
        "expired": expired,
        "size_in_bytes": 100,
        "workflow_run_id": identifier + 1000,
    }


def test_only_primary_execution_artifacts_are_required() -> None:
    inv = {
        "artifacts": [
            artifact(1, "e4-live-market-stress-1"),
            artifact(2, "e4-v12-forward-2"),
            artifact(3, "e4-v12-social-golden-historical-3"),
        ]
    }
    recovery = {"manifest": [{"archive": "1.zip"}, {"archive": "2.zip"}]}
    result = subject.build(inv, recovery)
    assert result["primary_artifact_count"] == 2
    assert result["content_reconciliation_complete"] is True


def test_missing_or_expired_primary_artifact_fails_closed() -> None:
    inv = {
        "artifacts": [
            artifact(1, "e4-v11-forward-batch-1"),
            artifact(2, "e4-v12-full-wallet-history-2", expired=True),
            artifact(3, "e4-v12-forward-3"),
        ]
    }
    recovery = {"manifest": [{"archive": "1.zip"}]}
    result = subject.build(inv, recovery)
    assert result["content_reconciliation_complete"] is False
    assert result["missing_retained_primary_artifacts"] == 1
    assert result["expired_primary_artifacts"] == 1
