from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
    "e4_v12_latency300_causal_outcome_regime_tests",
    SCRIPTS / "e4_v12_latency300_causal_outcome_regime.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-causal-outcome-regime.json"
        ).read_text(encoding="utf-8")
    )


def test_regime_memory_excludes_unresolved_and_current_outcomes() -> None:
    second = research.RESOLUTION_DELAY_MS * 1_000_000 - 1
    resolved = research.RESOLUTION_DELAY_MS * 1_000_000
    rows = [
        SimpleNamespace(decision_ns=0, run_id="1", mint="a"),
        SimpleNamespace(decision_ns=second, run_id="1", mint="b"),
        SimpleNamespace(decision_ns=resolved, run_id="1", mint="c"),
    ]
    matrix = research.causal_regime_matrix(
        rows, np.asarray([0.02, -0.02, 0.50], dtype=np.float64)
    )
    assert matrix[0, 0] == 0
    assert matrix[1, 0] == 0
    assert matrix[2, 0] == 1
    assert matrix[2, 1] == np.float32(2 / 3)
    assert matrix[2, 2] == np.float32(0.02)
    assert matrix[2, 3] == 0
    assert matrix[2, 4] == 1
    assert matrix[2, 8] == 0
    assert matrix[2, 9] == 0


def test_identity_binds_new_features_and_unchanged_execution_contract() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    parameters = identity["full_parameters"]
    assert parameters["outcome_resolution_delay_ms"] == 60_300
    assert parameters["short_regime_window_ms"] == 300_000
    assert parameters["long_regime_window_ms"] == 1_800_000
    assert parameters["score_fraction"] == 0.0027
    assert identity["latency_assumptions_ms"] == [300]
    assert len(result["regime_feature_audit"]["feature_names"]) == 10


def test_causal_outcome_regime_is_profitable_but_fails_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 353
    assert metrics["wins"] == 206
    assert 0.583 < metrics["win_rate"] < 0.584
    assert 0.531 < metrics["wilson_95_lower_bound"] < 0.532
    assert metrics["net_pnl_sol"] > 2.235
    assert metrics["profit_factor"] > 2.00
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_win_rate"] < 0.511
    assert metrics["minimum_fold_profitable_windows"] == 5
    assert result["requirements"]["minimum_win_rate"] is False
    assert result["requirements"]["minimum_wilson_bound"] is False
    assert result["requirements"]["every_fold_minimum_win_rate"] is False


def test_failed_identity_is_retired_without_live_or_production_use() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert "variations are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["regime_feature_audit"]["current_or_future_outcomes_used"] is False
    assert result["anti_lookahead"][
        "regime_outcomes_resolve_before_current_decision"
    ] is True
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
