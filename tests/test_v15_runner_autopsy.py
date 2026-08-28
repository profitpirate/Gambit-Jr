from __future__ import annotations

from memecoin_bot.historical.runner_autopsy import (
    cohort_funnel,
    entry_gate,
    feature_diagnostics,
    miss_reason,
    rank_model,
    reconstruct_decision,
    selection_metrics,
    stable_random_score,
)


def _row(peak: float, score: float = 80, **overrides):
    row = {
        "mint": f"mint-{peak}-{score}",
        "peak_multiple": peak,
        "discovered": True,
        "discovered_early": True,
        "evaluated": True,
        "coverage": 100,
        "runner_score": score,
        "failure_score_lower_bound": 0,
        "failure_reasons": [],
        "entry_status": "OPEN",
        "tier": "PREMIUM" if score >= 75 else "SILENT_WATCH",
        "terminal_failure": False,
        "week_label": "2026-W01",
        "log_market_cap": peak,
    }
    row.update(overrides)
    return row


def test_reconstruction_preserves_unknowns_and_reports_failure_as_lower_bound():
    decision = reconstruct_decision(
        {
            "stage": "NEW",
            "timestamp_seconds": 180,
            "market_cap_growth": 1.1,
            "momentum_score": 90,
            "buyer_growth_score": 80,
            "creator_score": None,
            "concentration_score": 70,
            "survival_score": 100,
            "payoff_score": 70,
            "concentration_unknown": False,
            "toxic_creator": True,
        }
    )
    assert decision["coverage"] < 100
    assert decision["failure_score_lower_bound"] == 35
    assert decision["failure_reasons"] == ["TOXIC_CREATOR"]


def test_entry_gate_exact_boundaries_and_miss_taxonomy():
    assert entry_gate(1.69, 300, None) == "OPEN"
    assert entry_gate(1.7, 300, None) == "EXTENDED"
    assert entry_gate(3, 300, None) == "CHASING"
    assert miss_reason(_row(10, score=50)) == "LOW_RUNNER_SCORE"
    assert miss_reason(_row(10, coverage=50)) == "LOW_COVERAGE"
    assert miss_reason(_row(10, entry_status="CHASING")) == "ENTRY_CHASING"
    assert (
        miss_reason(_row(10, failure_score_lower_bound=75, failure_reasons=["RISK"]))
        == "FAILURE_SCORE_TOO_HIGH"
    )


def test_funnel_and_model_metrics_use_cohort_denominators():
    rows = [_row(20), _row(5, score=50), _row(1)]
    funnel = cohort_funnel(rows, 5)
    assert funnel["total_runners"]["count"] == 2
    assert funnel["reconstructed_signaled"]["count"] == 1
    metrics = selection_metrics(rows[:2], rows)
    assert metrics["2x_precision"] == 1
    assert metrics["20x_recall"] == 1


def test_feature_diagnostic_reports_direction_and_missingness():
    rows = [
        _row(3, log_market_cap=10),
        _row(2, log_market_cap=9),
        _row(1, log_market_cap=1),
        _row(1, log_market_cap=2),
        _row(1, log_market_cap=None),
    ]
    finding = feature_diagnostics(rows, ["log_market_cap"])[0]
    reverse_finding = feature_diagnostics(list(reversed(rows)), ["log_market_cap"])[0]
    assert finding["coverage"] == 0.8
    assert finding["standardized_2x_effect"] > 0
    assert finding == reverse_finding


def test_diagnostic_random_baseline_is_stable_and_mint_specific():
    first = stable_random_score({"mint": "mint-a"})
    assert first == stable_random_score({"mint": "mint-a"})
    assert first != stable_random_score({"mint": "mint-b"})
    assert 0 <= first <= 1


def test_model_ties_are_broken_by_mint_not_input_order():
    rows = [_row(2), _row(3), _row(4)]
    for index, row in enumerate(rows):
        row["mint"] = f"mint-{chr(ord('c') - index)}"
    forward = rank_model(rows, lambda _row: 1)
    reverse = rank_model(list(reversed(rows)), lambda _row: 1)
    assert forward["selected_mints"] == reverse["selected_mints"] == ["mint-a"]
