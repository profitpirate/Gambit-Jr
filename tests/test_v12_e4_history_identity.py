from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_full_history as subject


def test_transaction_signers_only_accept_explicit_signer_metadata() -> None:
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": "creator", "signer": True},
                    {"pubkey": "program", "signer": False},
                    "bare-key-with-no-signer-metadata",
                ]
            }
        }
    }
    assert subject._transaction_signers(tx) == ["creator"]


def test_creator_summary_carries_minimum_identity_confidence() -> None:
    rows = [
        {
            "creator": "creator",
            "mint": "a",
            "outcome": "WIN",
            "pnl_sol": 0.1,
            "source": "ONCHAIN_E4_WALLET",
            "creator_resolution_confidence": 0.95,
        },
        {
            "creator": "creator",
            "mint": "b",
            "outcome": "WIN",
            "pnl_sol": 0.2,
            "source": "ONCHAIN_E4_WALLET",
            "creator_resolution_confidence": 0.85,
        },
    ]
    result = subject.creator_summary(rows)
    assert result[0]["minimum_resolution_confidence"] == 0.85
    assert result[0]["wins"] == 2
