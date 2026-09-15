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


diagnostic = load_module(
    "e4_v12_latency300_temporal_drift_tests",
    SCRIPTS / "e4_v12_latency300_temporal_drift.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-temporal-drift.json").read_text(
            encoding="utf-8"
        )
    )


def test_identity_binds_source_dataset_features_and_latency() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        diagnostic.source_code_fingerprint(ROOT)
    )
    assert identity["latency_ms"] == 300
    assert identity["feature_set_fingerprint"] == diagnostic.base.stable_hash(
        diagnostic.actors.FEATURE_NAMES
    )


def test_exact_latency_opportunity_rate_decays_chronologically() -> None:
    result = report()
    assert result["rows"] == 191_425
    assert result["windows"] == 64
    assert len(result["per_window"]) == 64
    assert result["window_rate_spearman"]["rho"] < -0.64
    assert result["window_rate_spearman"]["p_value"] < 1e-8
    assert result["quarters"][0]["positive_rate"] > 0.166
    assert result["quarters"][-1]["positive_rate"] < 0.124
    assert result["last_10_positive_rate"] < result["first_10_positive_rate"]


def test_final_epoch_has_label_and_feature_shift() -> None:
    result = report()
    final = result["fold_epochs"][-1]
    assert final["fit_positive_rate"] > 0.159
    assert final["calibration_positive_rate"] < 0.109
    assert final["test_positive_rate"] < 0.131
    shifts = result["top_final_epoch_feature_shifts"]
    assert shifts[0]["feature"] == "prior_price_multiple_median"
    assert shifts[0]["ks_statistic"] > 0.37
    names = {row["feature"] for row in shifts}
    assert "creator_seed_sol" in names
    assert "creator_prior_launch_count" in names
    assert "buyer_average_pnl_max" in names


def test_diagnostic_does_not_fit_candidate_or_touch_live_production() -> None:
    result = report()
    assert result["candidate_fitted"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
