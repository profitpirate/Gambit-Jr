from __future__ import annotations

import pytest

from memecoin_bot.v12_shadow import (
    CreatorConcentrationAnalyzer,
    DriftMonitor,
    FailedIntentScorer,
    GuardCounterfactualAnalyzer,
    HardNegativeScorer,
    NewCreatorAnalogueScorer,
    RegimeProfiler,
    RiskOfRuinSimulator,
    SellAbsorptionScorer,
    ShadowEnsemble,
    build_shadow_report,
)


def trade(creator: str, pnl: float, i: int, **extra):
    row = {
        "creator": creator,
        "pnl_sol": pnl,
        "decision_ns": 1_790_000_000_000_000_000 + i * 1_000_000_000,
        "output_ratio": 0.8,
        "create_to_fill_price_multiple": 1.2,
        "creator_seed_sol": 3,
        "tweet_age_seconds": 2,
        "exit_reason": "MAXIMUM_HOLD",
    }
    row.update(extra)
    return row


def test_concentration_exclusions_are_real_recomputations():
    ledger = [trade("A", 0.2, i) for i in range(5)] + [trade("B", -0.05, 10)]
    result = CreatorConcentrationAnalyzer().analyze(ledger)
    assert result["creator_count"] == 2
    assert result["top_creator_trade_share"] == 5 / 6
    assert result["exclusions"]["exclude_top_1_by_trade_count"]["trades"] == 1
    assert result["exclusions"]["exclude_top_1_by_trade_count"]["net_pnl_sol"] == -0.05


def test_regime_profiler_segments_all_rows():
    ledger = [
        trade("A", 0.1, 1, output_ratio=0.95, tweet_age_seconds=0.5),
        trade("B", -0.02, 2, output_ratio=0.67, tweet_age_seconds=8),
    ]
    report = RegimeProfiler().analyze(ledger)
    assert report["output_band"][">=0.90"]["trades"] == 1
    assert report["output_band"]["0.65-0.70"]["trades"] == 1
    assert report["social_age_band"]["0-1s"]["wins"] == 1


def test_guard_counterfactual_marks_saves_and_misses():
    report = GuardCounterfactualAnalyzer().analyze([
        {"mint": "A", "counterfactual_pnl_sol": -0.3},
        {"mint": "B", "counterfactual_pnl_sol": 0.2},
        {"mint": "C"},
    ])
    assert report["correct_rejections"] == 1
    assert report["missed_winners"] == 1
    assert report["unresolved"] == 1
    assert report["net_guard_value_sol"] == pytest.approx(0.1)


def test_risk_simulator_is_deterministic_and_bounded():
    sim = RiskOfRuinSimulator()
    one = sim.simulate([0.1, 0.08, -0.02, -0.03], paths=1000, horizon=50, seed=7)
    two = sim.simulate([0.1, 0.08, -0.02, -0.03], paths=1000, horizon=50, seed=7)
    assert one == two
    assert 0 <= one["risk_of_ruin"] <= 1
    assert one["max_drawdown"]["p95"] >= one["max_drawdown"]["median"]


def test_drift_monitor_escalates_multi_signal_collapse():
    baseline = [0.1] * 15 + [-0.02] * 5
    current = [-0.05] * 20
    result = DriftMonitor().evaluate(baseline, current, window=20)
    assert result["severity"] == "HALT_REVIEW"
    assert len(result["reasons"]) >= 2


def test_new_creator_is_shadow_only_and_fail_closed():
    scorer = NewCreatorAnalogueScorer()
    high = scorer.score({
        "prelaunch_social_quality": 1,
        "funding_quality": 1,
        "creator_seed_quality": 1,
        "buyer_diversity": 1,
        "clean_bundle_score": 1,
        "known_funder_similarity": 1,
        "early_flow_quality": 1,
    })
    assert high.action == "SHADOW_TIER_B"
    assert high.shadow_only
    blocked = scorer.score({"hard_negative": True})
    assert blocked.action == "REJECT"


def test_hard_negative_vetoes_high_risk():
    d = HardNegativeScorer().score({
        "known_rug_funder": 1,
        "insider_concentration": 1,
        "bundle_probability": 1,
        "wash_probability": 1,
        "creator_dump_risk": 1,
        "data_staleness": 1,
        "provider_disagreement": 1,
    })
    assert d.action == "SHADOW_VETO"
    assert d.score == 1


def test_sell_absorption_and_failed_intent_outputs_are_non_executable():
    sell = SellAbsorptionScorer().score({
        "buy_after_sell_ratio": 1,
        "liquidity_resilience": 1,
        "buyer_quality": 1,
        "creator_sell_pressure": 0,
        "flow_acceleration": 1,
    })
    failed = FailedIntentScorer().score({
        "failed_buy_notional": 9,
        "landed_buy_notional": 1,
        "failed_sell_notional": 0,
        "landed_sell_notional": 5,
        "sophisticated_failed_buy_share": 1,
    })
    assert sell.action == "SHADOW_HOLD"
    assert failed.action == "SHADOW_SUPPORT"
    assert sell.shadow_only and failed.shadow_only


def test_ensemble_never_emits_executable_route():
    payload = ShadowEnsemble().evaluate({
        "new_creator": {
            "prelaunch_social_quality": 1,
            "funding_quality": 1,
            "creator_seed_quality": 1,
            "buyer_diversity": 1,
            "clean_bundle_score": 1,
            "known_funder_similarity": 1,
            "early_flow_quality": 1,
        },
        "hard_negative": {},
        "sell_absorption": {
            "buy_after_sell_ratio": 1,
            "liquidity_resilience": 1,
            "buyer_quality": 1,
            "creator_sell_pressure": 0,
            "flow_acceleration": 1,
        },
        "failed_intent": {
            "failed_buy_notional": 1,
            "landed_buy_notional": 0,
            "sophisticated_failed_buy_share": 1,
        },
    })
    assert payload["shadow_only"] is True
    assert payload["recommendation"].startswith("SHADOW_")
    assert len(payload["evidence_hash"]) == 64


def test_report_integrity_and_no_production_mutation():
    state = {
        "model_sha256": "abc",
        "status": "COLLECTING",
        "starting_bankroll_sol": 3,
        "ledger": [trade("A", 0.1, 1), trade("B", -0.01, 2)],
    }
    report = build_shadow_report(state, baseline_state=state, risk_paths=100)
    assert report["shadow_only"] is True
    assert report["production_mutations"] == 0
    assert report["integrity"]["ledger_rows"] == 2
    assert len(report["integrity"]["ledger_sha256"]) == 64
