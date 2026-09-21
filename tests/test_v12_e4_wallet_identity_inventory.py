from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_wallet_identity_inventory as subject


def test_wallet_inventory_requires_every_primary_head(monkeypatch) -> None:
    inv = {
        "artifacts": [{
            "id": 1,
            "name": "e4-live-market-stress-1",
            "expired": False,
            "workflow_run_head_sha": "abc",
        }]
    }
    monkeypatch.setattr(subject, "grep_commit", lambda _sha: ({subject.CANONICAL}, None))
    result = subject.build(inv)
    assert result["certified_complete"] is True
    assert result["unexpected_wallets"] == []


def test_wallet_inventory_fails_on_unresolved_or_different_wallet(monkeypatch) -> None:
    inv = {
        "artifacts": [
            {
                "id": 1,
                "name": "e4-v12-forward-1",
                "expired": False,
                "workflow_run_head_sha": "abc",
            },
            {
                "id": 2,
                "name": "e4-v11-forward-batch-2",
                "expired": False,
                "workflow_run_head_sha": "def",
            },
        ]
    }
    other = "11111111111111111111111111111111"
    monkeypatch.setattr(
        subject,
        "grep_commit",
        lambda sha: ({other}, None) if sha == "abc" else (set(), None),
    )
    result = subject.build(inv)
    assert result["certified_complete"] is False
    assert result["heads_unresolved"] == 1
    assert result["unexpected_wallets"] == [other]
