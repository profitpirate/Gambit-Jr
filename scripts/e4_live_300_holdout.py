#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
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

from memecoin_bot import e4_hardening_v6  # noqa: F401
from memecoin_bot import e4_v6_state as v6

core = e4_hardening_v6.core


def load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_300_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = load_base()
E4_NET_WIN_TARGET = 0.6008
E4_NET_PROFIT_FACTOR_TARGET = 4.92
E4_MEDIAN_HOLD_MS = 3_500.0


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    x = (len(ordered) - 1) * q
    lo, hi = math.floor(x), math.ceil(x)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - x) + ordered[hi] * (x - lo)


def summary(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(clean),
        "median": statistics.median(clean) if clean else None,
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "p99": percentile(clean, 0.99),
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
        "mean": statistics.fmean(clean) if clean else None,
    }


def dedupe(events: Sequence[Any]) -> list[Any]:
    seen: set[tuple[str, int, str]] = set()
    result = []
    for item in sorted(events, key=lambda value: (value.received_ns, value.slot, value.event_index)):
        key = (item.signature, int(item.event_index), str(item.kind))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    for index, item in enumerate(result, start=1):
        item.event_id = index
    return result


async def capture_exact_300(
    chunk_seconds: float,
    post_seconds: float,
    max_seconds: float,
) -> tuple[list[Any], list[str], dict[str, Any]]:
    started = time.monotonic()
    events: list[Any] = []
    errors: list[str] = []
    connections = messages = 0
    cohort: list[str] = []
    cohort_set: set[str] = set()
    reached_at_ns: int | None = None

    while len(cohort) < 300 and time.monotonic() - started < max_seconds:
        remaining = max_seconds - (time.monotonic() - started)
        seconds = min(chunk_seconds, remaining)
        if seconds <= 0:
            break
        batch, status = await base.capture_native_pump(seconds, base.DEFAULT_WS_RPCS)
        events.extend(batch)
        errors.extend(status.get("errors") or [])
        connections += int(status.get("connections") or 0)
        messages += int(status.get("messages") or 0)
        all_events = dedupe(events)
        for item in all_events:
            if item.kind != core.EventKind.CREATE.value or item.mint in cohort_set:
                continue
            cohort.append(item.mint)
            cohort_set.add(item.mint)
            if len(cohort) == 300:
                reached_at_ns = item.received_ns
                break
        events = all_events

    if len(cohort) < 300:
        return events, cohort, {
            "complete": False,
            "unique_launches": len(cohort),
            "reason": "capture deadline reached before 300 real creates",
            "connections": connections,
            "messages": messages,
            "errors": errors[-30:],
        }

    # Continue observing real trading so the 300th launch receives the same
    # exit horizon as the first. No generated candles or synthetic events are
    # introduced here.
    post, status = await base.capture_native_pump(post_seconds, base.DEFAULT_WS_RPCS)
    events.extend(post)
    errors.extend(status.get("errors") or [])
    connections += int(status.get("connections") or 0)
    messages += int(status.get("messages") or 0)
    events = dedupe(events)
    cohort = cohort[:300]
    cohort_set = set(cohort)
    cohort_events = [item for item in events if item.mint in cohort_set]
    create_times = {
        item.mint: item.received_ns
        for item in cohort_events
        if item.kind == core.EventKind.CREATE.value
    }
    return cohort_events, cohort, {
        "complete": True,
        "unique_launches": len(cohort),
        "cohort_reached_ns": reached_at_ns,
        "duration_seconds": time.monotonic() - started,
        "connections": connections,
        "messages": messages,
        "events": len(cohort_events),
        "trade_events": sum(item.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value} for item in cohort_events),
        "first_create_ns": min(create_times.values()) if create_times else None,
        "last_create_ns": max(create_times.values()) if create_times else None,
        "errors": errors[-30:],
    }


