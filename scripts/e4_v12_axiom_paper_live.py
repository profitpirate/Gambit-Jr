#!/usr/bin/env python3
"""Accumulate a frozen 50-trade forward paper-live ledger with Axiom costs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e4_v12_holdout_social_backfill as social
import e4_v12_profit_survival_search as base
import e4_v12_true_latency_replay as replay

SCHEMA_VERSION = "e4-v12-prearmed-axiom-paper-live-v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def wilson_lower(wins: int, trades: int, z: float = 1.959963984540054) -> float:
    if trades <= 0:
        return 0.0
    rate = wins / trades
    denominator = 1 + z * z / trades
    centre = rate + z * z / (2 * trades)
    margin = z * math.sqrt(rate * (1 - rate) / trades + z * z / (4 * trades**2))
    return (centre - margin) / denominator


@dataclass(frozen=True, slots=True)
class Costs:
    axiom_bps: float
    pump_bps: float
    base_sol: float
    priority_sol: float
    buy_bribe_sol: float
    sell_bribe_sol: float

    @property
    def variable_rate(self) -> float:
        return (self.axiom_bps + self.pump_bps) / 10_000.0

    @property
    def buy_fixed(self) -> float:
        return self.base_sol + self.priority_sol + self.buy_bribe_sol

    @property
    def sell_fixed(self) -> float:
        return self.base_sol + self.priority_sol + self.sell_bribe_sol


@dataclass(frozen=True, slots=True)
class Policy:
    stop: float
    first_take: float
    first_fraction: float
    hold_ms: int
    trail_retrace: float


@dataclass(frozen=True, slots=True)
class Candidate:
    run_id: str
    mint: str
    creator: str
    handle: str
    status_id: str
    tweet_age_seconds: float
    create_ns: int
    decision_ns: int
    decision_sequence: int
    creator_seed_sol: float


def load_costs(model: Mapping[str, Any]) -> Costs:
    values = model["execution_costs"]
    return Costs(
        axiom_bps=finite(values["axiom_net_fee_bps_each_side"]),
        pump_bps=finite(values["pump_bonding_curve_fee_bps_each_side"]),
        base_sol=finite(values["solana_base_fee_sol_per_transaction"]),
        priority_sol=finite(values["priority_fee_sol_per_transaction"]),
        buy_bribe_sol=finite(values["mev_bribe_sol_on_buy"]),
        sell_bribe_sol=finite(values["mev_bribe_sol_on_sell"]),
    )


def load_policy(model: Mapping[str, Any]) -> Policy:
    values = model["exit_policy"]
    return Policy(
        stop=finite(values["stop"]),
        first_take=finite(values["first_take"]),
        first_fraction=finite(values["first_fraction"]),
        hold_ms=integer(values["hold_ms"]),
        trail_retrace=finite(values["trail_retrace"]),
    )


def read_cache(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in paths:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                output.update(
                    (str(key), dict(value))
                    for key, value in payload.items()
                    if isinstance(value, Mapping)
                )
    return output


def parse_pair(value: str) -> tuple[str, Path, Path]:
    """Parse run=batch::events, using :: so Windows drive letters remain valid."""
    run_id = ""
    body = value
    if "=" in value:
        run_id, body = value.split("=", 1)
    if "::" in body:
        batch, events = body.split("::", 1)
        batch_path = Path(batch)
        return run_id or batch_path.stem, batch_path, Path(events)
    return replay.parse_pair(value)


def creation(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next(
        (row for row in rows if str(row.get("kind") or "").upper() == "CREATE"),
        None,
    )


def creator_seed(
    rows: Sequence[Mapping[str, Any]], create: Mapping[str, Any], creator: str
) -> tuple[float, int, int]:
    signature = str(create.get("signature") or "")
    matching = [
        row
        for row in rows
        if str(row.get("kind") or "").upper() in base.choice_sets.BUY_KINDS
        and str(row.get("trader") or "") == creator
        and str(row.get("signature") or "") == signature
    ]
    if not matching:
        return 0.0, integer(create.get("received_ns")), integer(create.get("__sequence"), -1)
    decision = max(
        matching,
        key=lambda row: (integer(row.get("received_ns")), integer(row.get("__sequence"), -1)),
    )
    return (
        sum(finite(row.get("sol_amount")) for row in matching),
        integer(decision.get("received_ns")),
        integer(decision.get("__sequence"), -1),
    )


async def candidates_for_run(
    run: replay.RunData,
    model: Mapping[str, Any],
    cache: dict[str, dict[str, Any]],
) -> tuple[list[Candidate], Counter[str]]:
    selector = model["selector"]
    creator_handles = {
        str(creator): {str(handle).lower().lstrip("@") for handle in handles}
        for creator, handles in model["creator_handles"].items()
    }
    prior_attempts = {
        str(key): integer(value)
        for key, value in model["creator_prior_e4_attempts"].items()
    }
    preliminaries: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for mint, rows in run.events_by_mint.items():
        create = creation(rows)
        if create is None:
            counts["missing_create"] += 1
            continue
        create_ns = integer(create.get("received_ns"))
        if create_ns <= integer(model["maximum_evidence_ns"]):
            raise ValueError(f"run {run.run_id} is not strictly after the frozen evidence epoch")
        raw = create.get("raw") if isinstance(create.get("raw"), Mapping) else {}
        creator = str(create.get("creator") or raw.get("creator") or create.get("trader") or "")
        if prior_attempts.get(creator, 0) < integer(selector["minimum_prior_e4_attempts"]):
            counts["unknown_creator"] += 1
            continue
        if bool(raw.get("is_mayhem_mode")):
            counts["mayhem"] += 1
            continue
        seed, decision_ns, decision_sequence = creator_seed(rows, create, creator)
        if seed < finite(selector["minimum_creator_seed_sol"]):
            counts["creator_seed_below_floor"] += 1
            continue
        uri = str(raw.get("uri") or create.get("uri") or "")
        if not uri:
            counts["missing_metadata_uri"] += 1
            continue
        preliminaries.append(
            {
                "mint": mint,
                "creator": creator,
                "create_ns": create_ns,
                "decision_ns": decision_ns,
                "decision_sequence": decision_sequence,
                "creator_seed_sol": seed,
                "uri": uri,
                "known_handles": creator_handles.get(creator, set()),
            }
        )

    missing = [row["uri"] for row in preliminaries if row["uri"] not in cache]
    if missing:
        cache.update(await social.fetch_metadata(missing))

    output = []
    for row in preliminaries:
        metadata = cache.get(row["uri"], {})
        if metadata.get("error"):
            counts[f"metadata_{metadata['error']}"] += 1
            continue
        handle = str(
            metadata.get("handle")
            or social.social_handle(str(metadata.get("twitter") or ""))
        ).lower().lstrip("@")
        if not handle or handle not in row["known_handles"]:
            counts["not_prearmed_handle"] += 1
            continue
        status_id = str(
            metadata.get("status_id")
            or social.social_status_id(str(metadata.get("twitter") or ""))
        )
        status_ns = social.snowflake_timestamp_ns(status_id)
        if status_ns <= 0:
            counts["missing_status_timestamp"] += 1
            continue
        age = (row["create_ns"] - status_ns) / 1_000_000_000
        if not (
            finite(selector["minimum_tweet_age_seconds"])
            <= age
            <= finite(selector["maximum_tweet_age_seconds"])
        ):
            counts["social_age_outside_window"] += 1
            continue
        output.append(
            Candidate(
                run_id=run.run_id,
                mint=row["mint"],
                creator=row["creator"],
                handle=handle,
                status_id=status_id,
                tweet_age_seconds=age,
                create_ns=row["create_ns"],
                decision_ns=row["decision_ns"],
                decision_sequence=row["decision_sequence"],
                creator_seed_sol=row["creator_seed_sol"],
            )
        )
    output.sort(key=lambda item: (item.decision_ns, item.mint))
    counts["selected_candidates"] = len(output)
    return output, counts


def curve_input_for_budget(budget: float, costs: Costs) -> float:
    return max(0.0, (budget - costs.buy_fixed) / (1.0 + costs.variable_rate))


def trace_map(run_id: str, events_path: Path) -> dict[str, base.Trace]:
    spec = base.CaptureSpec(run_id, "live", 0, 0, events_path, "")
    traces, parse_errors = base.load_capture(spec, Counter())
    if parse_errors:
        raise ValueError(f"{parse_errors} malformed live event rows")
    for trace in traces:
        trace.points.sort(key=lambda point: point.timestamp_ns)
    return {trace.mint: trace for trace in traces}


def simulate_trade(
    run: replay.RunData,
    trace: base.Trace,
    candidate: Candidate,
    budget: float,
    model: Mapping[str, Any],
    costs: Costs,
    policy: Policy,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    selector = model["selector"]
    states = run.reserves_by_mint.get(candidate.mint, [])
    decision = replay.state_at_or_before(
        states, candidate.decision_ns, candidate.decision_sequence
    )
    fill_ns = candidate.decision_ns + int(finite(selector["entry_latency_ms"]) * 1_000_000)
    fill = replay.state_at_or_before(states, fill_ns)
    create = replay.state_at_or_before(states, candidate.create_ns)
    if decision is None or fill is None or create is None:
        return None, {"reason": "missing_causal_reserve_state", "fee_sol": 0.0}
    curve_input = curve_input_for_budget(budget, costs)
    if curve_input <= 0:
        return None, {"reason": "insufficient_available_cash", "fee_sol": 0.0}
    expected_tokens = replay.buy_tokens(curve_input, decision)
    received_tokens = replay.buy_tokens(curve_input, fill)
    output_ratio = received_tokens / max(expected_tokens, 1e-18)
    create_price = create.price_sol or create.virtual_sol / max(create.virtual_tokens, 1e-18)
    fill_price = fill.price_sol or fill.virtual_sol / max(fill.virtual_tokens, 1e-18)
    price_multiple = fill_price / max(create_price, 1e-18)
    if (
        output_ratio < finite(selector["minimum_entry_output_ratio"])
        or price_multiple > finite(selector["maximum_create_to_entry_price_multiple"])
    ):
        failed_fee = costs.base_sol + costs.priority_sol
        return None, {
            "reason": "entry_output_guard_rejected",
            "fee_sol": failed_fee,
            "output_ratio": output_ratio,
            "create_to_fill_price_multiple": price_multiple,
        }

    axiom_entry = curve_input * costs.axiom_bps / 10_000.0
    pump_entry = curve_input * costs.pump_bps / 10_000.0
    entry_cost = curve_input + axiom_entry + pump_entry + costs.buy_fixed
    remaining = received_tokens
    proceeds = 0.0
    axiom_exit = 0.0
    pump_exit = 0.0
    exit_fixed = 0.0
    first_done = False
    peak = 1.0
    exit_count = 0
    reason = "MAXIMUM_HOLD"
    last = next(
        (
            point
            for point in reversed(trace.points)
            if point.timestamp_ns <= fill_ns and point.virtual_sol > 0 and point.virtual_tokens > 0
        ),
        None,
    )
    deadline = fill_ns + policy.hold_ms * 1_000_000
    for point in trace.points:
        if point.timestamp_ns <= fill_ns:
            continue
        if point.timestamp_ns > deadline:
            break
        if point.price_sol <= 0 or point.virtual_sol <= 0 or point.virtual_tokens <= 0:
            continue
        last = point
        multiple = point.price_sol / max(fill_price, 1e-18)
        peak = max(peak, multiple)
        if not first_done and policy.first_fraction > 0 and multiple >= policy.first_take:
            amount = received_tokens * policy.first_fraction
            gross = amount * point.virtual_sol / max(point.virtual_tokens + amount, 1e-18)
            axiom = gross * costs.axiom_bps / 10_000.0
            pump = gross * costs.pump_bps / 10_000.0
            proceeds += max(0.0, gross - axiom - pump - costs.sell_fixed)
            axiom_exit += axiom
            pump_exit += pump
            exit_fixed += costs.sell_fixed
            remaining -= amount
            exit_count += 1
            first_done = True
        floor = policy.stop
        if first_done:
            floor = max(floor, 1.02, peak * (1.0 - policy.trail_retrace))
        elif peak >= policy.first_take:
            floor = max(floor, peak * (1.0 - policy.trail_retrace))
        if point.complete or multiple <= floor:
            reason = "LIQUIDITY_EMERGENCY" if point.complete else "TRAILING_OR_STOP"
            break
    if last is None:
        return None, {"reason": "missing_exit_state", "fee_sol": costs.base_sol + costs.priority_sol}
    if remaining > 0:
        gross = remaining * last.virtual_sol / max(last.virtual_tokens + remaining, 1e-18)
        axiom = gross * costs.axiom_bps / 10_000.0
        pump = gross * costs.pump_bps / 10_000.0
        proceeds += max(0.0, gross - axiom - pump - costs.sell_fixed)
        axiom_exit += axiom
        pump_exit += pump
        exit_fixed += costs.sell_fixed
        exit_count += 1
    pnl = proceeds - entry_cost
    return (
        {
            "run_id": candidate.run_id,
            "mint": candidate.mint,
            "creator": candidate.creator,
            "social_handle": candidate.handle,
            "social_status_id": candidate.status_id,
            "tweet_age_seconds": candidate.tweet_age_seconds,
            "creator_seed_sol": candidate.creator_seed_sol,
            "decision_ns": candidate.decision_ns,
            "fill_ns": fill_ns,
            "exit_ns": last.timestamp_ns,
            "entry_latency_ms": finite(selector["entry_latency_ms"]),
            "entry_budget_sol": budget,
            "curve_input_sol": curve_input,
            "entry_cost_sol": entry_cost,
            "proceeds_sol": proceeds,
            "pnl_sol": pnl,
            "win": pnl > 0,
            "exit_reason": reason,
            "exit_transactions": exit_count,
            "output_ratio": output_ratio,
            "create_to_fill_price_multiple": price_multiple,
            "costs_sol": {
                "axiom_entry": axiom_entry,
                "axiom_exit": axiom_exit,
                "pump_entry": pump_entry,
                "pump_exit": pump_exit,
                "base_priority_bribe_entry": costs.buy_fixed,
                "base_priority_exit": exit_fixed,
                "total_axiom": axiom_entry + axiom_exit,
                "total_pump": pump_entry + pump_exit,
                "total_fixed_execution": costs.buy_fixed + exit_fixed,
            },
        },
        None,
    )


def empty_state(model: Mapping[str, Any], model_hash: str) -> dict[str, Any]:
    bankroll = finite(model["position_sizing"]["starting_bankroll_sol"])
    return {
        "schema_version": SCHEMA_VERSION,
        "model_sha256": model_hash,
        "status": "COLLECTING_UNTOUCHED_FORWARD_TRADES",
        "social_evidence_mode": (
            "immutable launch metadata proves the status existed before CREATE; "
            "live X transport is not claimed"
        ),
        "real_money_execution": False,
        "starting_bankroll_sol": bankroll,
        "ending_bankroll_sol": bankroll,
        "processed_runs": [],
        "ledger": [],
        "rejections": [],
        "audit_counts": {},
        "completion": {"required_closed_trades": 50, "reached": False},
        "golden_thesis_approved": False,
        "production_authorised": False,
        "production_paths_changed": 0,
    }


def settle(active: list[dict[str, Any]], cash: float, cutoff_ns: int) -> tuple[list[dict[str, Any]], float]:
    remaining = []
    for trade in active:
        if integer(trade["exit_ns"]) <= cutoff_ns:
            cash += finite(trade["proceeds_sol"])
        else:
            remaining.append(trade)
    return remaining, cash


def metrics(state: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    ledger = list(state["ledger"])
    pnls = [finite(row["pnl_sol"]) for row in ledger]
    wins = sum(value > 0 for value in pnls)
    profits = sum(value for value in pnls if value > 0)
    losses = abs(sum(value for value in pnls if value <= 0))
    starting = finite(state["starting_bankroll_sol"])
    equity = starting
    peak = starting
    drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    result = {
        "closed_trades": len(ledger),
        "wins": wins,
        "losses": len(ledger) - wins,
        "win_rate": wins / max(len(ledger), 1),
        "wilson_lower_bound": wilson_lower(wins, len(ledger)),
        "net_pnl_sol": sum(pnls),
        "ending_bankroll_sol": starting + sum(pnls),
        "profit_factor": profits / max(losses, 1e-12),
        "maximum_closed_equity_drawdown_fraction": drawdown / max(starting, 1e-12),
        "capture_windows": len({str(row["run_id"]) for row in ledger}),
        "total_axiom_fees_sol": sum(
            finite(row["costs_sol"]["total_axiom"]) for row in ledger
        ),
        "total_pump_fees_sol": sum(
            finite(row["costs_sol"]["total_pump"]) for row in ledger
        ),
        "total_fixed_execution_costs_sol": sum(
            finite(row["costs_sol"]["total_fixed_execution"]) for row in ledger
        ),
    }
    gate = model["acceptance_gate"]
    result["acceptance_gate_passed"] = bool(
        result["closed_trades"] >= integer(gate["closed_trades"])
        and result["win_rate"] >= finite(gate["minimum_win_rate"])
        and result["wilson_lower_bound"] >= finite(gate["minimum_wilson_lower_bound"])
        and result["profit_factor"] >= finite(gate["minimum_profit_factor"])
        and result["net_pnl_sol"] > finite(gate["minimum_net_pnl_sol"])
        and result["maximum_closed_equity_drawdown_fraction"]
        <= finite(gate["maximum_drawdown_fraction"])
        and result["capture_windows"] >= integer(gate["minimum_capture_windows"])
    )
    return result


def render_report(state: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    result = state["metrics"]
    costs = model["execution_costs"]
    lines = [
        "# E4 V12 pre-armed Axiom-costed paper-live test",
        "",
        f"Status: **{state['status']}**",
        "",
        "This is an untouched forward paper test. It does not place real trades or modify production V12.",
        "",
        "## Progress",
        "",
        f"- Closed trades: {result['closed_trades']} / {model['acceptance_gate']['closed_trades']}",
        f"- Wins: {result['wins']}",
        f"- Win rate: {result['win_rate']:.2%}",
        f"- Wilson lower bound: {result['wilson_lower_bound']:.2%}",
        f"- Net PnL: {result['net_pnl_sol']:+.9f} SOL",
        f"- Ending bankroll: {result['ending_bankroll_sol']:.9f} SOL",
        f"- Profit factor: {result['profit_factor']:.4f}",
        f"- Maximum closed-equity drawdown: {result['maximum_closed_equity_drawdown_fraction']:.2%}",
        f"- Capture windows: {result['capture_windows']}",
        "",
        "## Execution costs included",
        "",
        f"- Axiom net trading fee: {finite(costs['axiom_net_fee_bps_each_side']) / 100:.2f}% each side",
        f"- Pump bonding-curve fee: {finite(costs['pump_bonding_curve_fee_bps_each_side']) / 100:.2f}% each side",
        f"- Priority fee: {finite(costs['priority_fee_sol_per_transaction']):.6f} SOL per transaction",
        f"- Buy MEV bribe: {finite(costs['mev_bribe_sol_on_buy']):.6f} SOL",
        f"- Solana base fee: {finite(costs['solana_base_fee_sol_per_transaction']):.6f} SOL per transaction",
        f"- Total Axiom fees charged: {result['total_axiom_fees_sol']:.9f} SOL",
        f"- Total Pump fees charged: {result['total_pump_fees_sol']:.9f} SOL",
        f"- Total fixed execution costs charged: {result['total_fixed_execution_costs_sol']:.9f} SOL",
        "",
        "## Integrity",
        "",
        f"- Frozen model SHA-256: `{state['model_sha256']}`",
        "- The test stops at the first 50 chronological closed trades.",
        "- The selector, position sizing, exit policy, latency, and cost model cannot change mid-test.",
        "- Social timestamps come from immutable launch metadata and must predate CREATE by 0-10 seconds.",
        "- A real-time X transport/fill claim is not made by this paper test.",
        "- Production paths changed: zero.",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    model = json.loads(args.model.read_text(encoding="utf-8"))
    model_hash = stable_hash(model)
    state = (
        json.loads(args.state.read_text(encoding="utf-8"))
        if args.state.exists()
        else empty_state(model, model_hash)
    )
    if state.get("model_sha256") != model_hash:
        raise ValueError("frozen model fingerprint changed after paper-live test began")
    if bool(state.get("completion", {}).get("reached")):
        state["metrics"] = metrics(state, model)
        args.report.write_text(render_report(state, model), encoding="utf-8")
        return state

    costs = load_costs(model)
    policy = load_policy(model)
    cache = read_cache(args.metadata_cache)
    processed = {str(value) for value in state["processed_runs"]}
    cash = finite(state["ending_bankroll_sol"])
    active: list[dict[str, Any]] = []
    audit = Counter(state.get("audit_counts") or {})
    required = integer(model["acceptance_gate"]["closed_trades"])

    for value in args.pair:
        run_id, batch_path, events_path = parse_pair(value)
        if run_id in processed:
            continue
        run_data = replay.load_run(run_id, batch_path, events_path)
        traces = trace_map(run_id, events_path)
        candidates, counts = await candidates_for_run(run_data, model, cache)
        audit.update(counts)
        for candidate in candidates:
            if len(state["ledger"]) >= required:
                audit["post_completion_candidates_ignored"] += 1
                continue
            active, cash = settle(active, cash, candidate.decision_ns)
            if len(active) >= integer(model["position_sizing"]["maximum_concurrent_positions"]):
                audit["concurrency_rejected"] += 1
                continue
            budget = cash * finite(model["position_sizing"]["fraction_of_available_cash"])
            trace = traces.get(candidate.mint)
            if trace is None:
                raise ValueError(f"selected candidate {candidate.mint} has no trace")
            trade, rejection = simulate_trade(
                run_data, trace, candidate, budget, model, costs, policy
            )
            if rejection is not None:
                fee = min(cash, finite(rejection.get("fee_sol")))
                cash -= fee
                state["rejections"].append(
                    {
                        "run_id": run_id,
                        "mint": candidate.mint,
                        **rejection,
                        "fee_sol": fee,
                    }
                )
                audit[f"execution_{rejection['reason']}"] += 1
                continue
            assert trade is not None
            cash -= finite(trade["entry_cost_sol"])
            active.append(trade)
            state["ledger"].append(trade)
        active, cash = settle(active, cash, 2**63 - 1)
        if active:
            raise ValueError("paper positions remained open after captured tail")
        state["processed_runs"].append(run_id)
        processed.add(run_id)

    state["ending_bankroll_sol"] = cash
    state["audit_counts"] = dict(sorted(audit.items()))
    state["metrics"] = metrics(state, model)
    reached = state["metrics"]["closed_trades"] >= required
    state["completion"] = {
        "required_closed_trades": required,
        "reached": reached,
        "remaining": max(0, required - state["metrics"]["closed_trades"]),
    }
    if reached:
        state["status"] = (
            "PAPER_LIVE_GOLDEN_GATE_PASSED"
            if state["metrics"]["acceptance_gate_passed"]
            else "PAPER_LIVE_SAMPLE_COMPLETE_GATE_FAILED"
        )
        state["golden_thesis_approved"] = bool(
            state["metrics"]["acceptance_gate_passed"]
        )
    atomic_json(args.state, state)
    for path in args.metadata_cache:
        atomic_json(path, cache)
        break
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(state, model), encoding="utf-8")
    return state


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--pair", action="append", default=[])
    value.add_argument(
        "--model",
        type=Path,
        default=Path("research/e4-v12-prearmed-axiom-frozen-model.json"),
    )
    value.add_argument(
        "--state",
        type=Path,
        default=Path("research/e4-v12-prearmed-axiom-paper-live.json"),
    )
    value.add_argument(
        "--report",
        type=Path,
        default=Path("research/e4-v12-prearmed-axiom-paper-live.md"),
    )
    value.add_argument("--metadata-cache", action="append", type=Path, default=[])
    return value


def main() -> int:
    args = parser().parse_args()
    if not args.pair:
        raise SystemExit("at least one --pair is required")
    if not args.metadata_cache:
        args.metadata_cache = [Path("artifacts/e4-v12-paper-live-metadata-cache.json")]
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "status": result["status"],
                "completion": result["completion"],
                "metrics": result["metrics"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
