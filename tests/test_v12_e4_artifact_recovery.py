from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_artifact_recovery as subject


def test_recovery_reads_only_explicit_e4_position_keys(tmp_path: Path) -> None:
    archive = tmp_path / "batch.zip"
    payload = {
        "gambit_positions": {
            "fake": {"mint": "fake", "entry_time": 1, "pnl_sol": 999}
        },
        "same_window_e4_positions": {
            "real": {"mint": "real", "entry_time": 10, "exit_time": 12, "pnl_sol": 0.2}
        },
    }
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("report.json", json.dumps(payload))
    result = subject.build(tmp_path)
    assert result["closed_trade_records"] == 1
    assert result["e4_positions"][0]["mint"] == "real"


def test_recovery_preserves_e4_attempts_without_promoting_them_to_trades(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "attempts.zip"
    payload = {
        "outcomes": [{
            "event_identity": "wallet:sig",
            "selection_type": "FAILED_ATTEMPT",
            "wallet_signature": "sig",
            "outcome": "UNRESOLVED_MINT",
            "timestamp": 100,
        }]
    }
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("selection.json", json.dumps(payload))
    result = subject.build(tmp_path)
    assert result["closed_trade_records"] == 0
    assert result["attempt_records"] == 1
