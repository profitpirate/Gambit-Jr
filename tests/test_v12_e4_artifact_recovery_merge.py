from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_artifact_recovery_merge as subject


def part(mint: str, signature: str) -> dict:
    return {
        "schema_version": "e4-lifetime-artifact-recovery-v1",
        "artifact_archives_scanned": 1,
        "json_documents_scanned": 1,
        "manifest": [{"archive": f"{signature}.zip"}],
        "e4_positions": [{
            "mint": mint,
            "creator": "UNKNOWN_CREATOR",
            "creator_resolution_confidence": 0.0,
            "entry_signature": signature,
            "exit_signature": "",
            "entry_time": 1,
            "exit_time": 2,
            "pnl_sol": 0.1,
            "cost_sol": None,
            "proceeds_sol": None,
            "hold_ms": None,
            "outcome": "WIN",
            "sources": [f"artifact://{signature}"],
            "evidence_classes": ["E4_POSITIONS"],
        }],
        "outcomes": [],
    }


def test_merge_dedupes_repeated_trade_identity() -> None:
    result = subject.build([part("mint-a", "sig-a"), part("mint-a", "sig-a")])
    assert result["artifact_archives_scanned"] == 2
    assert result["closed_trade_records"] == 1


def test_merge_keeps_distinct_trade_signatures_for_same_mint() -> None:
    left = part("mint-a", "sig-a")
    right = part("mint-a", "sig-b")
    right["e4_positions"][0]["entry_time"] = 100
    result = subject.build([left, right])
    assert result["closed_trade_records"] == 2
