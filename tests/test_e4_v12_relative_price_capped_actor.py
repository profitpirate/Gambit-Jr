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
    "e4_v12_relative_price_capped_actor_tests",
    SCRIPTS / "e4_v12_relative_price_capped_actor.py",
)


def load_artifact(name: str) -> dict:
    return json.loads((ROOT / "artifacts" / name).read_text(encoding="utf-8"))


def report() -> dict:
    return load_artifact("e4-v12-relative-price-capped-actor-development.json")


def frozen() -> dict:
    return load_artifact("e4-v12-relative-price-capped-actor-frozen-candidate.json")


def test_v11_rule_is_interpretable_causal_and_capped() -> None:
    assert research.ACTOR_RULE.known_buyers_min == 1
    assert research.ACTOR_RULE.history_max_min == 5
    assert research.ACTOR_RULE.bayesian_win_rate_max_min == 0.60
    assert research.ACTOR_RULE.average_pnl_max_min == 0.010
    assert research.ACTOR_RULE.positive_expectancy_fraction_min == 0.50
    assert research.REGIME_FEATURE == "relative_price_multiple"
    assert research.REGIME_QUANTILE == 0.05
    assert research.POSITION_FRACTION == 0.04
    assert research.POSITION_CAP_SOL == 0.12
    result = report()
    for fold in result["folds"]:
        assert set(fold["train_windows"]).isdisjoint(fold["validation_windows"])
        assert len(fold["validation_windows"]) == 10
        assert fold["relative_price_threshold"] > 0


def test_v11_development_candidate_clears_every_declared_gate() -> None:
    result = report()
    winner = result["winner"]
    assert winner["trades"] == 340
    assert winner["win_rate"] >= 0.65
    assert winner["wilson_95_lower_bound"] >= 0.60
    assert winner["net_pnl_sol"] > 0
    assert winner["profit_factor"] >= 1.25
    assert winner["positive_folds"] == 3
    assert winner["positive_capture_windows"] >= 22
    assert winner["maximum_fold_drawdown_fraction"] <= 0.15
    assert winner["largest_winner_contribution"] <= 0.15
    assert all(result["development_requirements"].values())
    assert result["development_gate_passed"] is True


def test_v11_all_fold_latencies_are_profitable_and_fully_quoted() -> None:
    result = report()
    for fold in result["folds"]:
        for latency in ("0", "1", "2", "5", "10"):
            metrics = fold["veto_latencies"][latency]
            assert metrics["net_pnl_sol"] > 0
            assert metrics["profit_factor"] >= 1.25
            assert metrics["quote_coverage"] == 1.0
            assert metrics["ledger_hash"]


def test_v11_veto_beats_same_size_actor_only_ablation() -> None:
    result = report()
    assert result["ablation"]["win_rate_delta"] > 0
    assert result["ablation"]["profit_factor_delta"] > 0
    assert result["ablation"]["pnl_delta_sol"] > 0
    assert result["ablation"]["positive_capture_windows_delta"] > 0
    assert all(result["ablation_requirements"].values())
    assert result["ablation_gate_passed"] is True


def test_v11_source_manifest_and_point_in_time_audit_are_complete() -> None:
    source = load_artifact("e4-v12-relative-price-capped-actor-source-manifest.json")
    result = report()
    assert source["totals"]["captures"] == 54
    assert source["totals"]["launches"] == 162_000
    assert len(source["supplemental_captures"]) == 6
    assert all(row["role"] == "consumed_development" for row in source["supplemental_captures"])
    assert result["data_audit"]["captures"] == 54
    assert result["data_audit"]["launches"] == 162_000
    assert result["data_audit"]["parse_errors"] == 0
    assert result["data_audit"]["future_values_in_features"] is False
    assert result["data_audit"]["actor_updates_delayed_ms"] == 60_000


def test_v11_freeze_is_content_addressed_and_cannot_promote() -> None:
    result = report()
    candidate = frozen()
    development = ROOT / "artifacts/e4-v12-relative-price-capped-actor-development.json"
    assert candidate["development_sha256"] == research.sha256_lf(development)
    assert candidate["experiment_id"] == result["experiment_id"]
    assert candidate["identity"] == result["identity"]
    assert result["ready_for_strictly_later_evidence"] is True
    for payload in (result, candidate):
        assert payload["untouched_holdout_passed"] is False
        assert payload["live_confirmation_authorised"] is False
        assert payload["production_promotion_authorised"] is False
        assert payload["production_deployment_authorised"] is False
        assert payload["production_paths_changed"] == 0
