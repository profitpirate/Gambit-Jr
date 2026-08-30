#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

# Apply the exact production hardening before the stress harness is imported.
from memecoin_bot import e4_hardening_v3  # noqa: F401


def _load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_validation_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = _load_base()
core = base.core


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def load_events(path: Path) -> list[Any]:
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
                events.append(base.LiveEvent(**payload))
            except Exception as exc:
                raise RuntimeError(f"invalid live event at line {line_number}: {exc}") from exc
    events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index, item.event_id))
    return events


def group_events(events: Sequence[Any]) -> dict[str, list[Any]]:
    launches = {event.mint for event in events if event.kind == core.EventKind.CREATE.value}
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        if event.mint in launches:
            grouped[event.mint].append(event)
    for values in grouped.values():
        values.sort(key=lambda item: (item.received_ns, item.slot, item.event_index, item.event_id))
    return dict(grouped)


def candidate_fingerprint(candidate: Any) -> dict[str, Any]:
    return {
        "mint": candidate.mint,
        "entry_decision_ns": candidate.entry_decision_ns,
        "entry_fill_ns": candidate.entry_fill_ns,
        "entry_price_sol": candidate.entry_price_sol,
        "entry_fdv_usd": candidate.entry_fdv_usd,
        "score": candidate.score,
        "requested_fraction": candidate.requested_fraction,
        "exit_ns": candidate.exit_ns,
        "first_partial_fraction": candidate.first_partial_fraction,
        "failure_exit": candidate.failure_exit,
        "stale_fill": candidate.stale_fill,
        "sell_legs": [asdict(leg) for leg in candidate.sell_legs],
    }


