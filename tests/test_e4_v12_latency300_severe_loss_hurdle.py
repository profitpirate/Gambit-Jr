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
    "e4_v12_latency300_severe_loss_hurdle_tests",
    SCRIPTS / "e4_v12_latency300_severe_loss_hurdle.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-latency300-severe-loss-hurdle-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_identity_binds_joint_model_and_exact_latency_labels() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["model_family"] == "dual_head_profit_severe_loss_hurdle"
    assert identity["full_parameters"]["severe_loss_sol"] == -0.010
    assert identity["latency_assumptions_ms"] == [300]
    assert identity["full_parameters"]["label_matrix_sha256"] == (
        "1b21575dd37159a825116a940cb67e685e3a3cb784de85482476df5a091de76e"
    )


def test_hurdle_has_negative_expectancy_and_fails_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 317
    assert metrics["wins"] == 77
    assert 0.242 < metrics["win_rate"] < 0.243
    assert 0.198 < metrics["wilson_95_lower_bound"] < 0.200
    assert metrics["net_pnl_sol"] < -0.466
    assert metrics["profit_factor"] < 0.604
    assert metrics["positive_folds"] == 1
    assert result["requirements"]["positive_net_pnl"] is False
    assert result["requirements"]["minimum_profit_factor"] is False
    assert result["requirements"]["minimum_win_rate"] is False


def test_scientific_failure_is_retired_without_live_or_production_use() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "NEGATIVE_EXPECTANCY"
    assert "retuning are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
