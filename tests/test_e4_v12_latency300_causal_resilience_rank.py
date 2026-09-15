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
    "e4_v12_latency300_causal_resilience_rank_tests",
    SCRIPTS / "e4_v12_latency300_causal_resilience_rank.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT
            / "artifacts/e4-v12-latency300-causal-resilience-rank-failure.json"
        ).read_text(encoding="utf-8")
    )


def test_identity_binds_rank_features_and_exact_latency_labels() -> None:
    result = report()
    identity = result["identity"]
    assert result["experiment_id"] == (
        f"e4x-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["full_parameters"]["resilience_features"] == [
        "buyer_history_count_mean",
        "log_return_volatility",
    ]
    assert identity["latency_assumptions_ms"] == [300]


def test_prior_only_percentile_rank_is_monotone() -> None:
    prior = [0.0, 1.0, 2.0, 3.0]
    values = np.asarray([-1.0, 1.5, 4.0])
    ranked = research.percentiles(prior, values)
    assert np.all(np.diff(ranked) > 0)
    assert np.all((ranked > 0) & (ranked < 1))


def test_resilience_rank_has_negative_expectancy_and_fails_gate() -> None:
    result = report()
    metrics = result["aggregate"]
    assert metrics["trades"] == 504
    assert metrics["wins"] == 172
    assert 0.341 < metrics["win_rate"] < 0.342
    assert 0.301 < metrics["wilson_95_lower_bound"] < 0.302
    assert metrics["net_pnl_sol"] < -1.072
    assert metrics["profit_factor"] < 0.767
    assert metrics["maximum_fold_drawdown_fraction"] > 0.26
    assert metrics["positive_folds"] == 2
    assert result["requirements"]["positive_net_pnl"] is False
    assert result["requirements"]["minimum_profit_factor"] is False


def test_scientific_failure_is_retired_without_live_or_production_use() -> None:
    result = report()
    assert result["development_gate_passed"] is False
    assert result["retired"] is True
    assert result["failure_classification"] == "NEGATIVE_EXPECTANCY"
    assert "retuning are prohibited" in (
        result["material_change_required_before_rerun"]
    )
    assert result["anti_lookahead"]["feature_ranks_use_prior_values_only"] is True
    assert result["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
