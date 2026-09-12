from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


research = load_module(
    "e4_v12_direct_hazard_consensus_tests",
    SCRIPTS / "e4_v12_direct_hazard_consensus.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-direct-hazard-consensus-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_rolling_quantile_does_not_use_later_score() -> None:
    seed = np.linspace(0.0, 1.0, 1_000)
    original = research.causal_quantile_mask(seed, [0.999, 0.1, 0.2], 0.995)
    changed_future = research.causal_quantile_mask(seed, [0.999, 0.9, 0.8], 0.995)
    assert original[0] == changed_future[0]
    assert original[0]


def test_walk_forward_uses_four_strictly_later_folds() -> None:
    result = report()
    assert len(result["folds"]) == 4
    for fold in result["folds"]:
        assert len(fold["validation_windows"]) == 5
        assert set(fold["train_windows"]).isdisjoint(fold["validation_windows"])
        assert int(fold["train_windows"][-1]) < int(fold["validation_windows"][0])


def test_failed_precision_thesis_is_not_promoted() -> None:
    result = report()
    assert result["winner"]["net_pnl_sol"] > 0
    assert result["winner"]["profit_factor"] >= 1.25
    assert result["winner"]["win_rate"] < 0.65
    assert result["walk_forward_gate_passed"] is False
    assert result["ready_for_strictly_later_evidence"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False


def test_consumed_evidence_is_not_claimed_as_future_holdout() -> None:
    result = report()
    assert result["audit"]["captures"] == 48
    assert result["audit"]["all_evidence_previously_consumed"] is True
    assert result["audit"]["future_holdout_claimed"] is False
    assert result["production_paths_changed"] == 0
