#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

# Apply the exact production policy/lifecycle/builder integration before loading
# the shared real-market capture and accounting helpers.
from memecoin_bot import e4_hardening_v7

core = e4_hardening_v7.core
v6 = e4_hardening_v7.v6


def _load_base():
    path = Path(__file__).with_name("e4_live_market_stress.py")
    name = "e4_live_market_stress_v7_base"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BASE = _load_base()


def _state_hint(events: Sequence[Any]) -> dict[str, Any] | None:
    create = next((event for event in events if event.kind == core.EventKind.CREATE.value), None)
    if create is None:
        return None
    create_raw = dict(create.raw or {})
    reserve_raw = create_raw
    for event in events:
        raw = dict(event.raw or {})
        if all(
            raw.get(key) is not None
            for key in (
                "virtual_token_reserves",
                "virtual_sol_reserves",
                "real_token_reserves",
                "real_sol_reserves",
            )
        ) and not bool(raw.get("complete")):
            reserve_raw = raw
    required = {
        "virtual_token_reserves": reserve_raw.get("virtual_token_reserves"),
        "virtual_sol_reserves": reserve_raw.get("virtual_sol_reserves"),
        "real_token_reserves": reserve_raw.get("real_token_reserves"),
        "real_sol_reserves": reserve_raw.get("real_sol_reserves"),
        "token_total_supply": (
            reserve_raw.get("token_total_supply")
            or create_raw.get("token_total_supply")
            or 1_000_000_000_000_000
        ),
        "creator": create_raw.get("creator") or create.creator,
    }
    if any(value in (None, "") for value in required.values()):
        return None
    return {
        **{key: str(value) for key, value in required.items()},
        "token_program": create_raw.get("token_program") or "token2022",
        "mayhem_mode": bool(create_raw.get("is_mayhem_mode")),
        "cashback": bool(
            create_raw.get("is_cashback_enabled")
            or create_raw.get("is_cashback_coin")
        ),
        "complete": False,
        "user_ata_exists": False,
    }


def _apply_range(
    state: Any,
    events: Sequence[Any],
    first: int,
    last: int,
) -> None:
    if last < first:
        return
    for index in range(first, last + 1):
        state.apply(events[index].to_core(), None)


def _next_fill(
    events: Sequence[Any],
    target_ns: int,
    start: int,
) -> tuple[int | None, Any | None]:
    result = BASE.event_at_or_after(events, target_ns, start)
    if result is not None:
        return result
    fallback = next(
        (event for event in reversed(events[: max(start, 1)]) if event.price_sol),
        None,
    )
    return None, fallback


