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
    "e4_v12_realistic_latency_diagnostic_tests",
    SCRIPTS / "e4_v12_realistic_latency_diagnostic.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-realistic-latency-diagnostic.json"
        ).read_text(encoding="utf-8")
    )


def test_diagnostic_identity_binds_empirical_runtime_evidence() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        diagnostic.source_code_fingerprint(ROOT)
    )
    evidence = result["observed_runtime_evidence"]
    assert evidence["workflow_run_id"] == "34938012082"
    assert evidence["artifact_zip_sha256"] == (
        "be3fbeccb0fa22b835b1e982bb9b91d88381470f55f908c65ca5b1635a05422e"
    )
    assert evidence["evidence_batches"] == 49
    assert evidence["launches"] == 147_000
    assert evidence["direct_copy_closed_positions"] == 454
    assert evidence["median_source_to_fill_ms"] == 297.207515


def test_realistic_latency_preserves_economics_but_not_win_rate() -> None:
    result = report()
    observed = result["latencies"]["300"]
    assert observed["trades"] == 317
    assert observed["wins"] == 177
    assert observed["win_rate"] > 0.558
    assert observed["win_rate"] < 0.559
    assert observed["wilson_95_lower_bound"] > 0.503
    assert observed["net_pnl_sol"] > 1.896
    assert observed["profit_factor"] > 2.06
    assert observed["maximum_fold_drawdown_fraction"] < 0.079
    assert observed["minimum_fold_win_rate"] == 0.425
    assert observed["minimum_fold_profit_factor"] > 1.10
    assert observed["positive_folds"] == 4
    assert observed["quote_coverage"] == 1.0
    assert result["economic_robustness_passed"] is True
    assert result["declared_win_rate_gate_passed_at_observed_latency"] is False
    assert result["supplemental_failure_classification"] == "LATENCY_FAILURE"


def test_latency_diagnostic_cannot_change_frozen_or_production_state() -> None:
    result = report()
    assert result["frozen_protocol_changed"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