def fill_at(events: Sequence[Any], timestamp_ns: int, start: int) -> tuple[int, Any] | None:
    for index in range(start, len(events)):
        if events[index].received_ns >= timestamp_ns and events[index].price_sol and events[index].price_sol > 0:
            return index, events[index]
    return None


def simulate_real_path(events: Sequence[Any], settings: Any, latency_ms: float, capture_end_ns: int):
    if not events:
        return None
    state = core.TokenState(events[0].mint)
    policy = core.E4Policy(settings)
    entry_index = None
    score = fraction = 0.0
    decision_ns = 0
    for index, item in enumerate(events):
        state.apply(item.to_core(), None)
        if item.kind not in {core.EventKind.CREATE.value, core.EventKind.BUY.value, core.EventKind.CURVE.value}:
            continue
        accepted, candidate_score, candidate_fraction, _, _ = policy.entry(state)
        if accepted:
            entry_index, score, fraction, decision_ns = index, candidate_score, candidate_fraction, item.received_ns
            break
    if entry_index is None:
        return None
    fill = fill_at(events, decision_ns + int(latency_ms * 1_000_000), entry_index)
    if fill is None:
        return None
    fill_index, fill_event = fill
    entry_price = float(fill_event.price_sol or 0)
    if entry_price <= 0:
        return None

    position = core.Position(
        position_id=f"holdout:{events[0].mint}", mint=events[0].mint,
        status=core.PositionStatus.OPEN, opened_ns=time.time_ns(), entry_sol=1.0,
        tokens=1.0 / entry_price, remaining=1.0 / entry_price,
        entry_price=entry_price, max_price=entry_price, last_price=entry_price,
        entry_signature="hypothesis-only",
    )
    remaining = 1.0
    legs = []
    first_partial = None
    failure = False
    stale = False
    confirmation_due = fill_event.received_ns + settings.failure_window_ms * 1_000_000
    horizon_due = fill_event.received_ns + settings.max_hold_ms * 1_000_000
    index = fill_index + 1

    def append_sell(decision: int, sell_event: Any, fraction_now: float, reason: str, urgent: bool) -> None:
        nonlocal remaining, first_partial, failure
        original = remaining if fraction_now >= 0.999 else remaining * fraction_now
        original = min(remaining, max(0.0, original))
        if original <= 0:
            return
        legs.append(base.SellLeg(
            decision_ns=decision, fill_ns=sell_event.received_ns,
            fraction_of_original=original, price_sol=float(sell_event.price_sol or position.last_price),
            reason=reason, urgent=urgent,
        ))
        remaining = max(0.0, remaining - original)
        if first_partial is None and fraction_now < 0.999:
            first_partial = original
            position.first_partial_done = True
            position.first_partial_fraction = original
        if "failure" in reason.lower() or "confirmation" in reason.lower():
            failure = True

    while index < len(events) and remaining > 1e-9:
        item = events[index]
        # Independent five-second confirmation deadline: if no market event woke
        # the policy in time, use the next *actual observed* priced event as the
        # hypothetical fill. If none exists, the trade is marked stale and is
        # excluded from performance certification.
        if not position.first_partial_done and confirmation_due <= item.received_ns:
            resolved = fill_at(events, confirmation_due + int(latency_ms * 1_000_000), index)
            if resolved is None:
                stale = True
                break
            sell_index, sell_event = resolved
            append_sell(confirmation_due, sell_event, 1.0, "E4 independent confirmation-window liquidation", True)
            index = sell_index + 1
            break
        if horizon_due <= item.received_ns:
            resolved = fill_at(events, horizon_due + int(latency_ms * 1_000_000), index)
            if resolved is None:
                stale = True
                break
            sell_index, sell_event = resolved
            append_sell(horizon_due, sell_event, 1.0, "E4 runner emergency horizon", True)
            index = sell_index + 1
            break

        state.apply(item.to_core(), None)
        elapsed = max(0, item.received_ns - fill_event.received_ns)
        position.opened_ns = time.time_ns() - elapsed
        action, sell_fraction, reason = policy.exit(position, state)
        if not action.startswith("SELL"):
            index += 1
            continue
        resolved = fill_at(events, item.received_ns + int(latency_ms * 1_000_000), index)
        if resolved is None:
            stale = True
            break
        sell_index, sell_event = resolved
        price = float(sell_event.price_sol or position.last_price)
        position.last_price = price
        position.max_price = max(position.max_price, price)
        urgent = sell_fraction >= 0.999 or any(word in reason.lower() for word in ("failure", "broke", "horizon", "liquidation"))
        append_sell(item.received_ns, sell_event, sell_fraction, reason, urgent)
        index = sell_index + 1

    if remaining > 1e-9 and not stale:
        timer = confirmation_due if not position.first_partial_done else horizon_due
        if timer <= capture_end_ns:
            resolved = fill_at(events, timer + int(latency_ms * 1_000_000), max(fill_index, index - 1))
            if resolved is None:
                stale = True
            else:
                _, sell_event = resolved
                append_sell(timer, sell_event, 1.0,
                            "E4 independent confirmation-window liquidation" if not position.first_partial_done else "E4 runner emergency horizon", True)

    if not legs or remaining > 1e-9 or stale:
        return base.CandidateTrade(
            mint=events[0].mint, entry_decision_ns=decision_ns,
            entry_fill_ns=fill_event.received_ns, entry_price_sol=entry_price,
            entry_fdv_usd=float(fill_event.fdv_usd or state.fdv_usd or 0.0),
            score=score, requested_fraction=fraction, sell_legs=legs,
            exit_ns=max([leg.fill_ns for leg in legs] or [fill_event.received_ns]),
            first_partial_fraction=first_partial, failure_exit=failure, stale_fill=True,
        )
    return base.CandidateTrade(
        mint=events[0].mint, entry_decision_ns=decision_ns,
        entry_fill_ns=fill_event.received_ns, entry_price_sol=entry_price,
        entry_fdv_usd=float(fill_event.fdv_usd or state.fdv_usd or 0.0),
        score=score, requested_fraction=fraction, sell_legs=legs,
        exit_ns=max(leg.fill_ns for leg in legs), first_partial_fraction=first_partial,
        failure_exit=failure, stale_fill=False,
    )