def simulate_token_v7(
    events: Sequence[Any],
    settings: Any,
    latency_ms: float,
) -> Any | None:
    """Counterfactual trade replay over only real captured market events.

    The sole counterfactual is whether Gambit entered. Prices, ordering and flow
    are the observed Pump events. Independent five-second and runner watchdogs
    use the last real curve price when the token becomes quiet.
    """

    if not events:
        return None
    events = sorted(events, key=lambda item: (item.received_ns, item.slot, item.event_index))
    state = core.TokenState(events[0].mint)
    policy = core.E4Policy(settings)
    entry_index = None
    score = 0.0
    requested_fraction = 0.0
    entry_decision_ns = 0
    for index, event in enumerate(events):
        state.apply(event.to_core(), None)
        if event.kind not in {
            core.EventKind.CREATE.value,
            core.EventKind.BUY.value,
            core.EventKind.CURVE.value,
        }:
            continue
        accepted, candidate_score, fraction, _, _ = policy.entry(state)
        if accepted:
            entry_index = index
            score = candidate_score
            requested_fraction = fraction
            entry_decision_ns = event.received_ns
            break
    if entry_index is None:
        return None

    target = entry_decision_ns + int(latency_ms * 1_000_000)
    fill_index, fill_event = _next_fill(events, target, entry_index)
    if fill_event is None or not fill_event.price_sol or fill_event.price_sol <= 0:
        return None
    if fill_index is not None and fill_index > entry_index:
        _apply_range(state, events, entry_index + 1, fill_index)
    fill_index = entry_index if fill_index is None else fill_index
    entry_price = float(fill_event.price_sol)
    opened_wall = time.time_ns()
    position = core.Position(
        position_id=f"v7:{events[0].mint}",
        mint=events[0].mint,
        status=core.PositionStatus.OPEN,
        opened_ns=opened_wall,
        entry_sol=1.0,
        tokens=1.0 / entry_price,
        remaining=1.0 / entry_price,
        entry_price=entry_price,
        max_price=entry_price,
        last_price=entry_price,
        entry_signature="v7-live-replay",
    )
    # Entry() records the actual relative confidence fraction. Preserve it for
    # the deterministic 20%/30% E4 partial family.
    v6._ENTRY_FRACTION_BY_MINT[position.mint] = requested_fraction

    latency_ns = int(latency_ms * 1_000_000)
    confirmation_deadline = fill_event.received_ns + settings.failure_window_ms * 1_000_000
    runner_deadline = fill_event.received_ns + int(
        os.getenv("E4_RUNNER_EMERGENCY_HOLD_MS", "300000")
    ) * 1_000_000
    remaining_fraction = 1.0
    legs: list[Any] = []
    first_partial = None
    failure = False
    stale = False
    cursor = fill_index + 1

    def add_leg(
        decision_ns: int,
        fraction: float,
        reason: str,
        start_index: int,
    ) -> tuple[int, bool]:
        nonlocal remaining_fraction, first_partial, failure, stale
        target_ns = decision_ns + latency_ns
        resolved_index, resolved_event = _next_fill(events, target_ns, start_index)
        if resolved_event is None:
            return start_index, False
        if resolved_index is not None:
            _apply_range(state, events, start_index, resolved_index)
            next_index = resolved_index + 1
        else:
            next_index = start_index
            stale = True
        price = float(resolved_event.price_sol or position.last_price or entry_price)
        original_fraction = (
            remaining_fraction
            if fraction >= 0.999
            else remaining_fraction * max(0.0, min(1.0, fraction))
        )
        original_fraction = min(remaining_fraction, original_fraction)
        if original_fraction <= 0:
            return next_index, False
        urgent = fraction >= 0.999 or any(
            term in reason.lower()
            for term in ("failure", "broke", "horizon", "liquidation")
        )
        legs.append(
            BASE.SellLeg(
                decision_ns=decision_ns,
                fill_ns=(resolved_event.received_ns if resolved_index is not None else target_ns),
                fraction_of_original=original_fraction,
                price_sol=price,
                reason=reason,
                urgent=urgent,
            )
        )
        remaining_fraction = max(0.0, remaining_fraction - original_fraction)
        position.remaining = position.tokens * remaining_fraction
        position.last_price = price
        position.max_price = max(position.max_price, price)
        if first_partial is None and fraction < 0.999:
            first_partial = original_fraction
            position.first_partial_done = True
            position.first_partial_fraction = original_fraction
        if "failure" in reason.lower() or "confirmation" in reason.lower():
            failure = True
        return next_index, True

    while remaining_fraction > 1e-9:
        deadline = runner_deadline if position.first_partial_done else confirmation_deadline
        next_event_ns = events[cursor].received_ns if cursor < len(events) else 2**63 - 1
        if deadline <= next_event_ns:
            reason = (
                "E4 independent runner emergency horizon"
                if position.first_partial_done
                else "E4 independent confirmation-window liquidation"
            )
            cursor, _ = add_leg(deadline, 1.0, reason, cursor)
            break
        if cursor >= len(events):
            cursor, _ = add_leg(deadline, 1.0, "E4 independent watchdog liquidation", cursor)
            break

        event = events[cursor]
        state.apply(event.to_core(), None)
        elapsed = max(0, event.received_ns - fill_event.received_ns)
        position.opened_ns = time.time_ns() - elapsed
        action, fraction, reason = policy.exit(position, state)
        if not action.startswith("SELL"):
            cursor += 1
            continue
        next_cursor, sold = add_leg(event.received_ns, fraction, reason, cursor + 1)
        if not sold:
            break
        cursor = next_cursor
        if remaining_fraction <= 1e-9 or fraction >= 0.999:
            break

        # V5/V7 production performs an immediate post-fill catch-up. Recreate
        # that here using the latest real market state already applied above.
        elapsed = max(0, (legs[-1].fill_ns - fill_event.received_ns))
        position.opened_ns = time.time_ns() - elapsed
        action, catch_fraction, catch_reason = policy.exit(position, state)
        if action.startswith("SELL"):
            next_cursor, sold = add_leg(
                legs[-1].fill_ns,
                catch_fraction,
                f"E4 post-fill catch-up: {catch_reason}",
                cursor,
            )
            if sold:
                cursor = next_cursor
                if remaining_fraction <= 1e-9 or catch_fraction >= 0.999:
                    break

    if not legs:
        return None
    return BASE.CandidateTrade(
        mint=events[0].mint,
        entry_decision_ns=entry_decision_ns,
        entry_fill_ns=fill_event.received_ns,
        entry_price_sol=entry_price,
        entry_fdv_usd=fill_event.fdv_usd or state.fdv_usd or 0.0,
        score=score,
        requested_fraction=requested_fraction,
        sell_legs=legs,
        exit_ns=max(leg.fill_ns for leg in legs),
        first_partial_fraction=first_partial,
        failure_exit=failure,
        stale_fill=stale,
    )


