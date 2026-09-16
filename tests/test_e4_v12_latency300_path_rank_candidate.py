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
    "e4_v12_latency300_path_rank_candidate_tests",
    SCRIPTS / "e4_v12_latency300_path_rank_candidate.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-path-rank-candidate.json").read_text(
            encoding="utf-8"
        )
    )


def test_empirical_rank_scores_use_fit_reference_and_all_features() -> None:
    reference = np.asarray([[0.0, 10.0], [1.0, 20.0], [2.0, 30.0]])
    candidates = np.asarray([[1.5, 15.0], [3.0, 5.0]])
    scores = research.empirical_rank_scores(
        reference, candidates, np.asarray([1, -1])
    )
    assert np.allclose(scores, [2 / 3, 1.0])


def test_identity_binds_rank_family_and_exact_matrices() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["model_family"] == (
        "fit_only_equal_weight_empirical_copula_rank_ensemble"
    )
    assert identity["full_parameters"]["feature_weighting"] == (
        "equal_weight_all_sixteen_features"
    )
    assert identity["latency_assumptions_ms"] == [300]
    assert identity["price_path_matrix_sha256"] == (
        result["price_path_cache_audit"]["matrix_sha256"]
    )
    assert identity["microstructure_matrix_sha256"] == (
        result["microstructure_cache_audit"]["matrix_sha256"]
    )


def test_candidate_is_chronological_and_sealed() -> None:
    result = report()
    assert len(result["folds"]) == 4
    for fold in result["folds"]:
        fit = {int(value) for value in fold["fit_windows"]}
        calibration = {int(value) for value in fold["calibration_windows"]}
        test = {int(value) for value in fold["test_windows"]}
        assert max(fit) < min(calibration) < min(test)
        assert len(fold["fit_only_feature_directions"]) == 16
        assert len(fold["fit_only_raw_auc"]) == 16
    anti = result["anti_lookahead"]
    assert anti["active_untouched_live_data_used"] is False
    assert all(
        value
        for key, value in anti.items()
        if key != "active_untouched_live_data_used"
    )
    assert result["untouched_holdout_passed"] is False
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
