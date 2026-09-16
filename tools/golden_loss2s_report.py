"""Validate and compare parent versus 2s-loss shadow arms."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from golden_loss2s_engine import (
    ARMS,
    FULL2,
    V2_LOSS2,
    CampaignEngine,
    Config,
)
import golden_top4_engine as top4


PAIRS = (("FULL_3S", FULL2), (top4.V2, V2_LOSS2))


def validate_state(raw: dict[str, Any], *, require_target: bool = True):
    if raw.get("market_source") != "DIRECT_SOLANA_NEW_CREATIONS":
        raise ValueError("wrong market source")
    if any(s.get("errors") for s in raw.get("sessions", [])):
        raise ValueError("campaign session contains errors")
    state = raw["engine"]
    engine = CampaignEngine(
        Config(**state["config"]), state, fixture_mode=state["fixture_mode"]
    )
    counts = engine.forward_counts()
    if require_target and min(counts.values(), default=0) < 100:
        raise ValueError("loss2s campaign has not reached 100 verified per model")
    engine.validate()
    return engine


def _ledger(engine, arm):
    return {p["mint"]: p for p in engine.accounts[arm]["ledger"]}


def compare_pair(engine, parent: str, shadow: str) -> dict[str, Any]:
    a = _ledger(engine, parent)
    b = _ledger(engine, shadow)
    common = sorted(engine.verified_mints(parent) & engine.verified_mints(shadow))
    triggered = [m for m in common if b[m].get("loss2s_triggered") is True]
    deltas = [b[m]["net_pnl_sol"] - a[m]["net_pnl_sol"] for m in common]
    triggered_deltas = [
        b[m]["net_pnl_sol"] - a[m]["net_pnl_sol"] for m in triggered
    ]
    return {
        "parent": parent,
        "shadow": shadow,
        "paired_verified_trades": len(common),
        "parent_net_pnl_sol": sum(a[m]["net_pnl_sol"] for m in common),
        "shadow_net_pnl_sol": sum(b[m]["net_pnl_sol"] for m in common),
        "shadow_minus_parent_net_pnl_sol": sum(deltas),
        "shadow_better_trade_count": sum(x > 0 for x in deltas),
        "shadow_worse_trade_count": sum(x < 0 for x in deltas),
        "same_trade_count": sum(x == 0 for x in deltas),
        "checkpoint_triggered_count": len(triggered),
        "triggered_shadow_minus_parent_net_pnl_sol": sum(triggered_deltas),
        "parent_eventual_winners_cut_at_2s": sum(
            a[m]["net_pnl_sol"] > 0 for m in triggered
        ),
        "parent_eventual_losses_cut_at_2s": sum(
            a[m]["net_pnl_sol"] < 0 for m in triggered
        ),
        "shadow_win_rate": (
            sum(b[m]["net_pnl_sol"] > 0 for m in common) / len(common)
            if common
            else None
        ),
        "parent_win_rate": (
            sum(a[m]["net_pnl_sol"] > 0 for m in common) / len(common)
            if common
            else None
        ),
    }


def analyse(engine):
    return {
        "schema": "golden-loss2s-report-1",
        "paper_only": True,
        "models": list(ARMS),
        "verified_forward_counts": engine.forward_counts(),
        "comparisons": [compare_pair(engine, *pair) for pair in PAIRS],
        "interpretation": (
            "Positive shadow_minus_parent_net_pnl_sol means the 2s checkpoint "
            "improved paired paper expectancy over the sampled forward cohort; "
            "negative means it reduced it. No result is a guarantee of live fills."
        ),
    }


def report(state_path: Path, output: Path):
    raw = json.loads(state_path.read_text())
    engine = validate_state(raw, require_target=True)
    result = analyse(engine)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    lines = [
        "# Golden 2s loss-exit forward A/B",
        "",
        "Paper-only, direct-Solana forward observations. All values below are paired "
        "on verified mints traded by both the parent and its shadow.",
        "",
    ]
    for row in result["comparisons"]:
        lines += [
            f"## {row['parent']} vs {row['shadow']}",
            f"- Paired verified trades: {row['paired_verified_trades']}",
            f"- Parent net PnL: {row['parent_net_pnl_sol']:.9f} SOL",
            f"- Shadow net PnL: {row['shadow_net_pnl_sol']:.9f} SOL",
            f"- Shadow - parent: {row['shadow_minus_parent_net_pnl_sol']:.9f} SOL",
            f"- 2s checkpoint triggered: {row['checkpoint_triggered_count']}",
            f"- Eventual parent winners cut at 2s: {row['parent_eventual_winners_cut_at_2s']}",
            f"- Eventual parent losses cut at 2s: {row['parent_eventual_losses_cut_at_2s']}",
            "",
        ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return result


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("state", type=Path)
    p.add_argument("--output", type=Path, default=Path("artifacts/final"))
    a = p.parse_args()
    report(a.state, a.output)
