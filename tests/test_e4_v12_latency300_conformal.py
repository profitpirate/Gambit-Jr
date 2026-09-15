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
    "e4_v12_latency300_conformal_tests",
    SCRIPTS / "e4_v12_latency300_conformal.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-conformal-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_experiment_identity_binds_exact_latency_labels() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert identity["latency_assumptions_ms"] == [300]
    assert identity["full_parameters"]["label_matrix_sha256"] == (
        "1b21575dd37159a825116a940cb67e685e3a3cb784de85482476df5a091de76e"
    )


def test_label_audit_covers_every_consumed_row_without_live_data() -> None:
    audit = report()["label_audit"]
    assert audit["capture_windows"] == 64
    assert audit["rows"] == 191_425
    assert audit["visited_rows"] == 191_425
    assert audit["executable_rows"] == 191_425
    assert audit["no_quote_rows"] == 0
    assert audit["profitable_rows"] == 28_857
    assert audit["negative_or_zero_rows"] == 162_568
    assert audit["all_source_hashes_verified"] is True
    assert audit["active_untouched_live_data_used"] is False
    assert audit["production_paths_changed"] == 0


def test_latency_aware_candidate_improves_but_fails_declared_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 326
    assert metrics["wins"] == 199
    assert 0.610 < metrics["win_rate"] < 0.611
    assert 0.556 < metrics["wilson_95_lower_bound"] < 0.557
    assert metrics["net_pnl_sol"] > 2.337
    assert metrics["profit_factor"] > 2.17
    assert metrics["maximum_fold_drawdown_fraction"] < 0.06
    assert metrics["minimum_fold_win_rate"] > 0.554
    assert metrics["minimum_fold_profit_factor"] > 1.53
    assert metrics["positive_folds"] == 4
    assert metrics["minimum_fold_profitable_windows"] == 6
    assert result["requirements"]["minimum_win_rate"] is False
    assert result["requirements"]["minimum_wilson_bound"] is False
    assert all(
        value
        for key, value in result["requirements"].items()
        if key not in {"minimum_win_rate", "minimum_wilson_bound"}
    )


def test_atomic_label_checkpoint_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "labels.npy"
    expected = np.asarray([1.0, -2.0, 3.5], dtype=np.float64)
    research.labels.atomic_npy(path, expected)
    assert np.array_equal(np.load(path, allow_pickle=False), expected)
    assert list(tmp_path.iterdir()) == [path]


def test_scientific_failure_is_retired_and_cannot_touch_production() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "LATENCY_FAILURE"
    assert "parameter-only reruns are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
