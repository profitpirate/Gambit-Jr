"""Fail-closed guardrail policy for V12 Pre-Armed research and future execution.

This module has no transaction authority.  It translates already-persisted
paper/shadow evidence into an explicit CLEAR/WATCH/HALT decision that a future
executor can consume before any real-money integration is considered.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def evaluate_guardrails(
    *,
    paper_state: Mapping[str, Any],
    shadow_state: Mapping[str, Any],
    dependency_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    hard_halts: list[str] = []
    warnings: list[str] = []

    reliability = shadow_state.get("reliability") or {}
    if reliability.get("status") != "PASS":
        hard_halts.append("EVIDENCE_OR_STATE_INTEGRITY_FAILED")

    if reliability.get("frozen_v12_modified") is not False:
        hard_halts.append("FROZEN_MODEL_MUTATION_NOT_PROVEN_FALSE")

    paper_metrics = paper_state.get("metrics") or {}
    closed = integer(paper_metrics.get("closed_trades"))
    drawdown = finite(paper_metrics.get("maximum_closed_equity_drawdown_fraction"))
    if drawdown > 0.20:
        hard_halts.append("CLOSED_EQUITY_DRAWDOWN_OVER_20_PERCENT")
    elif drawdown > 0.12:
        warnings.append("CLOSED_EQUITY_DRAWDOWN_OVER_12_PERCENT")

    drift = shadow_state.get("drift") or {}
    drift_status = str(drift.get("status") or "UNKNOWN").upper()
    if closed >= 20 and drift_status == "RED":
        hard_halts.append("MODEL_DRIFT_RED")
    elif drift_status in {"YELLOW", "UNKNOWN"}:
        warnings.append(f"MODEL_DRIFT_{drift_status}")

    risk = shadow_state.get("risk_of_ruin") or {}
    if risk.get("status") == "SIMULATED":
        dd20 = finite(risk.get("probability_drawdown_ge_20pct"))
        below_half = finite(risk.get("probability_bankroll_below_half"))
        if dd20 > 0.25:
            hard_halts.append("MONTE_CARLO_20_PERCENT_DD_PROBABILITY_OVER_25_PERCENT")
        elif dd20 > 0.10:
            warnings.append("MONTE_CARLO_20_PERCENT_DD_PROBABILITY_OVER_10_PERCENT")
        if below_half > 0.10:
            hard_halts.append("MONTE_CARLO_HALF_BANKROLL_PROBABILITY_OVER_10_PERCENT")

    stress = shadow_state.get("execution_stress_summary") or {}
    baseline = stress.get("5ms_fee_1.0x") or {}
    stress25 = stress.get("25ms_fee_1.0x") or {}
    stress50 = stress.get("50ms_fee_1.0x") or {}

    baseline_samples = integer(baseline.get("attempted"))
    if baseline_samples >= 20:
        if finite(baseline.get("net_pnl_sol")) <= 0:
            hard_halts.append("BASELINE_EXECUTION_STRESS_NET_NEGATIVE")
        if finite(baseline.get("profit_factor")) < 1.25:
            hard_halts.append("BASELINE_EXECUTION_STRESS_PF_BELOW_1_25")

    for label, result in (("25MS", stress25), ("50MS", stress50)):
        attempted = integer(result.get("attempted"))
        if attempted >= 20 and finite(result.get("net_pnl_sol")) <= 0:
            warnings.append(f"{label}_EXECUTION_STRESS_NET_NEGATIVE")

    concentration = shadow_state.get("creator_concentration") or {}
    top_share = finite(concentration.get("top_creator_trade_share"))
    effective = finite(concentration.get("effective_creator_count"))
    if closed >= 20 and top_share > 0.60:
        warnings.append("TOP_CREATOR_SHARE_OVER_60_PERCENT")
    if closed >= 30 and effective < 3.0:
        warnings.append("EFFECTIVE_CREATOR_COUNT_BELOW_3")

    if dependency_summary:
        after = dependency_summary.get("after") or {}
        critical = integer(after.get("critical"))
        high = integer(after.get("high"))
        if critical > 0:
            hard_halts.append("CRITICAL_RUNTIME_DEPENDENCY_ADVISORY")
        if high > 0:
            warnings.append(f"HIGH_RUNTIME_DEPENDENCY_ADVISORIES_{high}")

    if hard_halts:
        state = "HALT"
    elif warnings:
        state = "WATCH"
    else:
        state = "CLEAR"

    return {
        "version": "v12-pre-armed-guardrail-policy-v1",
        "state": state,
        "hard_halts": sorted(set(hard_halts)),
        "warnings": sorted(set(warnings)),
        "closed_trades_observed": closed,
        "shadow_only": True,
        "real_money_authorised": False,
        "future_executor_action": (
            "BLOCK_NEW_ENTRIES"
            if state == "HALT"
            else "REQUIRE_OPERATOR_REVIEW"
            if state == "WATCH"
            else "NO_RESEARCH_GUARDRAIL_BLOCK"
        ),
    }
