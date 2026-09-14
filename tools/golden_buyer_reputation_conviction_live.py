#!/usr/bin/env python3
"""Golden Buyer Reputation live-paper runner with a shadow-only conviction sizing layer.

The frozen Golden entry/exit strategy is unchanged. Real paper trades still use the
original 1.85% position fraction from golden_buyer_reputation_core.py.

For each qualifying trade from the moment this layer is activated, this wrapper also
records a deterministic conviction score and a hypothetical variable position size.
The shadow P&L is a sizing-only counterfactual using the exact percentage return of
the fixed-size paper trade. It is NOT market-impact/slippage adjusted and never
changes the actual paper account.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any, Mapping

import golden_buyer_reputation_live as base

SHADOW_VERSION = "golden-conviction-shadow-v1"

_ORIGINAL_INIT = base.PaperAccount.__init__
_ORIGINAL_TRADE = base.PaperAccount._trade
_ORIGINAL_SUMMARY = base.PaperAccount.summary
_ORIGINAL_PERSISTENCE = base.PaperAccount.persistence


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def conviction(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Frozen shadow-v1 score using only information available at entry.

    A qualifying signal starts at 50 points. Extra points reward:
      - historical early-buyer win-rate above the 70% entry floor (max +25),
      - larger causal prior sample size from 10 to 100 appearances (max +15),
      - independent buyer diversity from 1 to 5 buyers (max +10).

    Position fractions are hypothetical only:
      50-64 -> 2.0%
      65-74 -> 3.0%
      75-84 -> 5.0%
      85-100 -> 8.0%
    """
    rate = float(decision.get("prior_win_rate") or 0.0)
    appearances = max(0, int(decision.get("prior_appearances") or 0))
    buyers = max(0, int(decision.get("buyer_count") or len(decision.get("buyers") or [])))

    rate_component = 25.0 * _clip((rate - 0.70) / 0.20)
    if appearances <= 10:
        sample_component = 0.0
    else:
        sample_component = 15.0 * _clip(math.log(appearances / 10.0) / math.log(10.0))
    diversity_component = 10.0 * _clip((buyers - 1.0) / 4.0)
    score = round(_clip(50.0 + rate_component + sample_component + diversity_component, 0.0, 100.0), 3)

    if score >= 85.0:
        tier, fraction = "EXCEPTIONAL", 0.08
    elif score >= 75.0:
        tier, fraction = "HIGH", 0.05
    elif score >= 65.0:
        tier, fraction = "MEDIUM", 0.03
    else:
        tier, fraction = "BASE", 0.02

    return {
        "shadow_conviction_version": SHADOW_VERSION,
        "shadow_conviction_score": score,
        "shadow_conviction_tier": tier,
        "shadow_position_fraction": fraction,
        "shadow_score_inputs": {
            "prior_win_rate": rate,
            "prior_appearances": appearances,
            "buyer_count": buyers,
        },
    }


