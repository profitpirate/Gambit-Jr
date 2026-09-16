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
    "e4_v12_latency300_distributed_discovery_candidate_tests",
    SCRIPTS / "e4_v12_latency300_distributed_discovery_candidate.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-distributed-discovery-candidate.json"
        ).read_text(encoding="utf-8")
    )


def test_rolling_selection_never_uses_current_batch_in_threshold() -> None:
    original_history = research.conformal.HISTORY_ROWS
    original_refresh = research.conformal.REFRESH_ROWS
    research.conformal.HISTORY_ROWS = 4
    research.conformal.REFRESH_ROWS = 2
    try:
        selected, thresholds = research.rolling_selection(
            np.asarray([0.0, 1.0, 2.0, 3.0]),
            np.asarray([100.0, 101.0, 102.0, 103.0]),
            0.25,
        )
    finally:
        research.conformal.HISTORY_ROWS = original_history
        research.conformal.REFRESH_ROWS = original_refresh
    assert thresholds[0] == 2.25
    assert thresholds[1] == 100.25
    assert selected.tolist() == [True, True, True, True]


def test_identity_binds_two_stage_policy_and_exact_matrices() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["latency_assumptions_ms"] == [300]
    assert identity["full_parameters"]["range_gate_fraction"] == 0.05
    assert identity["full_parameters"]["conditional_score_fraction"] == 0.054
    assert identity["full_parameters"]["implied_total_score_fraction"] == 0.0027
    assert identity["price_path_matrix_sha256"] == (
        result["price_path_cache_audit"]["matrix_sha256"]
    )
    assert identity["microstructure_matrix_sha256"] == (
        result["microstructure_cache_audit"]["matrix_sha256"]
    )
    assert result["label_audit"]["latency_ms"] == 300


def test_candidate_is_chronological_and_keeps_live_and_production_sealed() -> None:
    result = report()
    assert len(result["folds"]) == 4
    for fold in result["folds"]:
        fit = {int(value) for value in fold["fit_windows"]}
        calibration = {int(value) for value in fold["calibration_windows"]}
        test = {int(value) for value in fold["test_windows"]}
        assert max(fit) < min(calibration) < min(test)
        assert fold["fit_gate_rows"] > fold["calibration_gate_rows"] > 0
        assert fold["test_gate_rows"] > 0
    anti = result["anti_lookahead"]
    assert anti["active_untouched_live_data_used"] is False
    assert all(
        value
        for key, value in anti.items()
        if key != "active_untouched_live_data_used"
    )
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0


def test_failure_is_retired_or_success_is_frozen_without_retuning() -> None:
    result = report()
    passed = result["development_gate_passed"]
    assert result["ready_for_new_strictly_later_holdout"] is passed
    assert result["retired"] is (not passed)
    if passed:
        assert result["failure_classification"] is None
        assert "no development retuning" in result[
            "material_change_required_before_rerun"
        ]
    else:
        assert result["failure_classification"] == "VALIDATION_COLLAPSE"
        assert "variants are prohibited" in result[
            "material_change_required_before_rerun"
        ]
