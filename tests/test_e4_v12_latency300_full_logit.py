from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_latency300_full_logit_tests",
    ROOT / "scripts/e4_v12_latency300_full_logit.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def artifact() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-full-logit.json").read_text(
            encoding="utf-8"
        )
    )


def test_logit_identity_is_one_fixed_full_feature_model() -> None:
    assert research.MODEL_PARAMETERS == {
        "C": 0.1,
        "class_weight": "balanced",
        "max_iter": 1_000,
        "solver": "liblinear",
    }
    assert len(research.actors.FEATURE_NAMES) == 58


def test_logit_candidate_is_chronological_and_production_is_untouched() -> None:
    report = artifact()
    for fold in report["identity"]["chronological_split"]:
        fit = [int(value) for value in fold["fit"]]
        calibration = [int(value) for value in fold["calibration"]]
        test = [int(value) for value in fold["test"]]
        assert max(fit) < min(calibration) < min(test)
    anti = report["anti_lookahead"]
    assert anti["scaler_fitted_on_fit_only"] is True
    assert anti["threshold_uses_prior_scores_only"] is True
    assert anti["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
    assert report["production_deployment_authorised"] is False


def test_logit_verdict_matches_every_frozen_requirement() -> None:
    report = artifact()
    passed = all(report["requirements"].values())
    assert report["development_gate_passed"] is passed
    assert report["retired"] is (not passed)
    assert report["ready_for_new_strictly_later_holdout"] is passed
    assert report["untouched_holdout_passed"] is False
    assert report["live_confirmation_authorised"] is False
    if not passed:
        assert report["failure_classification"] == "OUTPUT_DETERIORATION"
        audit = report["output_guard_audit"]
        assert audit["saturated_probability_threshold_folds"]
        assert audit["strict_greater_than_substitution_tested"] is False
        assert audit["post_result_guard_retuning_permitted"] is False
