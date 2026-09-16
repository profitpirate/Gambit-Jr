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
    "e4_v12_latency300_nested_transport_tests",
    SCRIPTS / "e4_v12_latency300_nested_transport.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-nested-transport.json"
        ).read_text(encoding="utf-8")
    )


def test_nested_selection_uses_only_supplied_fit_windows() -> None:
    features = np.zeros((8, len(research.actors.FEATURE_NAMES)), dtype=np.float32)
    profitable = np.asarray([False, True] * 4)
    row_windows = np.repeat(np.arange(4), 2)
    features[:, 0] = np.asarray([0, 1] * 4)
    features[:, 1] = np.asarray([0, 1, 1, 0, 0, 1, 1, 0])
    selected, audit = research.nested_transport_selection(
        features, profitable, row_windows, fit_end=4
    )
    assert selected.tolist() == [0]
    assert audit["fit_window_chunks"] == [[0], [1], [2], [3]]
    assert audit["selected_feature_count"] == 1
    assert audit["features"][0]["oriented_auc_by_fit_subepoch"] == [1.0] * 4
    assert audit["features"][1]["selected"] is False


def test_identity_binds_nested_fit_only_transport_policy() -> None:
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
    assert parameters["score_fraction"] == 0.0027
    assert identity["latency_assumptions_ms"] == [300]
    assert [
        fold["selection_audit"]["selected_feature_count"]
        for fold in result["folds"]
    ] == [40, 39, 40, 36]


def test_nested_transport_is_profitable_but_fails_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 321
    assert metrics["wins"] == 184
    assert 0.573 < metrics["win_rate"] < 0.574
    assert 0.518 < metrics["wilson_95_lower_bound"] < 0.519
    assert metrics["net_pnl_sol"] > 1.680
    assert metrics["profit_factor"] > 1.78
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_win_rate"] == 0.4875
    assert metrics["minimum_fold_profit_factor"] < 1.053
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
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