def hash_candidates(values: Sequence[Any]) -> str:
    payload = [candidate_fingerprint(value) for value in sorted(values, key=lambda row: row.mint)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def trace_portfolio(candidates: Sequence[Any], starting_balance: float, settings: Any) -> dict[str, Any]:
    liquid = float(starting_balance)
    active: list[tuple[int, float, str]] = []
    traces: list[dict[str, Any]] = []
    peak_concurrency = 0
    negative_liquid = False
    reserve_violations = 0
    reentries = 0
    seen: set[str] = set()

    def settle(until_ns: int) -> None:
        nonlocal liquid, active
        remaining = []
        for exit_ns, proceeds, mint in active:
            if exit_ns <= until_ns:
                liquid += proceeds
            else:
                remaining.append((exit_ns, proceeds, mint))
        active = remaining

    for candidate in sorted(candidates, key=lambda item: (item.entry_fill_ns, item.mint)):
        settle(candidate.entry_fill_ns)
        if candidate.mint in seen:
            reentries += 1
        seen.add(candidate.mint)
        if len(active) >= 2:
            traces.append({"mint": candidate.mint, "status": "SKIPPED_CONCURRENCY"})
            continue
        fraction = min(candidate.requested_fraction, settings.max_position_fraction)
        estimated_order_fee = base.fee_bid(settings, liquid * fraction, candidate.score)
        deployable = max(0.0, liquid - settings.reserve_sol - estimated_order_fee)
        size = min(deployable * fraction, settings.max_position_sol)
        if size < settings.min_position_sol:
            traces.append({"mint": candidate.mint, "status": "SKIPPED_SIZE", "size_sol": size})
            continue
        buy_route_cost = base.fee_bid(settings, size, candidate.score)
        entry_cost = size + buy_route_cost
        if liquid - entry_cost < settings.reserve_sol - 1e-12:
            size = max(0.0, liquid - settings.reserve_sol - buy_route_cost)
            entry_cost = size + buy_route_cost
        if size < settings.min_position_sol:
            traces.append({"mint": candidate.mint, "status": "SKIPPED_SIZE", "size_sol": size})
            continue
        pre_liquid = liquid
        liquid -= entry_cost
        negative_liquid = negative_liquid or liquid < -1e-12
        if liquid < settings.reserve_sol - 1e-9:
            reserve_violations += 1
        tokens = size * (1.0 - base.TOTAL_PERCENT_COST) / candidate.entry_price_sol
        net_proceeds = 0.0
        for leg in candidate.sell_legs:
            raw = tokens * leg.fraction_of_original * leg.price_sol
            net = raw * (1.0 - base.TOTAL_PERCENT_COST)
            net -= base.fee_bid(
                settings,
                size * leg.fraction_of_original,
                1.0,
                leg.urgent,
            )
            net_proceeds += max(0.0, net)
        active.append((candidate.exit_ns, net_proceeds, candidate.mint))
        peak_concurrency = max(peak_concurrency, len(active))
        traces.append(
            {
                "mint": candidate.mint,
                "status": "ENTERED",
                "pre_liquid_sol": pre_liquid,
                "requested_fraction": candidate.requested_fraction,
                "applied_fraction": fraction,
                "size_sol": size,
                "buy_route_cost_sol": buy_route_cost,
                "entry_cost_sol": entry_cost,
                "post_liquid_sol": liquid,
                "expected_proceeds_sol": net_proceeds,
                "exit_ns": candidate.exit_ns,
            }
        )
    settle(2**63 - 1)
    return {
        "starting_balance_sol": starting_balance,
        "ending_balance_sol": liquid,
        "negative_liquid": negative_liquid,
        "reserve_violations": reserve_violations,
        "reentries": reentries,
        "max_concurrent_positions": peak_concurrency,
        "traces": traces,
    }


def validate_candidates(candidates: Sequence[Any], settings: Any) -> dict[str, Any]:
    failures: list[str] = []
    stale = 0
    partial_20 = 0
    partial_30 = 0
    for candidate in candidates:
        fractions = [leg.fraction_of_original for leg in candidate.sell_legs]
        total = sum(fractions)
        if abs(total - 1.0) > 1e-8:
            failures.append(f"{candidate.mint}:sell fractions sum to {total}")
        if candidate.exit_ns < candidate.entry_fill_ns:
            failures.append(f"{candidate.mint}:exit precedes entry")
        if candidate.hold_ms > settings.max_hold_ms + 2_000:
            failures.append(f"{candidate.mint}:hold {candidate.hold_ms}ms exceeds bounded horizon")
        if not candidate.sell_legs:
            failures.append(f"{candidate.mint}:no exit legs")
        if candidate.stale_fill:
            stale += 1
        if candidate.first_partial_fraction is not None:
            if abs(candidate.first_partial_fraction - 0.20) <= 0.03:
                partial_20 += 1
            elif abs(candidate.first_partial_fraction - 0.30) <= 0.03:
                partial_30 += 1
            else:
                failures.append(
                    f"{candidate.mint}:unexpected first partial {candidate.first_partial_fraction}"
                )
        for leg in candidate.sell_legs:
            if not finite(leg.price_sol) or leg.price_sol <= 0:
                failures.append(f"{candidate.mint}:invalid sell price")
            if leg.fill_ns < leg.decision_ns:
                failures.append(f"{candidate.mint}:fill precedes decision")
    return {
        "passed": not failures,
        "failures": failures,
        "stale_fill_count": stale,
        "first_partial_20pct_count": partial_20,
        "first_partial_30pct_count": partial_30,
    }


def normalized_returns(portfolio: Mapping[str, Any]) -> list[float]:
    values = []
    for position in portfolio.get("positions") or []:
        cost = float(position.get("entry_cost_sol") or 0)
        pnl = float(position.get("pnl_sol") or 0)
        if cost > 0:
            values.append(pnl / cost)
    return values


def compare(primary: Mapping[str, Any], actual: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    target = dict(baseline)
    if (actual.get("closed_positions") or 0) >= 15:
        for key in (
            "net_win_rate",
            "median_hold_ms",
            "fully_exited_within_5s_fraction",
            "fully_exited_within_10s_fraction",
            "losers_exited_within_5s_fraction",
            "reentries",
            "max_concurrent_positions",
        ):
            if actual.get(key) is not None:
                target[key] = actual[key]
    keys = (
        "net_win_rate",
        "gross_win_rate",
        "median_hold_ms",
        "fully_exited_within_5s_fraction",
        "fully_exited_within_10s_fraction",
        "losers_exited_within_5s_fraction",
        "median_entry_fdv_usd",
        "entries_below_10000_fraction",
        "reentries",
        "max_concurrent_positions",
    )
    result = {}
    for key in keys:
        bot_value = primary.get(key)
        target_value = target.get(key)
        result[key] = {
            "gambit": bot_value,
            "actual_e4": target_value,
            "difference": (
                float(bot_value) - float(target_value)
                if bot_value is not None and target_value is not None
                else None
            ),
        }
    returns = normalized_returns(primary)
    result["normalized_return"] = {
        "count": len(returns),
        "mean": statistics.fmean(returns) if returns else None,
        "median": statistics.median(returns) if returns else None,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the production E4 policy only against recorded real Pump market events"
    )
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--reference-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--latencies", default="50,100,150,200,250,300,400,500,750,1000")
    parser.add_argument("--replay-rounds", type=int, default=100)
    args = parser.parse_args()

    started = time.time()
    events = load_events(args.events)
    grouped = group_events(events)
    reference = json.loads(args.reference_report.read_text(encoding="utf-8"))
    settings = core.Settings(model_path=Path("missing-model.json"))
    latencies = [float(item.strip()) for item in args.latencies.split(",") if item.strip()]

    scenarios: dict[str, Any] = {}
    candidate_sets: dict[float, list[Any]] = {}
    for latency in latencies:
        candidates = []
        errors = []
        for mint, values in grouped.items():
            try:
                candidate = base.simulate_token(values, settings, latency)
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:
                errors.append(f"{mint}:{type(exc).__name__}:{exc}")
        candidate_sets[latency] = candidates
        portfolios = {
            str(balance): base.evaluate_portfolio(candidates, balance, settings)
            for balance in (0.3, 1.2, 5.0)
        }
        traces = {
            str(balance): trace_portfolio(candidates, balance, settings)
            for balance in (0.3, 1.2, 5.0)
        }
        scenarios[f"{int(latency)}ms"] = {
            "candidate_trades": len(candidates),
            "candidate_hash": hash_candidates(candidates),
            "candidate_validation": validate_candidates(candidates, settings),
            "simulation_errors": errors,
            "portfolios": portfolios,
            "sizing_traces": traces,
        }

    primary_latency = 250.0
    primary_candidates = candidate_sets.get(primary_latency, [])
    expected_hash = hash_candidates(primary_candidates)
    replay_hashes: list[str] = []
    replay_failures: list[str] = []
    for round_number in range(args.replay_rounds):
        values = []
        try:
            for mint, rows in grouped.items():
                candidate = base.simulate_token(rows, settings, primary_latency)
                if candidate is not None:
                    values.append(candidate)
            digest = hash_candidates(values)
            replay_hashes.append(digest)
            if digest != expected_hash:
                replay_failures.append(
                    f"round {round_number}: {digest} != expected {expected_hash}"
                )
        except Exception as exc:
            replay_failures.append(f"round {round_number}:{type(exc).__name__}:{exc}")

    primary = scenarios["250ms"]["portfolios"]["1.2"]
    baseline = (reference.get("actual_e4_observed_baseline") or {}).copy()
    fresh = reference.get("actual_e4_fresh_sample") or {}
    comparison = compare(primary, fresh, baseline)

    sizing_failures = []
    for latency, scenario in scenarios.items():
        for balance, trace in scenario["sizing_traces"].items():
            if trace["negative_liquid"]:
                sizing_failures.append(f"{latency}/{balance}:negative liquid")
            if trace["reserve_violations"]:
                sizing_failures.append(
                    f"{latency}/{balance}:reserve violations={trace['reserve_violations']}"
                )
            if trace["reentries"]:
                sizing_failures.append(f"{latency}/{balance}:reentries={trace['reentries']}")
            if trace["max_concurrent_positions"] > 2:
                sizing_failures.append(
                    f"{latency}/{balance}:concurrency={trace['max_concurrent_positions']}"
                )
            official = scenario["portfolios"][balance]
            official_sizes = [row["size_sol"] for row in official.get("positions") or []]
            traced_sizes = [
                row["size_sol"]
                for row in trace["traces"]
                if row.get("status") == "ENTERED"
            ]
            if len(official_sizes) != len(traced_sizes) or any(
                abs(left - right) > 1e-10
                for left, right in zip(official_sizes, traced_sizes)
            ):
                sizing_failures.append(f"{latency}/{balance}:size trace mismatch")

    mechanical_pass = (
        not replay_failures
        and len(set(replay_hashes)) <= 1
        and not sizing_failures
        and all(
            not scenario["simulation_errors"]
            and scenario["candidate_validation"]["passed"]
            for scenario in scenarios.values()
        )
    )
    sample_sufficient = primary.get("closed_positions", 0) >= 15
    performance_comparable = (
        sample_sufficient
        and (primary.get("net_win_rate") or 0) >= max(
            0.55,
            float(fresh.get("net_win_rate") or 0.65) - 0.10,
        )
        and (primary.get("net_pnl_sol") or 0) > 0
        and (primary.get("fully_exited_within_10s_fraction") or 0) >= 0.80
        and (primary.get("losers_exited_within_5s_fraction") or 0) >= 0.80
    )

    report = {
        "report_version": "e4-live-data-validation-v1",
        "generated_at_epoch": time.time(),
        "mode": "HYPOTHETICAL_REPLAY_OF_REAL_LIVE_MARKET_EVENTS",
        "synthetic_coins_used": False,
        "synthetic_price_paths_used": False,
        "mainnet_transactions_sent": 0,
        "source": {
            "events_file": str(args.events),
            "event_count": len(events),
            "real_launch_count": len(grouped),
            "capture_summary": reference.get("capture"),
        },
        "production_policy": {
            "module": "memecoin_bot.e4_hardening_v3",
            "max_entries_per_mint": 1,
            "max_concurrent_positions": 2,
        },
        "latencies_ms": latencies,
        "scenarios": scenarios,
        "repeated_replay": {
            "rounds": args.replay_rounds,
            "expected_hash": expected_hash,
            "unique_hashes": sorted(set(replay_hashes)),
            "failures": replay_failures,
            "passed": not replay_failures and len(set(replay_hashes)) <= 1,
        },
        "sizing_audit": {
            "passed": not sizing_failures,
            "failures": sizing_failures,
        },
        "actual_e4_fresh_sample": fresh,
        "actual_e4_observed_baseline": baseline,
        "primary_comparison": {
            "latency_ms": 250,
            "starting_balance_sol": 1.2,
            "gambit": primary,
            "comparison": comparison,
        },
        "verdict": {
            "mechanically_deterministic": mechanical_pass,
            "sample_sufficient_for_profitability_claim": sample_sufficient,
            "performance_comparable_to_e4": performance_comparable,
            "good_to_go_live": mechanical_pass and performance_comparable,
            "classification": (
                "GOOD_TO_GO_LIVE"
                if mechanical_pass and performance_comparable
                else "MECHANICALLY_STABLE_BUT_MORE_LIVE_TRADES_REQUIRED"
                if mechanical_pass
                else "NOT_READY_MECHANICAL_FAILURE"
            ),
        },
        "elapsed_seconds": time.time() - started,
        "limitations": [
            "All coins and prices are real captured Pump/Solana market events; fills are counterfactual at the next observed real trade after each latency.",
            "This validates policy, sizing and exit mechanics without risking funds, but it cannot prove private-route mainnet landing performance.",
            "A small number of qualifying entries is not enough to certify profitability even when deterministic execution passes.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "real_launches": len(grouped),
                "primary_closed_positions": primary.get("closed_positions"),
                "primary_net_pnl_sol": primary.get("net_pnl_sol"),
                "mechanical_pass": mechanical_pass,
                "classification": report["verdict"]["classification"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
