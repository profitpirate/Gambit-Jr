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
    "e4_v12_latency300_rolling_regime_tests",
    SCRIPTS / "e4_v12_latency300_rolling_regime.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-rolling-regime-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_identity_binds_rolling_chronology_and_exact_latency_labels() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["full_parameters"]["rolling_fit_windows"] == 16
    assert identity["latency_assumptions_ms"] == [300]
    assert all(len(row["fit"]) == 16 for row in identity["chronological_split"])


def test_rolling_regime_is_profitable_but_fails_validation_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 334
    assert metrics["wins"] == 193
    assert 0.577 < metrics["win_rate"] < 0.579
    assert 0.524 < metrics["wilson_95_lower_bound"] < 0.525
    assert metrics["net_pnl_sol"] > 2.130
    assert metrics["profit_factor"] > 2.00
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_win_rate"] < 0.489
    assert metrics["minimum_fold_profitable_windows"] == 5
    assert result["requirements"]["minimum_win_rate"] is False
    assert result["requirements"]["minimum_wilson_bound"] is False
    assert result["requirements"]["every_fold_minimum_win_rate"] is False


def test_scientific_failure_is_retired_without_live_or_production_use() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert "variations are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["fit_uses_completed_windows_only"] is True
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
