from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from memecoin_bot.v12_live_readiness import (
    EXPECTED_FROZEN_MODEL_SHA256,
    _causal_gate,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_bundle(tmp_path: Path) -> tuple[Path, dict, dict, dict]:
    model = json.loads(
        (ROOT / "research/v12-pre-armed-100-trade-frozen-model.json").read_text(
            encoding="utf-8"
        )
    )
    certification = json.loads(
        (ROOT / "research/v12-pre-armed-certification-status.json").read_text(
            encoding="utf-8"
        )
    )

    ledger = []
    for index in range(100):
        pnl = 0.01 if index < 60 else -0.005
        decision_ns = 1_000_000 + index * 10_000
        ledger.append(
            {
                "decision_ns": decision_ns,
                "fill_ns": decision_ns + 1_000,
                "exit_ns": decision_ns + 2_000,
                "entry_cost_sol": 0.1,
                "proceeds_sol": 0.1 + pnl,
                "pnl_sol": pnl,
            }
        )
    net_pnl = sum(float(row["pnl_sol"]) for row in ledger)
    state = {
        "completion": {
            "reached": True,
            "remaining": 0,
            "required_closed_trades": 100,
        },
        "ledger": ledger,
        "metrics": {
            "acceptance_gate_passed": True,
            "closed_trades": 100,
            "wins": 60,
            "losses": 40,
            "net_pnl_sol": net_pnl,
        },
        "model_sha256": EXPECTED_FROZEN_MODEL_SHA256,
        "production_paths_changed": 0,
        "real_money_execution": False,
    }

    research = tmp_path / "research"
    state_path = research / "v12-pre-armed-100-causal-paper-live.json"
    _write_json(state_path, state)
    _write_json(research / "v12-pre-armed-certification-status.json", certification)
    _write_json(research / "v12-pre-armed-100-trade-frozen-model.json", model)
    return state_path, state, certification, model


def test_causal_gate_accepts_only_complete_recognized_bundle(tmp_path: Path) -> None:
    state_path, _, _, _ = _valid_bundle(tmp_path)

    passed, detail = _causal_gate(state_path, repository_root=tmp_path)

    assert passed is True, detail
    assert "invalidated_sample_excluded=true" in detail


def test_causal_gate_rejects_imported_invalidated_sample(tmp_path: Path) -> None:
    state_path, _, certification, _ = _valid_bundle(tmp_path)
    certification["pre_fix_100_trade_sample_imported"] = True
    _write_json(
        tmp_path / "research/v12-pre-armed-certification-status.json",
        certification,
    )

    passed, detail = _causal_gate(state_path, repository_root=tmp_path)

    assert passed is False
    assert "old pre-fix sample was imported" in detail


def test_causal_gate_rejects_frozen_model_tampering(tmp_path: Path) -> None:
    state_path, _, _, model = _valid_bundle(tmp_path)
    corrupted = deepcopy(model)
    corrupted["selector"]["entry_latency_ms"] = 999
    _write_json(
        tmp_path / "research/v12-pre-armed-100-trade-frozen-model.json",
        corrupted,
    )

    passed, detail = _causal_gate(state_path, repository_root=tmp_path)

    assert passed is False
    assert "frozen model file fingerprint mismatch" in detail


def test_causal_gate_rejects_impossible_prefill_exit(tmp_path: Path) -> None:
    state_path, state, _, _ = _valid_bundle(tmp_path)
    state["ledger"][7]["exit_ns"] = state["ledger"][7]["fill_ns"] - 1
    _write_json(state_path, state)

    passed, detail = _causal_gate(state_path, repository_root=tmp_path)

    assert passed is False
    assert "impossible causal timestamps" in detail


def test_causal_gate_rejects_incomplete_trade_count(tmp_path: Path) -> None:
    state_path, state, _, _ = _valid_bundle(tmp_path)
    state["completion"]["reached"] = False
    state["completion"]["remaining"] = 1
    state["metrics"]["closed_trades"] = 99
    state["ledger"] = state["ledger"][:99]
    state["metrics"]["wins"] = 60
    state["metrics"]["losses"] = 39
    state["metrics"]["net_pnl_sol"] = sum(
        float(row["pnl_sol"]) for row in state["ledger"]
    )
    _write_json(state_path, state)

    passed, detail = _causal_gate(state_path, repository_root=tmp_path)

    assert passed is False
    assert "100-trade completion not reached" in detail