def buy_fee(settings: Any, amount: float, score: float) -> float:
    total = min(settings.max_execution_cost_sol, max(0.0, amount) * (0.035 + 0.030 * max(0.0, min(score, 1.0))))
    return min(settings.max_priority_fee_sol, total * 0.55) + min(settings.max_tip_sol, max(0.0, total - total * 0.55))


def sell_fee(settings: Any, urgent: bool) -> float:
    return min(settings.max_priority_fee_sol, 0.00050 if urgent else 0.00030) + min(settings.max_tip_sol, 0.00100 if urgent else 0.00020)


def evaluate_v6(candidates: Sequence[Any], starting_balance: float, settings: Any) -> dict[str, Any]:
    liquid = starting_balance
    active: list[tuple[int, float, dict[str, Any]]] = []
    completed: list[dict[str, Any]] = []
    skipped_concurrency = skipped_size = 0
    max_concurrent = 0

    def settle(until: int) -> None:
        nonlocal liquid, active
        keep = []
        for exit_ns, proceeds, row in active:
            if exit_ns <= until:
                liquid += proceeds
                completed.append(row)
            else:
                keep.append((exit_ns, proceeds, row))
        active = keep

    # Local SDK removes the old remote-builder service dependency. Keep a
    # conservative 1.5% protocol+impact allowance on each side in addition to
    # the observed E4-style transaction fee policy.
    swap_drag = 0.015
    for candidate in sorted(candidates, key=lambda value: (value.entry_fill_ns, value.mint)):
        if candidate.stale_fill:
            continue
        settle(candidate.entry_fill_ns)
        if len(active) >= 2:
            skipped_concurrency += 1
            continue
        fraction = min(candidate.requested_fraction, settings.max_position_fraction, v6.MAX_POSITION_FRACTION)
        intended = min(max(0.0, liquid - settings.reserve_sol) * fraction, settings.max_position_sol)
        if intended < settings.min_position_sol:
            skipped_size += 1
            continue
        fee = buy_fee(settings, intended, candidate.score)
        size = min(intended, max(0.0, liquid - settings.reserve_sol - fee))
        if size < settings.min_position_sol:
            skipped_size += 1
            continue
        entry_cost = size + fee
        liquid -= entry_cost
        tokens = size * (1.0 - swap_drag) / candidate.entry_price_sol
        proceeds = gross = 0.0
        for leg in candidate.sell_legs:
            raw = tokens * leg.fraction_of_original * leg.price_sol
            gross += raw
            proceeds += max(0.0, raw * (1.0 - swap_drag) - sell_fee(settings, leg.urgent))
        pnl = proceeds - entry_cost
        row = {
            "mint": candidate.mint, "size_sol": size, "wallet_fraction": size / max(1e-12, liquid + entry_cost),
            "score": candidate.score, "entry_cost_sol": entry_cost,
            "proceeds_sol": proceeds, "pnl_sol": pnl,
            "gross_pnl_sol": gross - size, "hold_ms": candidate.hold_ms,
            "first_partial_fraction": candidate.first_partial_fraction,
            "failure_exit": candidate.failure_exit, "entry_fdv_usd": candidate.entry_fdv_usd,
            "entry_ns": candidate.entry_fill_ns, "exit_ns": candidate.exit_ns,
            "sell_count": len(candidate.sell_legs),
        }
        active.append((candidate.exit_ns, proceeds, row))
        max_concurrent = max(max_concurrent, len(active))
    settle(2**63 - 1)
    wins = [row for row in completed if row["pnl_sol"] > 0]
    losses = [row for row in completed if row["pnl_sol"] <= 0]
    gains = sum(row["pnl_sol"] for row in wins)
    loss = abs(sum(row["pnl_sol"] for row in losses))
    return {
        "starting_balance_sol": starting_balance,
        "ending_balance_sol": liquid,
        "net_pnl_sol": liquid - starting_balance,
        "closed_positions": len(completed),
        "net_win_rate": len(wins) / len(completed) if completed else None,
        "profit_factor": gains / loss if loss > 0 else None,
        "median_hold_ms": statistics.median([row["hold_ms"] for row in completed]) if completed else None,
        "losers_exited_within_5s_fraction": sum(row["hold_ms"] <= 5000 for row in losses) / len(losses) if losses else None,
        "fully_exited_within_10s_fraction": sum(row["hold_ms"] <= 10000 for row in completed) / len(completed) if completed else None,
        "first_partial_20pct_count": sum(row["first_partial_fraction"] is not None and abs(row["first_partial_fraction"] - 0.20) <= 0.03 for row in completed),
        "first_partial_30pct_count": sum(row["first_partial_fraction"] is not None and abs(row["first_partial_fraction"] - 0.30) <= 0.03 for row in completed),
        "median_wallet_fraction": statistics.median([row["wallet_fraction"] for row in completed]) if completed else None,
        "max_wallet_fraction": max([row["wallet_fraction"] for row in completed], default=None),
        "reentries": 0,
        "max_concurrent_positions": max_concurrent,
        "skipped_for_concurrency": skipped_concurrency,
        "skipped_for_size": skipped_size,
        "positions": completed,
    }


