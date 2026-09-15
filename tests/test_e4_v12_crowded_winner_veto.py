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
    "e4_v12_crowded_winner_veto_tests",
    SCRIPTS / "e4_v12_crowded_winner_veto.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-crowded-winner-veto-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_two_gate_selection_is_deterministic_and_vetoes_crowding() -> None:
    calibration_scores = np.linspace(0.0, 1.0, 10_000)
    calibration_crowding = np.ones(10_000)
    test_scores = np.zeros(100)
    test_scores[:2] = 1.0
    test_crowding = np.ones(100)
    test_crowding[0] = 2.0

    first = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_crowding,
        test_scores,
        test_crowding,
    )
    second = research.rolling_two_gate_selection(
        calibration_scores.copy(),
        calibration_crowding.copy(),
        test_scores.copy(),
        test_crowding.copy(),
    )
    assert np.array_equal(first[0], second[0])
    assert first[1:] == second[1:]
    assert first[0][0] == np.False_
    assert first[0][1] == np.True_
    assert first[3] == 1


def test_future_values_cannot_change_an_earlier_selection_block() -> None:
    calibration_scores = np.linspace(0.0, 1.0, 10_000)
    calibration_crowding = np.linspace(1.0, 2.0, 10_000)
    baseline_scores = np.linspace(0.0, 1.0, 200)
    baseline_crowding = np.linspace(1.0, 2.0, 200)
    changed_scores = baseline_scores.copy()
    changed_crowding = baseline_crowding.copy()
    changed_scores[100:] = 1_000.0
    changed_crowding[100:] = -1_000.0

    baseline = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_crowding,
        baseline_scores,
        baseline_crowding,
    )
    changed = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_crowding,
        changed_scores,
        changed_crowding,
    )
    assert np.array_equal(baseline[0][:100], changed[0][:100])
    assert baseline[1][0] == changed[1][0]
    assert baseline[2][0] == changed[2][0]


def test_retired_experiment_identity_is_complete_and_novel() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert identity["full_parameters"]["secondary_feature"] == (
        "buyer_average_pnl_max"
    )
    assert identity["full_parameters"]["secondary_upper_quantile"] == 0.95
    assert result["experiment_id"] != result["baseline_experiment_id"]


def test_improved_aggregate_is_rejected_when_breadth_gates_fail() -> None:
    result = report()
    metrics = result["aggregate"]
    baseline = result["baseline"]
    assert metrics["trades"] == 296
    assert metrics["wins"] == 199
    assert metrics["win_rate"] > baseline["win_rate"]
    assert metrics["wilson_95_lower_bound"] > baseline["wilson_95_lower_bound"]
    assert metrics["net_pnl_sol"] > baseline["net_pnl_sol"]
    assert metrics["profit_factor"] > baseline["profit_factor"]
    assert metrics["maximum_fold_drawdown_fraction"] < (
        baseline["maximum_fold_drawdown_fraction"]
    )
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_win_rate"] >= 0.64
    assert metrics["minimum_fold_profit_factor"] >= 1.32
    assert metrics["minimum_fold_profitable_windows"] == 5
    assert metrics["crowded_winner_vetoes"] == 21
    assert result["requirements"]["minimum_closed_trades"] is False
    assert result["requirements"]["every_fold_six_profitable_windows"] is False
    assert all(result["incremental_requirements"].values())


def test_scientific_failure_is_retired_and_cannot_consume_live_evidence() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert "parameter-only reruns are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
