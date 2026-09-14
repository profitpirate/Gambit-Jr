#!/usr/bin/env python3
"""Hardened true-online runner for Golden Management v2.

Adds two lifecycle guarantees on top of golden_management_v2_live:
1. RunnerGuardian sees market-changing states once; timer-only ticks are used only
   when an initial/max-hold deadline must execute on the latest known curve state.
2. At a campaign window boundary, new entries stop first while the websocket feed
   remains live. Existing positions flatten and 20-second post-exit observation
   completes before the feed is shut down and state is persisted.

Paper execution only. No transaction signing or broadcasting exists here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Mapping

import golden_buyer_reputation_core as core
import golden_buyer_reputation_live as live_v1
import golden_management_v2 as mgmt
import golden_management_v2_live as base

VERSION = "golden-management-v2-hardened-v1"
DRAIN_SECONDS = 45.0


async def _hardened_manage_loop(self: base.ManagedPaperAccount, mint: str, latest_states: dict[str, dict[str, Any]]) -> None:
    """Process distinct market states, plus only the timer ticks that are required."""
    last_market_state_ns = -1
    while mint in self.active:
        await asyncio.sleep(0.004)
        async with self._lock:
            p = self.active.get(mint)
            if not p:
                return
            state = latest_states.get(mint)
            if not base.valid_state(state):
                if (time.time_ns() - int(p["entry_ns"])) / 1e9 > 20.0:
                    p["status"] = "UNRESOLVED_EXIT"
                    if f"{mint}:UNRESOLVED_EXIT" not in self.task_errors:
                        self.task_errors.append(f"{mint}:UNRESOLVED_EXIT")
                continue
            state_ns = int(state.get("ns", 0))
            elapsed_ms = (time.time_ns() - int(p["entry_ns"])) / 1_000_000.0
            guardian: mgmt.RunnerGuardian = p["guardian"]
            timer_due = (
                (not guardian.initial_done and elapsed_ms >= guardian.config.initial_target_ms)
                or elapsed_ms >= guardian.config.max_hold_ms
                or (p.get("control_v1_2s") is None and elapsed_ms >= base.CONTROL_HOLD_MS)
            )
            if state_ns == last_market_state_ns and not timer_due:
                continue
            if state_ns != last_market_state_ns:
                last_market_state_ns = state_ns
            await self._apply_mark_locked(p, state)


# Patch the account class before constructing a campaign.
base.ManagedPaperAccount._manage_loop = _hardened_manage_loop


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    base.atomic_json(path, payload)


async def run(args: argparse.Namespace) -> int:
    production_root = args.production_root.resolve()
    sys.path.insert(0, str(production_root / "src"))
    prod = live_v1.load_module("golden_v2_hardened_prod", production_root / "scripts" / "e4_live_market_stress.py")
    resume = base.read_resume(args.resume_state)
    rep = core.initialise_reputation_from_corpus(args.corpus)
    base.apply_delta(rep, resume.get("reputation_delta") or {})
    delta_appear = Counter({k: int(v[0]) for k, v in (resume.get("reputation_delta") or {}).items()})
    delta_wins = Counter({k: int(v[1]) for k, v in (resume.get("reputation_delta") or {}).items()})
    account = base.ManagedPaperAccount(base.STARTING_BALANCE_SOL, resume.get("account"))

    latest_states: dict[str, dict[str, Any]] = {}
    launches: dict[str, dict[str, Any]] = {}
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=30_000)
    stop = asyncio.Event(); endpoints: dict[str, Any] = {}; dedupe: set[tuple[str, int, str]] = set()
    counts: Counter = Counter(); recent: deque[dict[str, Any]] = deque(maxlen=100)
    task_errors: list[str] = []; background: set[asyncio.Task[Any]] = set()
    started = time.time(); session_started_ns = time.time_ns(); last_event = started
    accept_new = True; drain_deadline: float | None = None

    def add_task(coro: Any) -> None:
        task = asyncio.create_task(coro); background.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            background.discard(t)
            if not t.cancelled() and t.exception():
                task_errors.append(repr(t.exception()))
        task.add_done_callback(done)

    def checkpoint(terminal: str | None = None) -> None:
        account.finalize_post_exit()
        payload = base.status_payload(started, counts, account, queue, endpoints, last_event, recent, terminal)
        payload["version"] = VERSION
        payload["session_accepting_new_entries"] = accept_new
        payload["clean_drain_required"] = True
        if task_errors:
            payload["task_errors"] = list(task_errors[-20:])
        atomic_json(args.status, payload)
        delta = {w: [delta_appear[w], delta_wins[w]] for w in delta_appear}
        atomic_json(args.state_output, {
            "version": base.VERSION,
            "runner_version": VERSION,
            "rule_version": core.VERSION,
            "management_version": mgmt.VERSION,
            "paper_only": True,
            "campaign_starting_balance_sol": base.STARTING_BALANCE_SOL,
            "account": account.persistence(),
            "reputation_delta": delta,
            "updated_at": time.time(),
        })

    async def settle_reputation(mint: str, buyers: list[str], decision_state: dict[str, Any] | None) -> None:
        await asyncio.sleep(core.HOLD_MS / 1000.0)
        exit_state = latest_states.get(mint)
        if not base.valid_state(decision_state) or not base.valid_state(exit_state):
            counts["benchmark_skipped"] += 1; return
        tokens, _ = core.quote_buy(core.BENCHMARK_STAKE_SOL, decision_state)
        if tokens <= 0:
            counts["benchmark_skipped"] += 1; return
        won = core.quote_sell(tokens, exit_state) - core.BENCHMARK_STAKE_SOL > 0
        for wallet in dict.fromkeys(buyers):
            rep.appear[wallet] += 1; rep.wins[wallet] += int(won)
            delta_appear[wallet] += 1; delta_wins[wallet] += int(won)
        counts["benchmark_outcomes"] += 1; counts["benchmark_wins"] += int(won)

    async def decide(mint: str) -> None:
        launch = launches.get(mint)
        if not launch: return
        deadline_ns = int(launch["create_ns"]) + core.DECISION_MS * 1_000_000
        delay = max(0.0, (deadline_ns - time.time_ns()) / 1e9)
        if delay: await asyncio.sleep(delay)
        launch = launches.get(mint)
        if not launch: return
        create_ns = int(launch["create_ns"])
        buyers = core.unique_early_buyers(launch["events"], str(launch["creator"]), create_ns, core.DECISION_MS)
        counts["decisions"] += 1; counts["early_buyers_seen"] += len(buyers)
        qualifies, n, w, rate = rep.qualifies(buyers)
        decision_ns = time.time_ns(); state = latest_states.get(mint)
        decision = {
            "mint": mint, "create_ns": create_ns, "decision_ns": decision_ns,
            "buyers": buyers, "buyer_count": len(buyers), "prior_appearances": n,
            "prior_wins": w, "prior_win_rate": rate, "qualifies": qualifies,
            "valid_decision_state": base.valid_state(state),
        }
        recent.append({k: decision[k] for k in ("mint", "buyer_count", "prior_appearances", "prior_win_rate", "qualifies")})
        counts["qualified"] += int(qualifies)
        add_task(settle_reputation(mint, buyers, dict(state) if state else None))
        if qualifies:
            if accept_new:
                account.schedule(mint, decision, latest_states, args.execution_delay_ms)
            else:
                counts["qualified_after_entry_cutoff"] += 1

    workers = [asyncio.create_task(live_v1.endpoint_worker(url, prod, queue, stop, endpoints)) for url in dict.fromkeys(args.ws_url or prod.DEFAULT_WS_RPCS)]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass

    hard_deadline = time.monotonic() + args.duration_seconds
    last_checkpoint = 0.0
    checkpoint()
    try:
        while not stop.is_set():
            now_mono = time.monotonic()
            if accept_new and (now_mono >= hard_deadline or account.closed() >= args.target_closed_trades):
                accept_new = False
                drain_deadline = now_mono + DRAIN_SECONDS
                counts["entry_cutoff_reached"] += 1
            if not accept_new:
                clean = not account.active and not account.pending and not account.post_exit
                if clean:
                    break
                if drain_deadline is not None and now_mono >= drain_deadline:
                    task_errors.append("CLEAN_DRAIN_TIMEOUT")
                    break

            try:
                payload = await asyncio.wait_for(queue.get(), timeout=0.20)
            except asyncio.TimeoutError:
                payload = None
            if payload is not None:
                incoming: list[Any] = []; prod.decode_log_payload(payload, incoming, dedupe)
                if incoming:
                    last_event = time.time(); counts["decoded_events"] += len(incoming)
                    grouped: dict[str, list[Any]] = {}
                    for event in incoming:
                        mint = str(event.mint); grouped.setdefault(mint, []).append(event)
                        state = core.state_from_event(event)
                        if state:
                            latest_states[mint] = state
                            account.observe_state(mint, state)
                    for mint, events in grouped.items():
                        creates = [e for e in events if core.event_kind(e) == "CREATE"]
                        if creates and mint not in launches and accept_new:
                            create = creates[0]; create_ns = core.event_ns(create)
                            if create_ns < session_started_ns:
                                counts["rejected_not_fresh"] += 1; continue
                            raw = getattr(create, "raw", {}) if isinstance(getattr(create, "raw", {}), Mapping) else {}
                            if not core.native_quote(raw):
                                counts["unsupported_quote"] += 1; continue
                            launches[mint] = {"create_ns": create_ns, "creator": core.event_creator(create), "events": []}
                            counts["launches_seen"] += 1; add_task(decide(mint))
                        launch = launches.get(mint)
                        if launch:
                            cutoff = int(launch["create_ns"]) + 45_000_000_000
                            launch["events"].extend(e for e in events if core.event_ns(e) <= cutoff)

            account.finalize_post_exit()
            now_mono = time.monotonic()
            if now_mono - last_checkpoint >= args.heartbeat_seconds:
                protected = set(account.active) | set(account.pending) | set(account.post_exit)
                cutoff_ns = time.time_ns() - 120_000_000_000
                for mint in list(latest_states):
                    if mint not in protected and int(latest_states[mint].get("ns", 0)) < cutoff_ns:
                        latest_states.pop(mint, None); launches.pop(mint, None)
                checkpoint(); last_checkpoint = now_mono
                print("GOLDEN_V2_HARDENED " + json.dumps({"counts": dict(counts), "account": account.summary(), "accept_new": accept_new}, separators=(",", ":")), flush=True)
            if task_errors or account.task_errors or account.summary()["unresolved_exposure"]:
                stop.set()
    finally:
        stop.set()
        for w in workers: w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if background:
            try: await asyncio.wait_for(asyncio.gather(*list(background), return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError: pass
        if account.tasks:
            try: await asyncio.wait_for(asyncio.gather(*list(account.tasks), return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError: pass

    account.finalize_post_exit()
    if account.active or account.pending:
        terminal = "TECHNICAL_FAILURE_ACTIVE_EXPOSURE"; rc = 2
    elif task_errors or account.task_errors or account.summary()["unresolved_exposure"]:
        terminal = "TECHNICAL_FAILURE"; rc = 2
    elif account.closed() >= args.target_closed_trades:
        terminal = "SAMPLE_TARGET_REACHED"; rc = 0
    else:
        terminal = "TIME_LIMIT_REACHED"; rc = 3
    checkpoint(terminal)
    final = json.loads(args.status.read_text(encoding="utf-8"))
    final["phase3_requirement"] = {"minimum_closed_trades": args.target_closed_trades, "met": account.closed() >= args.target_closed_trades}
    final["post_exit_complete_for_all_closed"] = all(bool((r.get("post_exit") or {}).get("complete")) for r in account.ledger if r.get("status") == "CLOSED")
    atomic_json(args.output, final)
    return rc


def parser() -> argparse.ArgumentParser:
    p=base.parser()
    p.set_defaults(execution_delay_ms=6.0)
    return p


def self_test() -> None:
    base.self_test()
    assert DRAIN_SECONDS > max(x.max_hold_ms for x in mgmt.TIERS.values())/1000.0 + base.POST_EXIT_SECONDS
    print("GOLDEN_MANAGEMENT_V2_HARDENED_SELF_TEST_OK")


def main() -> int:
    args=parser().parse_args()
    if args.self_test:
        self_test(); return 0
    required=("production_root","corpus","state_output","status","output")
    missing=[x for x in required if getattr(args,x) is None]
    if missing: raise SystemExit("missing required arguments: "+",".join(missing))
    if not math.isclose(args.starting_balance_sol,base.STARTING_BALANCE_SOL):
        raise SystemExit("v2 campaign starting balance must be exactly 3 SOL")
    if args.execution_delay_ms < 0 or args.execution_delay_ms >= 10.0:
        raise SystemExit("paper execution delay must be >=0 and <10ms")
    return asyncio.run(run(args))

if __name__=='__main__': raise SystemExit(main())
