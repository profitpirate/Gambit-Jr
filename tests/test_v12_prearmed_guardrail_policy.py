from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_prearmed_guardrail_policy as subject


def base_paper() -> dict:
    return {
        "metrics": {
            "closed_trades": 30,
            "maximum_closed_equity_drawdown_fraction": 0.05,
        }
    }


def base_shadow() -> dict:
    return {
        "reliability": {
            "status": "PASS",
            "frozen_v12_modified": False,
        },
        "drift": {"status": "GREEN"},
        "risk_of_ruin": {
            "status": "SIMULATED",
            "probability_drawdown_ge_20pct": 0.05,
            "probability_bankroll_below_half": 0.01,
        },
        "execution_stress_summary": {
            "5ms_fee_1.0x": {
                "attempted": 30,
                "net_pnl_sol": 0.5,
                "profit_factor": 2.0,
            },
            "25ms_fee_1.0x": {
                "attempted": 30,
                "net_pnl_sol": 0.3,
            },
            "50ms_fee_1.0x": {
                "attempted": 30,
                "net_pnl_sol": 0.1,
            },
        },
        "creator_concentration": {
            "top_creator_trade_share": 0.3,
            "effective_creator_count": 5.0,
        },
    }


def test_clear_when_all_guardrails_are_satisfied() -> None:
    result = subject.evaluate_guardrails(
        paper_state=base_paper(),
        shadow_state=base_shadow(),
        dependency_summary={
            "after": {"critical": 0, "high": 0}
        },
    )
    assert result["state"] == "CLEAR"
    assert result["hard_halts"] == []
    assert result["real_money_authorised"] is False


def test_integrity_failure_halts() -> None:
    shadow = base_shadow()
    shadow["reliability"]["status"] = "FAIL"
    result = subject.evaluate_guardrails(
        paper_state=base_paper(),
        shadow_state=shadow,
    )
    assert result["state"] == "HALT"
    assert "EVIDENCE_OR_STATE_INTEGRITY_FAILED" in result["hard_halts"]


def test_red_drift_after_20_trades_halts() -> None:
    shadow = base_shadow()
    shadow["drift"]["status"] = "RED"
    result = subject.evaluate_guardrails(
        paper_state=base_paper(),
        shadow_state=shadow,
    )
    assert result["state"] == "HALT"
    assert "MODEL_DRIFT_RED" in result["hard_halts"]


def test_high_dependency_advisories_warn_but_do_not_authorise() -> None:
    result = subject.evaluate_guardrails(
        paper_state=base_paper(),
        shadow_state=base_shadow(),
        dependency_summary={
            "after": {"critical": 0, "high": 8}
        },
    )
    assert result["state"] == "WATCH"
    assert "HIGH_RUNTIME_DEPENDENCY_ADVISORIES_8" in result["warnings"]
    assert result["future_executor_action"] == "REQUIRE_OPERATOR_REVIEW"


def test_critical_dependency_advisory_halts() -> None:
    result = subject.evaluate_guardrails(
        paper_state=base_paper(),
        shadow_state=base_shadow(),
        dependency_summary={
            "after": {"critical": 1, "high": 8}
        },
    )
    assert result["state"] == "HALT"
    assert "CRITICAL_RUNTIME_DEPENDENCY_ADVISORY" in result["hard_halts"]
