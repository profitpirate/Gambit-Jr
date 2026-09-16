from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_latency300_selected_residual_transport_tests",
    ROOT / "scripts/e4_v12_latency300_selected_residual_transport.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def artifact() -> dict:
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-latency300-selected-residual-transport.json"
        ).read_text(encoding="utf-8")
    )


def test_ledger_indexes_require_exact_replay_identity() -> None:
    class Row:
        def __init__(self, run_id: str, mint: str, decision_ns: int) -> None:
            self.run_id = run_id
            self.mint = mint
            self.decision_ns = decision_ns

    rows = [Row("1", "a", 10), Row("1", "b", 20)]
    ledger = [{"run_id": "1", "mint": "b", "decision_ns": 20}]
    assert research.ledger_indexes(rows, ledger).tolist() == [1]


def test_residual_diagnostic_replays_exact_executed_baseline() -> None:
    report = artifact()
    audit = report["selection_audit"]
    assert audit["exact_baseline_replayed"] is True
    assert audit["concurrency_rejections_preserved"] is True
    assert audit["executed_trades"] == 326
    assert audit["wins"] == 199
    assert audit["losses"] == 127


def test_residual_diagnostic_cannot_promote_or_touch_production() -> None:
    report = artifact()
    assert report["candidate_fitted"] is False
    assert report["development_gate_passed"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
    assert report["production_deployment_authorised"] is False
