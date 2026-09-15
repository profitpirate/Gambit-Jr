#!/usr/bin/env python3
"""Hardening patch for the v2.1 four-arm tournament runner.

Guarantees matched-cohort atomic entry across all four paper arms and correct
account-equity snapshots when another tournament signal is concurrently open.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

import golden_buyer_reputation_core as core
import golden_management_v2 as v2
import golden_management_v2_1 as v21
import golden_management_v2_1_tournament_live as base

VERSION = "golden-management-v2.1-tournament-hardened-v1"


def _flat_active(self: base.Tournament, exclude_mint: str | None = None) -> dict[str, dict[str, Any]]:
    flat: dict[str, dict[str, Any]] = {}
    for mint, bundle in self.active.items():
        if exclude_mint is not None and mint == exclude_mint:
            continue
        for name, p in bundle["positions"].items():
            if p.get("status") == "OPEN":
                flat[f"{mint}:{name}"] = p
    return flat


async def _atomic_enter(self: base.Tournament, mint: str, decision: dict[str, Any], latest_states: dict[str, dict[str, Any]], delay_ms: float) -> None:
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
    async with self._lock:
        if len(self.active) >= base.MAX_CONCURRENT_SIGNALS:
            self.rejections["CONCURRENCY"] += 1
            return
        state = latest_states.get(mint)
        if not base.valid_state(state):
            self.rejections["NO_VALID_ENTRY_STATE"] += 1
            return

        entry_ns = time.time_ns()
        current_conv = v2.conviction(decision)
        enhanced = v21.enhanced_conviction(decision, self.evidence)
        tracker = v21.FlowTracker()
        flat_before = _flat_active(self)

        # Phase A: prepare every paper fill without mutating any account.  If one
        # arm cannot enter, none enters; the cohort therefore stays aligned.
        plan: dict[str, dict[str, Any]] = {}
        for name in v21.TOURNAMENT_ARMS:
            conv = enhanced if name == "FLOW_V2_1" else current_conv
            tier = str(conv["conviction_tier"])
            cfg = v2.TIERS[tier]
            acc = self.accounts[name]
            equity_before = acc.equity(flat_before)
            stake = min(max(0.0, acc.cash - core.RESERVE_SOL), equity_before * cfg.position_fraction)
            if stake <= 0:
                self.rejections["ATOMIC_INSUFFICIENT_CASH"] += 1
                return
            tokens, _ = core.quote_buy(stake, state)
            if tokens <= 0:
                self.rejections["ATOMIC_NO_FILL"] += 1
                return
            plan[name] = {"conv": conv, "tier": tier, "cfg": cfg, "stake": stake, "tokens": tokens, "equity_before": equity_before}

        # Phase B: commit all four matched paper entries together.
        latency = (entry_ns - int(decision["decision_ns"])) / 1_000_000.0
        positions: dict[str, dict[str, Any]] = {}
        for name in v21.TOURNAMENT_ARMS:
            item = plan[name]
            conv, tier, cfg = item["conv"], item["tier"], item["cfg"]
            stake, tokens = float(item["stake"]), float(item["tokens"])
            acc = self.accounts[name]
            acc.cash -= stake
            acc.execution_latencies_ms.append(latency)
            acc.execution_latencies_ms = acc.execution_latencies_ms[-5000:]
            if name == "FULL_2S_CONTROL": guardian: Any = v21.FullTwoSecondGuardian()
            elif name == "PARTIALS_2S": guardian = v21.TwoSecondPartialsGuardian(cfg)
            elif name == "V2_CURRENT": guardian = v2.RunnerGuardian(cfg)
            else: guardian = v21.FlowAwareGuardian(cfg, tracker)
            immediate = core.quote_sell(tokens, state)
            positions[name] = {
                "arm": name, "mint": mint, "status": "OPEN", "stake_sol": stake,
                "position_fraction": cfg.position_fraction, "conviction_tier": tier,
                "conviction_score": conv["conviction_score"], "conviction": conv,
                "original_tokens": tokens, "remaining_tokens": tokens,
                "realized_proceeds_sol": 0.0, "mark_remaining_value_sol": immediate,
                "entry_ns": entry_ns, "entry_state_ns": int(state.get("ns", 0)),
                "create_ns": int(decision["create_ns"]), "decision_ns": int(decision["decision_ns"]),
                "create_to_decision_ms": (int(decision["decision_ns"]) - int(decision["create_ns"])) / 1e6,
                "decision_to_entry_ms": latency, "create_to_entry_ms": (entry_ns - int(decision["create_ns"])) / 1e6,
                "entry_reason": dict(decision), "guardian": guardian, "actions": [], "path": [],
                "mfe_fraction": immediate / stake - 1.0, "mae_fraction": immediate / stake - 1.0,
                "equity_before_sol": float(item["equity_before"]),
            }
        self.active[mint] = {
            "mint": mint, "decision": decision, "entry_ns": entry_ns,
            "tracker": tracker, "positions": positions, "last_state_ns": -1,
            "e4_events": [],
        }
    await self._manage(mint, latest_states)


def _equity_correct_finish(self: base.Tournament, bundle: dict[str, Any], state: Mapping[str, Any]) -> None:
    mint = str(bundle["mint"])
    rows: dict[str, dict[str, Any]] = {}
    for name, p in bundle["positions"].items():
        row = {k: v for k, v in p.items() if k != "guardian"}
        if isinstance(p.get("guardian"), v21.FlowAwareGuardian):
            row["flow_decisions"] = list(p["guardian"].decisions)
        row["e4_same_coin_events"] = list(bundle["e4_events"])
        rows[name] = row

    # Remove this signal before measuring the account after close; any other
    # concurrent signal remains mark-to-market in balance_after_sol.
    self.active.pop(mint, None)
    flat_remaining = _flat_active(self)
    for name, acc in self.accounts.items():
        rows[name]["balance_after_sol"] = acc.equity(flat_remaining)
        acc.ledger.append(rows[name])
    self.post_exit[mint] = {
        "deadline_ns": time.time_ns() + int(base.POST_EXIT_SECONDS * 1e9),
        "rows": rows,
    }


base.Tournament._enter = _atomic_enter
base.Tournament._finish_bundle = _equity_correct_finish


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
