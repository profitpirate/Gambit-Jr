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
    "e4_v12_online_conformal_precision_tests",
    SCRIPTS / "e4_v12_online_conformal_precision.py",
)


def artifact(name: str) -> dict:
    return json.loads((ROOT / "artifacts" / name).read_text(encoding="utf-8"))


def development() -> dict:
    return artifact("e4-v12-online-conformal-development.json")


def latency() -> dict:
    return artifact("e4-v12-online-conformal-latency.json")


def ablation() -> dict:
    return artifact("e4-v12-online-conformal-ablation.json")


def test_experiment_identity_is_deterministic_complete_and_novel() -> None:
    report = development()
    identity = report["identity"]
    expected_keys = {
        "thesis_family_identifier",
        "source_code_fingerprint",
        "dataset_source_manifest_fingerprint",
        "evidence_epoch",
        "feature_set_fingerprint",
        "model_family",
        "full_parameters",
        "causal_horizon_ms",
        "candidate_risk_set_policy",
        "chronological_split",
        "bankroll_sol",
        "position_sizing",
        "fee_model",
        "output_guard",
        "latency_assumptions_ms",
        "execution_policy",
        "exit_policy",
    }
    assert set(identity) == expected_keys
    assert report["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.sha256_lf(
        SCRIPTS / "e4_v12_online_conformal_precision.py"
    )
    assert report["experiment_id"] not in {
        "e4x-207b67e8dc16497ba27d4f6699c7a760bb3728290cc91e85a68ff026ac03744c",
        "e4x-4bb77c4529386b4fbad5afbe908276cfb97daf67e105f13c4041a3778a74d160",
    }


def test_evidence_epoch_and_chronology_are_strictly_point_in_time() -> None:
    report = development()
    assert report["data_audit"]["captures"] == 64
    assert report["data_audit"]["launches"] == 192_000
    assert report["data_audit"]["rows"] == 191_425
    assert report["data_audit"]["parse_errors"] == 0
    assert report["data_audit"]["future_values_in_features"] is False
    assert report["data_audit"]["actor_updates_delayed_ms"] == 60_000
    epoch = report["identity"]["evidence_epoch"]
    assert len(epoch) == 64
    assert epoch == sorted(epoch, key=int)
    for split in report["identity"]["chronological_split"]:
        fit = split["fit"]
        calibration = split["calibration"]
        test = split["test"]
        assert set(fit).isdisjoint(calibration)
        assert set(fit).isdisjoint(test)
        assert set(calibration).isdisjoint(test)
        assert max(map(int, fit)) < min(map(int, calibration))
        assert max(map(int, calibration)) < min(map(int, test))
        assert len(calibration) == 5
        assert len(test) == 10
    assert all(report["anti_lookahead"].values())


def test_rolling_threshold_never_uses_current_or_future_scores() -> None:
    calibration = np.linspace(0.0, 1.0, research.HISTORY_ROWS)
    baseline = np.linspace(0.0, 1.0, research.REFRESH_ROWS * 2)
    changed = baseline.copy()
    changed[research.REFRESH_ROWS :] = 1_000.0
    baseline_mask, baseline_thresholds = research.rolling_selection(calibration, baseline)
    changed_mask, changed_thresholds = research.rolling_selection(calibration, changed)
    assert np.array_equal(
        baseline_mask[: research.REFRESH_ROWS],
        changed_mask[: research.REFRESH_ROWS],
    )
    assert baseline_thresholds[0] == changed_thresholds[0]
    assert len(baseline_thresholds) == 2
    assert len(changed_thresholds) == 2


def test_development_candidate_clears_every_declared_gate() -> None:
    report = development()
    winner = report["aggregate"]
    assert winner["trades"] == 317
    assert winner["wins"] == 209
    assert winner["win_rate"] >= 0.65
    assert winner["wilson_95_lower_bound"] >= 0.60
    assert winner["net_pnl_sol"] > 2.30
    assert winner["profit_factor"] >= 2.24
    assert winner["positive_folds"] == 4
    assert winner["minimum_fold_win_rate"] >= 0.625
    assert winner["minimum_fold_profit_factor"] >= 1.25
    assert winner["maximum_fold_drawdown_fraction"] < 0.06
    assert winner["largest_winner_contribution"] < 0.03
    assert winner["profitable_capture_windows"] == 27
    assert all(report["requirements"].values())
    assert report["development_gate_passed"] is True
    assert report["ready_for_strictly_later_evidence"] is True


def test_exact_trace_latency_gate_clears_all_scenarios() -> None:
    report = latency()
    assert report["experiment_id"] == development()["experiment_id"]
    assert report["trace_audit"]["selected_traces"] == 317
    assert report["exact_trace_gate_passed"] is True
    assert all(report["requirements"].values())
    for latency_ms in (0, 1, 2, 5, 10):
        metrics = report["latencies"][str(latency_ms)]
        assert metrics["trades"] == 317
        assert metrics["net_pnl_sol"] > 2.20
        assert metrics["profit_factor"] > 2.18
        assert metrics["positive_folds"] == 4
        assert metrics["quote_coverage"] == 1.0
        assert metrics["rejected_quote"] == 0
        assert metrics["ledger_hash"]


def test_ablation_gate_attributes_edge_to_intended_components() -> None:
    report = ablation()
    assert report["experiment_id"] == development()["experiment_id"]
    assert report["ablation_gate_passed"] is True
    assert all(report["requirements"].values())
    results = report["results"]
    full = results["full_58"]["aggregate"]
    assert full["win_rate"] > results["without_actor_memory_42"]["aggregate"]["win_rate"]
    assert full["win_rate"] > results["without_market_context_46"]["aggregate"]["win_rate"]
    assert full["win_rate"] > results["fixed_calibration_threshold"]["aggregate"]["win_rate"]
    assert full["win_rate"] > results["fixed_baseline_exit"]["aggregate"]["win_rate"]
    shuffled = results["shuffled_labels"]["aggregate"]
    assert shuffled["win_rate"] < 0.15
    assert shuffled["net_pnl_sol"] < 0


def test_research_artifacts_cannot_promote_or_touch_production() -> None:
    reports = (development(), latency(), ablation())
    assert len({report["experiment_id"] for report in reports}) == 1
    for report in reports:
        assert report["production_paths_changed"] == 0
        assert report["untouched_holdout_passed"] is False
        assert report["production_promotion_authorised"] is False
        assert report["production_deployment_authorised"] is False
