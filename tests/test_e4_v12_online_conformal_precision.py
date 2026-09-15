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


def frozen() -> dict:
    return artifact("e4-v12-online-conformal-frozen-candidate.json")


def protocol() -> dict:
    return artifact("e4-v12-online-conformal-holdout-protocol.json")


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
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(
        ROOT
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
    chronology = report["identity"]["chronological_split"]
    for split in chronology["development_folds"]:
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
    live = chronology["frozen_live_training"]
    assert live["fit"] == epoch[:-5]
    assert live["calibration"] == epoch[-5:]
    assert set(live["fit"]).isdisjoint(live["calibration"])
    assert live["untouched_live"] == "exactly ten strictly future capture windows"
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


def test_frozen_candidate_is_bound_to_exact_development_evidence() -> None:
    candidate = frozen()
    report = development()
    assert candidate["experiment_id"] == report["experiment_id"]
    assert candidate["identity"] == report["identity"]
    assert candidate["source_commit"] == "c38b929711ccb5a715f2f17162d994b96f614368"
    for relative, digest in candidate["source_artifacts"].items():
        assert research.sha256_lf(ROOT / relative) == digest
    assert candidate["candidate"]["development"] == report["aggregate"]
    assert candidate["holdout_status"] == "strictly later evidence required"
    assert candidate["consumed_evidence_end_ns"] < candidate["frozen_at_epoch_ns"]
    assert candidate["untouched_holdout_passed"] is False
    assert candidate["production_paths_changed"] == 0


def test_holdout_protocol_requires_ten_strictly_later_windows_and_one_evaluation() -> None:
    candidate = frozen()
    contract = protocol()
    frozen_spec = contract["frozen_candidate"]
    assert contract["experiment_id"] == candidate["experiment_id"]
    assert frozen_spec["sha256"] == research.sha256_lf(
        ROOT / frozen_spec["path"]
    )
    evidence = contract["final_evidence_contract"]
    assert evidence["required_capture_windows"] == 10
    assert evidence["launches_per_window"] == 3_000
    assert evidence["required_total_launches"] == 30_000
    assert evidence["captures_must_not_overlap"] is True
    assert evidence["optional_stopping_allowed"] is False
    assert evidence["hypothesis_only_required"] is True
    assert evidence["mainnet_transactions_sent_required"] == 0
    assert evidence["mainnet_funds_risked_sol_required"] == 0.0
    gate = contract["golden_gate"]
    assert gate["minimum_closed_trades"] == 50
    assert gate["minimum_win_rate"] == 0.65
    assert gate["minimum_wilson_95_lower_bound"] == 0.55
    assert gate["minimum_profit_factor"] == 1.25
    assert gate["maximum_drawdown_fraction"] == 0.15
    result_policy = contract["result_policy"]
    assert result_policy["final_gate_is_evaluated_once"] is True
    assert result_policy["deterministic_replay_required"] is True
    assert result_policy["sentinel_cannot_declare_found"] is True
    assert result_policy["untouched_live_pass_required_to_declare_found"] is True


def test_holdout_manifest_starts_empty_and_cannot_imply_success() -> None:
    manifest = artifact("e4-v12-online-conformal-holdout-manifest.json")
    contract = protocol()
    assert manifest["experiment_id"] == development()["experiment_id"]
    assert manifest["protocol_sha256_lf"] == research.sha256_lf(
        ROOT / "artifacts/e4-v12-online-conformal-holdout-protocol.json"
    )
    assert manifest["captures"] == []
    assert manifest["production_paths_changed"] == 0
    assert contract["result_policy"]["production_promotion_authorised"] is False
    assert contract["result_policy"]["production_deployment_authorised"] is False