def _recompute_shadow(account: base.PaperAccount) -> dict[str, Any]:
    rows = [r for r in account.ledger if r.get("status") == "CLOSED"]
    activation = int(getattr(account, "shadow_activation_closed_trades", len(rows)))
    activation = max(0, min(activation, len(rows)))

    if activation:
        baseline = float(rows[activation - 1].get("balance_after_sol", account.starting_balance))
    else:
        baseline = float(account.starting_balance)

    equity = baseline
    wins = losses = 0
    shadow_gains = 0.0
    shadow_losses = 0.0
    tested = 0

    for idx, row in enumerate(rows):
        if idx < activation:
            row["shadow_conviction_eligible"] = False
            continue

        meta = conviction(row)
        row.update(meta)
        row["shadow_conviction_eligible"] = True
        fixed_stake = float(row.get("stake_sol") or 0.0)
        fixed_pnl = float(row.get("pnl_sol") or 0.0)
        return_fraction = fixed_pnl / fixed_stake if fixed_stake > 0 else 0.0
        fraction = float(meta["shadow_position_fraction"])
        shadow_stake = max(0.0, equity * fraction)
        shadow_pnl = shadow_stake * return_fraction
        equity += shadow_pnl

        row["shadow_equity_before_sol"] = equity - shadow_pnl
        row["shadow_stake_sol"] = shadow_stake
        row["shadow_return_fraction_from_fixed_trade"] = return_fraction
        row["shadow_pnl_sol"] = shadow_pnl
        row["shadow_equity_after_sol"] = equity
        row["shadow_market_impact_adjusted"] = False

        tested += 1
        if shadow_pnl > 0:
            wins += 1
            shadow_gains += shadow_pnl
        else:
            losses += 1
            shadow_losses += -shadow_pnl

    return {
        "version": SHADOW_VERSION,
        "enabled": True,
        "execution_unchanged": True,
        "actual_position_fraction": 0.0185,
        "activation_closed_trades": activation,
        "activation_equity_sol": baseline,
        "shadow_trades": tested,
        "wins": wins,
        "losses": losses,
        "shadow_equity_sol": equity,
        "shadow_net_pnl_sol_since_activation": equity - baseline,
        "shadow_roi_since_activation": (equity / baseline - 1.0) if baseline else None,
        "profit_factor": shadow_gains / shadow_losses if shadow_losses else (999.0 if shadow_gains else None),
        "market_impact_adjusted": False,
        "note": "Sizing-only counterfactual; same selected trades and percentage returns as fixed 1.85% paper execution.",
    }


def _patched_init(self: base.PaperAccount, starting_balance: float, resume: Mapping[str, Any] | None = None) -> None:
    resume = resume or {}
    _ORIGINAL_INIT(self, starting_balance, resume)
    saved_activation = resume.get("shadow_activation_closed_trades")
    # First activation starts AFTER all trades that already existed before this wrapper.
    self.shadow_activation_closed_trades = int(saved_activation) if saved_activation is not None else self.closed()
    _recompute_shadow(self)


async def _patched_trade(self: base.PaperAccount, mint: str, decision: dict[str, Any], latest_states: dict[str, dict[str, Any]], execution_delay_ms: float) -> None:
    await _ORIGINAL_TRADE(self, mint, decision, latest_states, execution_delay_ms)
    _recompute_shadow(self)


def _patched_summary(self: base.PaperAccount) -> dict[str, Any]:
    payload = _ORIGINAL_SUMMARY(self)
    payload["shadow_conviction"] = _recompute_shadow(self)
    return payload


def _patched_persistence(self: base.PaperAccount) -> dict[str, Any]:
    payload = _ORIGINAL_PERSISTENCE(self)
    payload["shadow_activation_closed_trades"] = int(self.shadow_activation_closed_trades)
    payload["shadow_conviction_version"] = SHADOW_VERSION
    payload["shadow_conviction"] = _recompute_shadow(self)
    return payload


base.PaperAccount.__init__ = _patched_init
base.PaperAccount._trade = _patched_trade
base.PaperAccount.summary = _patched_summary
base.PaperAccount.persistence = _patched_persistence


def shadow_self_test() -> None:
    low = conviction({"prior_win_rate": 0.70, "prior_appearances": 10, "buyer_count": 1})
    high = conviction({"prior_win_rate": 0.90, "prior_appearances": 100, "buyer_count": 5})
    assert low["shadow_conviction_score"] == 50.0
    assert low["shadow_position_fraction"] == 0.02
    assert high["shadow_conviction_score"] == 100.0
    assert high["shadow_position_fraction"] == 0.08
    print("SHADOW_CONVICTION_SELF_TEST_OK")


def main() -> int:
    args = base.parser().parse_args()
    if args.self_test:
        base.self_test()
        shadow_self_test()
        return 0
    required = ("production_root", "corpus", "state_output", "status", "output")
    missing = [x for x in required if getattr(args, x) is None]
    if missing:
        raise SystemExit("missing required arguments: " + ",".join(missing))
    if not math.isclose(args.starting_balance_sol, 5.0):
        raise SystemExit("this campaign is frozen to a 5 SOL starting paper balance")
    return asyncio.run(base.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
