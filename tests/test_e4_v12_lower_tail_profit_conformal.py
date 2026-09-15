from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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
    "e4_v12_lower_tail_profit_conformal_tests",
    SCRIPTS / "e4_v12_lower_tail_profit_conformal.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-lower-tail-profit-conformal-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_retired_experiment_identity_is_complete_and_deterministic() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(
        ROOT
    )
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert identity["model_family"] == (
        "histogram_gradient_boosted_quantile_regressor"
    )
    assert identity["full_parameters"]["model"]["quantile"] == 0.25


def test_score_degeneracy_and_negative_expectancy_are_preserved() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 1_145
    assert metrics["wins"] == 14
    assert metrics["win_rate"] < 0.013
    assert metrics["wilson_95_lower_bound"] < 0.008
    assert metrics["net_pnl_sol"] < -3.93
    assert metrics["profit_factor"] < 0.047
    assert metrics["maximum_fold_drawdown_fraction"] > 0.44
    assert metrics["positive_folds"] == 0
    assert max(
        item["largest_test_score_tie"] for item in result["score_degeneracy"]
    ) > 1_000
    assert not all(result["requirements"].values())


def test_scientific_failure_is_retired_and_cannot_consume_live_evidence() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "NEGATIVE_EXPECTANCY"
    assert "parameter-only reruns are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
