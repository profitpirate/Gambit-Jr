from __future__ import annotations

import hashlib
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
    "e4_v12_causal_regime_veto_position_tests",
    SCRIPTS / "e4_v12_causal_regime_veto_position.py",
)


def report():
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-causal-regime-veto-position-development.json"
        ).read_text(encoding="utf-8")
    )


def frozen():
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-causal-regime-veto-position-frozen-candidate.json"
        ).read_text(encoding="utf-8")
    )


def test_vetoes_are_fixed_interpretable_and_causal() -> None:
    assert research.POSITION_FRACTION == 0.04
    assert research.REGIME_VETO.feature == "prior_price_multiple_median"
    assert research.REGIME_VETO.keep == "at_or_below"
    assert research.ACTOR_VETO.feature == "buyer_average_pnl_max"
    assert research.ACTOR_VETO.keep == "at_or_above"
    result = report()
    for fold in result["folds"]:
        assert set(fold["train_windows"]).isdisjoint(fold["validation_windows"])
        assert len(fold["validation_windows"]) == 5
        assert set(fold["thresholds"]) == {veto.key for veto in research.VETOES}


def test_exact_development_candidate_clears_every_declared_gate() -> None:
    result = report()
    winner = result["winner"]
    assert winner["trades"] >= 200
    assert winner["win_rate"] >= 0.65
    assert winner["wilson_95_lower_bound"] >= 0.60
    assert winner["net_pnl_sol"] > 0
    assert winner["profit_factor"] >= 1.25
    assert winner["positive_folds"] == 4
    assert winner["minimum_fold_profit_factor"] >= 1.10
    assert winner["minimum_fold_win_rate"] >= 0.55
    assert winner["positive_capture_windows"] >= 16
    assert winner["maximum_fold_drawdown_fraction"] <= 0.15
    assert winner["largest_winner_contribution"] <= 0.15
    assert result["walk_forward_requirements_passed"] == 11
    assert result["walk_forward_gate_passed"] is True


def test_joint_veto_improves_the_exact_size_matched_ablation() -> None:
    result = report()
    verdict = result["ablation_verdict"]
    assert verdict["selected_improves_win_rate"] is True
    assert verdict["selected_improves_profit_factor"] is True
    assert verdict["selected_improves_time_coverage"] is True
    assert result["ablation_gate_passed"] is True


def test_frozen_candidate_is_content_addressed_and_not_promoted() -> None:
    result = report()
    freeze = frozen()
    development_path = (
        ROOT / "artifacts/e4-v12-causal-regime-veto-position-development.json"
    )
    actual_sha = hashlib.sha256(development_path.read_bytes()).hexdigest()
    assert freeze["development_sha256"] == actual_sha
    assert freeze["experiment_id"] == result["experiment_id"]
    assert result["ready_for_strictly_later_evidence"] is True
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_paths_changed"] == 0


def test_all_consumed_sources_are_verified_and_point_in_time() -> None:
    result = report()
    assert result["preformal_screen"]["holdout_status"] == (
        "development_only_not_untouched"
    )
    assert result["preformal_screen"]["pairs_tested"] == 59_508
    assert all(row["hash_match"] for row in result["source_audit"])
    assert sum(row["parse_errors"] for row in result["source_audit"]) == 0
    audit = result["actor_memory_audit"]
    assert audit["future_values_in_features"] is False
    assert audit["actor_updates_delayed_ms"] == 60_000