async def local_builder_benchmark(grouped: Mapping[str, Sequence[Any]], probes: int) -> dict[str, Any]:
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
    except ImportError as exc:
        return {"available": False, "reason": str(exc)}
    usable = []
    for mint, events in grouped.items():
        state = core.TokenState(mint)
        for item in events[:20]:
            state.apply(item.to_core(), None)
            curve = v6.curve_meta(state)
            if curve and not curve["complete"]:
                usable.append((mint, curve))
                break
    if not usable:
        return {"available": False, "reason": "no live cohort curves contained full reserve metadata"}
    keypair = Keypair()
    process = await asyncio.create_subprocess_exec(
        "node", "tools/e4-builder/daemon.mjs",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr

    async def request(payload: dict[str, Any], timeout: float = 8.0) -> tuple[dict[str, Any], float]:
        started = time.perf_counter_ns()
        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if not line:
            raise RuntimeError("builder closed stdout")
        result = json.loads(line)
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result, elapsed

    errors, wall, internal, signing, modes = [], [], [], [], []
    try:
        await request({"request_id": "warm", "side": "WARM", "metadata": {}}, 15)
        count = min(probes, len(usable))
        for index in range(count):
            mint, curve = usable[index % len(usable)]
            meta = {"curve": curve, "token_program": v6.TOKEN_2022_PROGRAM_ID, "token_decimals": 6}
            try:
                await request({"request_id": f"pre-{index}", "side": "PREFETCH", "mint": mint,
                               "public_key": str(keypair.pubkey()), "metadata": meta})
                result, elapsed = await request({
                    "request_id": f"buy-{index}", "side": "BUY", "mint": mint,
                    "public_key": str(keypair.pubkey()), "amount": 0.01,
                    "denominated_in_sol": True, "slippage_bps": 1000,
                    "priority_fee_sol": 0.0002, "tip_sol": 0.0002, "pool": "pump", "metadata": meta,
                })
                raw = base64.b64decode(result["transaction_base64"], validate=True)
                tx = VersionedTransaction.from_bytes(raw)
                t = time.perf_counter_ns()
                VersionedTransaction(tx.message, [keypair])
                signing.append((time.perf_counter_ns() - t) / 1_000_000)
                wall.append(elapsed)
                if result.get("build_ms") is not None:
                    internal.append(float(result["build_ms"]))
                modes.append(str(result.get("builder_mode")))
            except Exception as exc:
                errors.append(f"{mint}:{exc}")
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
    return {
        "available": True,
        "attempts": min(probes, len(usable)),
        "successes": len(wall),
        "success_rate": len(wall) / max(1, min(probes, len(usable))),
        "wall_latency_ms": summary(wall),
        "internal_build_ms": summary(internal),
        "signing_ms": summary(signing),
        "local_builder_fraction": sum(mode == "local_offline_pump_sdk" for mode in modes) / len(modes) if modes else 0.0,
        "modes": sorted(set(modes)),
        "errors": errors[-20:],
    }


def same_window_e4(fresh: Mapping[str, Any], cohort: set[str]) -> dict[str, Any]:
    positions = [row for row in fresh.get("positions", []) if row.get("mint") in cohort]
    return {
        "closed_positions_in_cohort": len(positions),
        "mints": [row["mint"] for row in positions],
        "net_win_rate": sum(float(row.get("pnl_sol") or 0) > 0 for row in positions) / len(positions) if positions else None,
        "net_pnl_sol": sum(float(row.get("pnl_sol") or 0) for row in positions),
        "positions": positions,
    }


def verdict(capture: Mapping[str, Any], candidates: Sequence[Any], portfolio: Mapping[str, Any], builder: Mapping[str, Any], overlap: Mapping[str, Any]) -> dict[str, Any]:
    selected = {candidate.mint for candidate in candidates if not candidate.stale_fill}
    actual = set(overlap.get("mints") or [])
    intersection = selected & actual
    gates = {
        "exact_300_real_launches": capture.get("complete") and capture.get("unique_launches") == 300,
        "real_trade_coverage": (capture.get("trade_events") or 0) >= 1000,
        "no_reentry": portfolio.get("reentries") == 0,
        "max_two_positions": (portfolio.get("max_concurrent_positions") or 0) <= 2,
        "all_selected_paths_observable": all(not candidate.stale_fill for candidate in candidates),
        "net_win_rate_near_e4": (portfolio.get("net_win_rate") or 0) >= 0.54,
        "positive_net_expectancy": (portfolio.get("net_pnl_sol") or 0) > 0,
        "profit_factor_minimum": (portfolio.get("profit_factor") or 0) >= 2.0,
        "fast_loser_exit": (portfolio.get("losers_exited_within_5s_fraction") or 0) >= 0.90,
        "fast_total_exit": (portfolio.get("fully_exited_within_10s_fraction") or 0) >= 0.80,
        "local_builder_only": (builder.get("local_builder_fraction") or 0) >= 0.999,
        "local_builder_success": (builder.get("success_rate") or 0) >= 0.99,
        "internal_build_p95_le_36ms": ((builder.get("internal_build_ms") or {}).get("p95") or 9e9) <= 36.0,
    }
    if len(actual) >= 3:
        gates["nonzero_e4_selection_overlap"] = bool(intersection)
    failed = [name for name, passed in gates.items() if not passed]
    return {
        "good_to_go_live": not failed,
        "classification": "GOOD_TO_GO_LIVE" if not failed else "NOT_YET_LIVE_CERTIFIED",
        "failed_gates": failed,
        "gates": gates,
        "selection": {
            "gambit_selected": len(selected),
            "actual_e4_selected_in_cohort": len(actual),
            "overlap": len(intersection),
            "precision_vs_e4": len(intersection) / len(selected) if selected else None,
            "recall_vs_e4": len(intersection) / len(actual) if actual else None,
            "comparison_power": "ADEQUATE" if len(actual) >= 3 else "UNDERPOWERED",
        },
    }


async def main_async(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    events, cohort_list, capture = await capture_exact_300(args.chunk_seconds, args.post_seconds, args.max_capture_seconds)
    raw_path = output.with_name(output.stem + "-events.jsonl")
    raw_path.write_text("\n".join(json.dumps(asdict(item), separators=(",", ":"), default=str) for item in events) + "\n", encoding="utf-8")
    if len(cohort_list) != 300:
        report = {"capture": capture, "verdict": {"good_to_go_live": False, "classification": "CAPTURE_INCOMPLETE"}}
        output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return 2

    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in events:
        grouped[item.mint].append(item)
    for values in grouped.values():
        values.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))
    capture_end_ns = max(item.received_ns for item in events)
    settings = core.Settings(model_path=Path("missing-e4-v6.json"), max_position_fraction=v6.MAX_POSITION_FRACTION, max_hold_ms=v6.RUNNER_EMERGENCY_HORIZON_MS)

    latency_results: dict[str, Any] = {}
    primary_candidates = []
    for latency in (25.0, 35.0, 50.0, 100.0, 250.0):
        v6.POLICY_BY_MINT.clear()
        candidates = [simulate_real_path(grouped[mint], settings, latency, capture_end_ns) for mint in cohort_list]
        candidates = [candidate for candidate in candidates if candidate is not None]
        usable = [candidate for candidate in candidates if not candidate.stale_fill]
        portfolio = evaluate_v6(usable, args.balance, settings)
        latency_results[str(int(latency))] = {
            "candidates": len(candidates), "usable_candidates": len(usable),
            "stale_candidates": sum(candidate.stale_fill for candidate in candidates),
            "portfolio": portfolio,
        }
        if latency == 35.0:
            primary_candidates = candidates

    primary = latency_results["35"]["portfolio"]
    builder = await local_builder_benchmark(grouped, args.builder_probes)
    async with base.RpcPool(base.DEFAULT_HTTP_RPCS, timeout=8.0) as rpc:
        fresh = await base.fetch_e4_wallet_sample(rpc, args.e4_signatures)
    overlap = same_window_e4(fresh, set(cohort_list)) if fresh.get("available") else {"closed_positions_in_cohort": 0, "mints": [], "reason": fresh.get("reason")}
    identity_coverage = {
        "known_creators": sum(mint in grouped and grouped[mint] and grouped[mint][0].creator in v6.CREATOR_PROFILES for mint in cohort_list),
        "profile_cache_size": len(v6.CREATOR_PROFILES),
        "funder_cache_size": len(v6.FUNDER_BY_CREATOR),
    }
    result = {
        "hypothesis_only": True,
        "real_market_only": True,
        "funded_orders_sent": 0,
        "capture": capture,
        "cohort": {"count": len(cohort_list), "mints": cohort_list},
        "identity_cache": identity_coverage,
        "latency_sensitivity": latency_results,
        "primary_35ms": latency_results["35"],
        "local_builder": builder,
        "fresh_actual_e4": fresh,
        "same_window_actual_e4": overlap,
        "actual_e4_targets": {
            "net_win_rate": E4_NET_WIN_TARGET,
            "net_profit_factor": E4_NET_PROFIT_FACTOR_TARGET,
            "median_hold_ms": E4_MEDIAN_HOLD_MS,
        },
    }
    result["verdict"] = verdict(capture, primary_candidates, primary, builder, overlap)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    md = output.with_suffix(".md")
    md.write_text(
        "# E4 V6 — exact 300 real Pump launch holdout\n\n"
        f"- Real launches: **{capture.get('unique_launches')}**\n"
        f"- Real trade events: **{capture.get('trade_events')}**\n"
        f"- Gambit candidates at 35ms: **{latency_results['35']['candidates']}**\n"
        f"- Closed positions at 35ms / {args.balance} SOL bankroll: **{primary.get('closed_positions')}**\n"
        f"- Net win rate: **{primary.get('net_win_rate')}** (E4 exact target {E4_NET_WIN_TARGET})\n"
        f"- Net P&L: **{primary.get('net_pnl_sol')} SOL**\n"
        f"- Profit factor: **{primary.get('profit_factor')}** (E4 exact target {E4_NET_PROFIT_FACTOR_TARGET})\n"
        f"- Max concurrent positions: **{primary.get('max_concurrent_positions')}**\n"
        f"- Internal local build p95: **{(builder.get('internal_build_ms') or {}).get('p95')} ms**\n"
        f"- Actual E4 cohort trades: **{overlap.get('closed_positions_in_cohort')}**\n"
        f"- Selection overlap: **{result['verdict']['selection']['overlap']}**\n"
        f"- Verdict: **{result['verdict']['classification']}**\n"
        f"- Failed gates: {', '.join(result['verdict']['failed_gates']) or 'none'}\n\n"
        "Every price/fill observation in this report comes from the captured live Pump stream; no synthetic token or generated candle was used.\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(output), "markdown": str(md), "events": str(raw_path),
        "verdict": result["verdict"], "primary": {k: primary.get(k) for k in ("closed_positions", "net_win_rate", "net_pnl_sol", "profit_factor")},
        "builder_p95_ms": (builder.get("internal_build_ms") or {}).get("p95"),
    }, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="E4 V6 exact 300-launch live Pump holdout")
    p.add_argument("--chunk-seconds", type=float, default=60.0)
    p.add_argument("--post-seconds", type=float, default=320.0)
    p.add_argument("--max-capture-seconds", type=float, default=1800.0)
    p.add_argument("--builder-probes", type=int, default=40)
    p.add_argument("--e4-signatures", type=int, default=250)
    p.add_argument("--balance", type=float, default=1.2)
    p.add_argument("--output", default="artifacts/e4-v6-300-holdout.json")
    return p


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parser().parse_args())))
