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
    "e4_v12_liquidity_floor_conformal_tests",
    SCRIPTS / "e4_v12_liquidity_floor_conformal.py",
)


def development() -> dict:
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-liquidity-floor-conformal-development.json"
        ).read_text(encoding="utf-8")
    )


def test_two_gate_selection_is_deterministic_and_vetoes_low_liquidity() -> None:
    calibration_scores = np.linspace(0.0, 1.0, 10_000)
    calibration_liquidity = np.ones(10_000)
    calibration_liquidity[-27:] = 10.0
    test_scores = np.zeros(100)
    test_scores[:2] = 1.0
    test_liquidity = np.full(100, 20.0)
    test_liquidity[0] = 1.0

    first = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_liquidity,
        test_scores,
        test_liquidity,
    )
    second = research.rolling_two_gate_selection(
        calibration_scores.copy(),
        calibration_liquidity.copy(),
        test_scores.copy(),
        test_liquidity.copy(),
    )
    assert np.array_equal(first[0], second[0])
    assert first[1:] == second[1:]
    assert first[0][0] == np.False_
    assert first[0][1] == np.True_
    assert first[3] == 1


def test_future_values_cannot_change_an_earlier_selection_block() -> None:
    calibration_scores = np.linspace(0.0, 1.0, 10_000)
    calibration_liquidity = np.linspace(1.0, 2.0, 10_000)
    baseline_scores = np.linspace(0.0, 1.0, 200)
    baseline_liquidity = np.linspace(1.0, 2.0, 200)
    changed_scores = baseline_scores.copy()
    changed_liquidity = baseline_liquidity.copy()
    changed_scores[100:] = 1_000.0
    changed_liquidity[100:] = -1_000.0

    baseline = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_liquidity,
        baseline_scores,
        baseline_liquidity,
    )
    changed = research.rolling_two_gate_selection(
        calibration_scores,
        calibration_liquidity,
        changed_scores,
        changed_liquidity,
    )
    assert np.array_equal(baseline[0][:100], changed[0][:100])
    assert baseline[1][0] == changed[1][0]
    assert baseline[2][0] == changed[2][0]


def test_experiment_identity_is_complete_deterministic_and_novel() -> None:
    report = development()
    identity = report["identity"]
    assert report["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(
        ROOT
    )
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert identity["full_parameters"]["secondary_feature"] == "median_buy_sol"
    assert identity["full_parameters"]["secondary_lower_quantile"] == 0.05
    assert report["experiment_id"] != (
        "e4x-b1086b70febc39fefa73d35f8451674a126a3c0256ca95a608b57f9c9c6cd1cc"
    )


def test_candidate_clears_absolute_gate_but_fails_incremental_gate() -> None:
    report = development()
    winner = report["aggregate"]
    assert winner["trades"] == 303
    assert winner["wins"] == 200
    assert winner["win_rate"] >= 0.66
    assert winner["wilson_95_lower_bound"] >= 0.60
    assert winner["net_pnl_sol"] > 2.09
    assert winner["profit_factor"] > 2.20
    assert winner["positive_folds"] == 4
    assert winner["minimum_fold_win_rate"] >= 0.62
    assert winner["minimum_fold_profit_factor"] >= 1.17
    assert winner["minimum_fold_profitable_windows"] >= 6
    assert winner["liquidity_vetoes"] == 14
    assert all(report["requirements"].values())
    assert report["absolute_gate_passed"] is True
    assert not all(report["incremental_requirements"].values())
    assert report["development_gate_passed"] is False
    assert report["ready_for_strictly_later_evidence"] is False
    assert report["failure_classification"] == "OUTPUT_DETERIORATION"


def test_research_candidate_cannot_authorise_production_or_claim_live() -> None:
    report = development()
    assert report["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["live_confirmation_authorised"] is False
    assert report["production_promotion_authorised"] is False
    assert report["production_deployment_authorised"] is False
    assert report["production_paths_changed"] == 0