async def builder_benchmark_v7(
    grouped: Mapping[str, Sequence[Any]],
    probes: int,
) -> dict[str, Any]:
    usable = [
        (mint, hint)
        for mint, events in grouped.items()
        if (hint := _state_hint(events)) is not None
    ]
    if not usable or probes <= 0:
        return {"available": False, "reason": "no captured launch state hints"}
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
    except ImportError as exc:
        return {"available": False, "reason": f"solders unavailable: {exc}"}

    keypair = Keypair()
    process = await asyncio.create_subprocess_exec(
        "node",
        "tools/e4-builder/daemon.mjs",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr

    async def request(payload: Mapping[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        process.stdin.write(json.dumps(dict(payload), separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("builder closed stdout")
        response = json.loads(line)
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    errors: list[str] = []
    roundtrip: list[float] = []
    internal: list[float] = []
    signing: list[float] = []
    sizes: list[float] = []
    tip_checks: list[bool] = []
    modes: defaultdict[str, int] = defaultdict(int)
    side_results: dict[str, list[bool]] = {"BUY": [], "SELL": []}

    try:
        await request({"request_id": "ping", "action": "PING"})
        # The first prefetch warms Pump global state and recent blockhash. Every
        # mint carries exact reserves from its real launch event, so the measured
        # order path has no remote transaction-builder call.
        for index, (mint, hint) in enumerate(usable[: min(8, len(usable))]):
            await request(
                {
                    "request_id": f"prefetch-{index}",
                    "action": "PREFETCH",
                    "side": "PREFETCH",
                    "mint": mint,
                    "public_key": str(keypair.pubkey()),
                    "metadata": {"state_hint": hint},
                }
            )

        # Warm JIT/module paths without contaminating measured latency.
        for index in range(min(4, probes)):
            mint, hint = usable[index % len(usable)]
            await request(
                {
                    "request_id": f"warm-{index}",
                    "side": "BUY",
                    "mint": mint,
                    "public_key": str(keypair.pubkey()),
                    "amount": 0.01,
                    "denominated_in_sol": True,
                    "slippage_bps": 1_000,
                    "priority_fee_sol": 0.00001,
                    "tip_sol": 0.000001,
                    "pool": "pump",
                    "metadata": {"state_hint": hint},
                }
            )

        for index in range(probes):
            side = "BUY" if index % 2 == 0 else "SELL"
            mint, hint = usable[index % len(usable)]
            payload = {
                "request_id": f"v7-stress-{index}",
                "side": side,
                "mint": mint,
                "public_key": str(keypair.pubkey()),
                "amount": 0.01 if side == "BUY" else 1_000,
                "denominated_in_sol": side == "BUY",
                "slippage_bps": 1_000,
                "priority_fee_sol": 0.00001,
                "tip_sol": 0.000001,
                "pool": "pump",
                "metadata": {"state_hint": hint},
            }
            started = time.perf_counter_ns()
            try:
                response = await request(payload)
                roundtrip.append((time.perf_counter_ns() - started) / 1_000_000)
                internal.append(float(response.get("build_ms") or 0.0))
                raw = base64.b64decode(response["transaction_base64"], validate=True)
                tx = VersionedTransaction.from_bytes(raw)
                sign_started = time.perf_counter_ns()
                signed = VersionedTransaction(tx.message, [keypair])
                signing.append((time.perf_counter_ns() - sign_started) / 1_000_000)
                keys = {str(value) for value in tx.message.account_keys}
                tip_checks.append(bool(keys.intersection(BASE.JITO_TIP_ACCOUNTS)))
                sizes.append(float(len(raw)))
                modes[str(response.get("builder_mode") or "unknown")] += 1
                side_results[side].append(bool(str(signed.signatures[0])))
            except Exception as exc:
                errors.append(f"{side}:{mint}:{exc}")
                side_results[side].append(False)
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
    stderr = (await process.stderr.read()).decode(errors="replace")[-4_000:]
    total = sum(len(values) for values in side_results.values())
    successes = sum(sum(values) for values in side_results.values())
    return {
        "available": True,
        "requests": total,
        "successes": successes,
        "success_rate": BASE.safe_ratio(successes, total),
        "buy_success_rate": BASE.safe_ratio(sum(side_results["BUY"]), len(side_results["BUY"])),
        "sell_success_rate": BASE.safe_ratio(sum(side_results["SELL"]), len(side_results["SELL"])),
        "latency_ms": BASE.metric_summary(roundtrip),
        "internal_build_latency_ms": BASE.metric_summary(internal),
        "signing_latency_ms": BASE.metric_summary(signing),
        "transaction_bytes": BASE.metric_summary(sizes),
        "jito_tip_present_fraction": BASE.safe_ratio(sum(tip_checks), len(tip_checks)),
        "builder_modes": dict(modes),
        "remote_builder_calls": modes.get("remote_fallback", 0),
        "errors": errors[-30:],
        "stderr_tail": stderr,
    }


def market_gates(
    capture: Mapping[str, Any],
    builder: Mapping[str, Any],
    primary: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> dict[str, Any]:
    actual_net = float(fresh.get("net_win_rate") or 0.6008)
    actual_pf = 4.92
    checks = [
        ("real_launch_sample", capture.get("new_launches", 0) >= 300, f"launches={capture.get('new_launches')}"),
        ("real_trade_sample", capture.get("trade_events", 0) >= 2_000, f"events={capture.get('trade_events')}"),
        ("official_local_builder", builder.get("remote_builder_calls") == 0 and builder.get("success_rate") == 1.0, f"modes={builder.get('builder_modes')} success={builder.get('success_rate')}"),
        ("local_builder_speed", ((builder.get("internal_build_latency_ms") or {}).get("p95") or 9e9) <= 15.0, f"p95={((builder.get('internal_build_latency_ms') or {}).get('p95'))}ms"),
        ("simulation_sample", (primary.get("closed_positions") or 0) >= 30, f"closed={primary.get('closed_positions')}"),
        ("positive_net_expectancy", (primary.get("net_pnl_sol") or 0) > 0, f"pnl={primary.get('net_pnl_sol')}"),
        ("net_win_rate_similarity", (primary.get("net_win_rate") or 0) >= max(0.50, actual_net - 0.10), f"gambit={primary.get('net_win_rate')} e4={actual_net}"),
        ("profit_factor_similarity", (primary.get("profit_factor") or 0) >= max(2.5, actual_pf - 2.0), f"gambit={primary.get('profit_factor')} e4={actual_pf}"),
        ("fast_loser_exit", (primary.get("losers_exited_within_5s_fraction") or 0) >= 0.90, f"fraction={primary.get('losers_exited_within_5s_fraction')}"),
        ("fast_total_exit", (primary.get("fully_exited_within_10s_fraction") or 0) >= 0.80, f"fraction={primary.get('fully_exited_within_10s_fraction')}"),
        ("single_entry", primary.get("reentries") == 0, f"reentries={primary.get('reentries')}"),
        ("two_position_limit", (primary.get("max_concurrent_positions") or 0) <= 2, f"max={primary.get('max_concurrent_positions')}"),
    ]
    failed = [name for name, passed, _ in checks if not passed]
    return {
        "market_hypothesis_pass": not failed,
        "classification": "MARKET_HYPOTHESIS_PASS" if not failed else "MARKET_HYPOTHESIS_FAIL",
        "checks": [
            {"name": name, "passed": bool(passed), "detail": detail}
            for name, passed, detail in checks
        ],
        "failed": failed,
        "funded_live_certified": False,
        "funded_live_reason": "No funded mainnet transaction or authenticated production-route landing test was performed.",
    }


async def certification(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_output = output.with_name(output.stem + "-live-events.jsonl")
    started_wall = int(time.time())
    ws_urls = tuple(filter(None, os.getenv("E4_STRESS_WS_URLS", "").split(","))) or BASE.DEFAULT_WS_RPCS
    rpc_urls = tuple(filter(None, os.getenv("E4_STRESS_RPC_URLS", "").split(","))) or BASE.DEFAULT_HTTP_RPCS

    async with BASE.aiohttp.ClientSession(timeout=BASE.aiohttp.ClientTimeout(total=8)) as session:
        sol_usd = await BASE.fetch_sol_usd(session)
    BASE.hardening._SOL_USD = sol_usd

    print(json.dumps({"event": "capture_started", "seconds": args.capture_seconds, "minimum_launches": args.minimum_launches}), flush=True)
    live_events, diagnostics = await BASE.capture_native_pump(args.capture_seconds, ws_urls)
    ended_wall = int(time.time())
    if len([event for event in live_events if event.kind == core.EventKind.CREATE.value]) < args.minimum_launches:
        async with BASE.RpcPool(rpc_urls, timeout=10) as rpc:
            try:
                backfill = await BASE.backfill_pump_events(rpc, started_wall, ended_wall, 1_000)
            except Exception as exc:
                diagnostics.setdefault("errors", []).append(f"backfill:{exc}")
                backfill = []
        keys = {(event.signature, event.event_index, event.kind) for event in live_events}
        for event in backfill:
            key = (event.signature, event.event_index, event.kind)
            if key not in keys:
                event.event_id = len(live_events) + 1
                live_events.append(event)
                keys.add(key)
        live_events.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    launches = {event.mint for event in live_events if event.kind == core.EventKind.CREATE.value}
    launch_events = [event for event in live_events if event.mint in launches]
    with raw_output.open("w", encoding="utf-8") as handle:
        for event in launch_events:
            handle.write(json.dumps(asdict(event), separators=(",", ":"), default=str) + "\n")

    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in launch_events:
        grouped[event.mint].append(event)
    for values in grouped.values():
        values.sort(key=lambda item: (item.received_ns, item.slot, item.event_index))

    print(json.dumps({"event": "capture_complete", "launches": len(launches), "trade_events": sum(item.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value} for item in launch_events)}), flush=True)
    builder_task = asyncio.create_task(builder_benchmark_v7(grouped, args.builder_probes))
    route_task = asyncio.create_task(BASE.testnet_route_probe(args.testnet_route_probes))

    latency_values = [29.0, 36.0, 50.0, 100.0, 250.0]
    scenarios: dict[str, Any] = {}
    for latency in latency_values:
        candidates = [
            trade
            for values in grouped.values()
            if (trade := simulate_token_v7(values, core.Settings(model_path=Path("missing-model.json")), latency))
        ]
        scenarios[f"{int(latency)}ms"] = {
            "candidate_trades": len(candidates),
            "balances": {
                str(balance): BASE.evaluate_portfolio(
                    candidates,
                    balance,
                    core.Settings(model_path=Path("missing-model.json")),
                )
                for balance in (0.3, 1.2, 5.0)
            },
        }
        print(json.dumps({"event": "scenario_complete", "latency_ms": latency, "candidates": len(candidates)}), flush=True)

    async with BASE.RpcPool(rpc_urls, timeout=10) as oracle_rpc:
        fresh = await BASE.fetch_e4_wallet_sample(oracle_rpc, args.wallet_signatures)
        rpc_diagnostics = {
            "latency_ms": BASE.metric_summary(oracle_rpc.latencies_ms),
            "errors": oracle_rpc.errors[-30:],
        }
    builder, route = await asyncio.gather(builder_task, route_task)

    primary = scenarios["36ms"]["balances"]["1.2"]
    baseline_payload = json.loads(Path("models/e4/e4-observed-v1.json").read_text())
    comparison = BASE.compare_metrics(primary, baseline_payload["evidence"], fresh)
    capture = {
        **diagnostics,
        "source": "real Solana processed Pump logs with bounded RPC backfill",
        "sol_usd": sol_usd,
        "captured_events_total": len(live_events),
        "new_launches": len(launches),
        "new_launch_events": len(launch_events),
        "trade_events": sum(item.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value} for item in launch_events),
        "buy_events": sum(item.kind == core.EventKind.BUY.value for item in launch_events),
        "sell_events": sum(item.kind == core.EventKind.SELL.value for item in launch_events),
        "tokens_with_trajectory": sum(len(values) >= 3 for values in grouped.values()),
    }
    verdict = market_gates(capture, builder, primary, fresh)
    report = {
        "report_version": "e4-v7-300-live-launch-certification",
        "generated_at_epoch": int(time.time()),
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "branch": os.getenv("GITHUB_REF_NAME"),
        "commit": os.getenv("GITHUB_SHA"),
        "capture": capture,
        "builder_benchmark": builder,
        "testnet_same_signature_route_probe": route,
        "actual_e4_fresh_sample": fresh,
        "actual_e4_observed_baseline": baseline_payload,
        "hypothetical_scenarios": scenarios,
        "primary_comparison_scenario": {"latency_ms": 36, "starting_balance_sol": 1.2, "results": primary},
        "comparison": comparison,
        "rpc_diagnostics": rpc_diagnostics,
        "stress_iterations": args.stress_iterations,
        "verdict": verdict,
        "limitations": [
            "All launches and prices are real captured Pump events; only Gambit's decision to enter is counterfactual.",
            "No funded mainnet order was sent.",
            "The 29-36ms scenarios model event-to-fill delay; authenticated production feed and route latency must still be measured separately.",
            "Identity and prearmed entry families activate only when explicit cached evidence is configured.",
        ],
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(
        "\n".join(
            [
                "# E4 V7 — 300 real-launch certification",
                "",
                f"**Market verdict:** {verdict['classification']}",
                "**Funded-live certified:** no — this was deliberately hypothesis-only.",
                f"**Real launches:** {capture['new_launches']}",
                f"**Real market events:** {capture['trade_events']}",
                f"**36ms candidate trades:** {scenarios['36ms']['candidate_trades']}",
                f"**36ms / 1.2 SOL net win rate:** {primary.get('net_win_rate')}",
                f"**36ms / 1.2 SOL profit factor:** {primary.get('profit_factor')}",
                f"**36ms / 1.2 SOL net P&L:** {primary.get('net_pnl_sol')} SOL",
                f"**Official local builder P95:** {(builder.get('internal_build_latency_ms') or {}).get('p95')}ms",
                f"**Actual fresh E4 positions:** {fresh.get('closed_positions')}",
                "",
                "## Failed market gates",
                *(f"- {item['name']}: {item['detail']}" for item in verdict['checks'] if not item['passed']),
            ]
        ) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"event": "certification_complete", "output": str(output), "verdict": verdict["classification"], "failed": verdict["failed"]}, indent=2), flush=True)
    return 0


async def run_with_heartbeat(args: argparse.Namespace) -> int:
    task = asyncio.create_task(certification(args))
    started = time.monotonic()
    heartbeat = 0
    while not task.done():
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
        except asyncio.TimeoutError:
            heartbeat += 1
            print(json.dumps({"event": "e4_v7_certification_heartbeat", "heartbeat": heartbeat, "elapsed_seconds": round(time.monotonic() - started, 1), "capture_target_seconds": args.capture_seconds, "minimum_real_launches": args.minimum_launches}, separators=(",", ":")), flush=True)
    return await task


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="E4 V7 hypothesis-only certification on real live Pump launches")
    value.add_argument("--capture-seconds", type=float, default=1_200)
    value.add_argument("--minimum-launches", type=int, default=300)
    value.add_argument("--wallet-signatures", type=int, default=1_000)
    value.add_argument("--builder-probes", type=int, default=100)
    value.add_argument("--testnet-route-probes", type=int, default=0)
    value.add_argument("--stress-iterations", type=int, default=100_000)
    value.add_argument("--output", default="artifacts/e4-v7-300-launch-certification.json")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_with_heartbeat(parser().parse_args())))
