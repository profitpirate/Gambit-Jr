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
    "e4_v12_latency300_monotonic_transport_tests",
    SCRIPTS / "e4_v12_latency300_monotonic_transport.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-monotonic-transport.json"
        ).read_text(encoding="utf-8")
    )


def test_constraints_follow_fit_derived_directions_exactly() -> None:
    audit = {
        "features": [
            {"direction": "higher_is_positive"},
            {"direction": "higher_is_positive"},
            {"direction": "lower_is_positive"},
        ]
    }
    constraints = research.monotonic_constraints(np.asarray([0, 2]), audit)
    assert constraints.tolist() == [1, -1]


def test_identity_binds_monotonic_fit_only_transport_policy() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    parameters = identity["full_parameters"]
    assert parameters["fit_subepochs"] == 4
    assert parameters["fit_only_oriented_auc_floor"] == 0.52
    assert parameters["monotonic_constraint_policy"] == (
        "fit-first-subepoch orientation"
    )
    assert identity["latency_assumptions_ms"] == [300]
    assert [len(fold["monotonic_constraints"]) for fold in result["folds"]] == [
        40,
        39,
        40,
        36,
    ]
    assert all(
        set(fold["monotonic_constraints"]) <= {-1, 1}
        for fold in result["folds"]
    )


def test_monotonic_transport_is_profitable_but_fails_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 344
    assert metrics["wins"] == 187
    assert 0.543 < metrics["win_rate"] < 0.544
    assert 0.490 < metrics["wilson_95_lower_bound"] < 0.491
    assert metrics["net_pnl_sol"] > 1.192
    assert metrics["profit_factor"] > 1.45
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_win_rate"] < 0.514
    assert metrics["minimum_fold_profit_factor"] < 1.093
    assert result["requirements"]["minimum_win_rate"] is False
    assert result["requirements"]["minimum_wilson_bound"] is False
    assert result["requirements"]["every_fold_minimum_profit_factor"] is False


def test_failed_identity_is_retired_without_live_or_production_use() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert "variants are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["feature_direction_uses_fit_only"] is True
    assert result["anti_lookahead"]["feature_selection_uses_fit_only"] is True
    assert result["anti_lookahead"]["monotonic_constraints_use_fit_only"] is True
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
