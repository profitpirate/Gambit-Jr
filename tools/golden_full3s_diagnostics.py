"""Post-run research only. These diagnostics never choose an entry or exit."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def classify_loss(row: Mapping[str, Any], delayed_net_marks: Sequence[float] | None = None) -> dict[str, Any]:
    net = float(row["net_pnl_sol"])
    tags: list[str] = []
    if net < 0:
        if row["gross_pnl_sol"] > 0: tags.append("FEE_ERASED_GAIN")
        if (row.get("mfe") or 0) > 0: tags.append("GIVEBACK_AFTER_POSITIVE_NET_MARK")
        if any(a.get("outcome") == "MODELLED_LANDED_FAILURE" for a in row.get("actions", [])):
            tags.append("EXECUTION_FAILURE_COST_OR_DELAY")
        if delayed_net_marks is not None and any(x > 0 for x in delayed_net_marks):
            tags.append("LATER_POSITIVE_NET_MARK_HINDSIGHT_ONLY")
        if (row.get("mfe") is not None and row["mfe"] <= 0 and delayed_net_marks is not None and
                delayed_net_marks and max(delayed_net_marks) <= 0):
            tags.append("NO_OBSERVED_NET_RECOVERY_IN_COVERED_WINDOW")
    post = row.get("post") or {}
    return {"net_loss": net < 0, "overlapping_categories": tags,
            "post_exit_spot_higher": post.get("went_higher"),
            "post_exit_net_recovery": None if delayed_net_marks is None or not delayed_net_marks else max(delayed_net_marks) > 0,
            "coverage": post.get("coverage", "UNKNOWN"),
            "hindsight_is_executable_profit": False}


def filter_tradeoff(evaluations: Mapping[str, Any], baseline_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed = {r["mint"]: r for r in baseline_rows if r.get("status") == "CLOSED"}
    prevented = sacrificed = 0.0
    rejected = missing = 0
    by_reason: dict[str, dict[str, Any]] = {}
    for mint, decision in evaluations.items():
        if not decision.get("baseline_accept") or decision.get("accept"): continue
        rejected += 1
        if mint not in observed:
            missing += 1; continue
        p = float(observed[mint]["net_pnl_sol"])
        if not math.isfinite(p): raise ValueError("nonfinite comparison PnL")
        prevented += max(0, -p); sacrificed += max(0, p)
        for reason in decision["vetoes"]:
            item = by_reason.setdefault(reason, {"coins": 0, "prevented_loss_sol": 0.0, "sacrificed_winner_sol": 0.0})
            item["coins"] += 1
            item["prevented_loss_sol"] += max(0, -p)
            item["sacrificed_winner_sol"] += max(0, p)
    return {"rejected_baseline_signals": rejected, "missing_counterfactual_outcomes": missing,
            "prevented_loss_at_observer_stake_sol": prevented,
            "sacrificed_winner_at_observer_stake_sol": sacrificed,
            "descriptive_difference_sol": prevented-sacrificed, "by_reason_overlapping": by_reason,
            "not_an_independently_compounded_account": True,
            "not_proof_of_causal_or_out_of_sample_improvement": True}


def evidence_gate(runtime: Any, minimum: int = 100) -> dict[str, Any]:
    """A passing offline stress suite or recovered coin can never pass this gate."""
    if minimum < 100: raise ValueError("completion threshold cannot be lowered")
    e = runtime.candidate
    return {"model": "FULL_3S_V2", "minimum": minimum,
            "fully_forward_observed": 0 if runtime.fixture_mode else e.forward_count(),
            "profitability_validated": False,
            "status": "NOT_LIVE_TESTED" if runtime.fixture_mode else "NEEDS_EXTERNAL_PROVENANCE_AUDIT",
            "complete": False,
            "reason": "Later live-feed/on-chain creation audit must be supplied; this offline build cannot self-certify live evidence"}
