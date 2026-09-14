#!/usr/bin/env python3
"""Prospective live-paper runner for frozen Golden Buyer Reputation v1.

Paper only: this process never signs or broadcasts transactions.
The entry rule is frozen in golden_buyer_reputation_core.py.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import signal
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Mapping

import aiohttp

import golden_buyer_reputation_core as core

VERSION = "golden-buyer-reputation-live-v1"
DEFAULT_STARTING_BALANCE_SOL = 5.0


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def valid_state(state: Mapping[str, Any] | None) -> bool:
    if not state or state.get("complete"):
        return False
    try:
        return float(state.get("vsol", 0)) > 0 and float(state.get("vtok", 0)) > 0
    except (TypeError, ValueError):
        return False


class PaperAccount:
    def __init__(self, starting_balance: float, resume: Mapping[str, Any] | None = None):
        resume = resume or {}
        self.starting_balance = float(resume.get("campaign_starting_balance_sol", starting_balance))
        self.cash = float(resume.get("cash_sol", starting_balance))
        self.ledger = list(resume.get("ledger") or [])
        self.signals = int(resume.get("signals", 0))
        self.rejections = Counter(resume.get("rejections") or {})
        self.active: dict[str, dict[str, Any]] = {}
        self.pending: set[str] = set()
        self.task_errors: list[str] = []
        self.tasks: set[asyncio.Task[Any]] = set()

    def equity(self) -> float:
        return self.cash + sum(float(p["stake_sol"]) for p in self.active.values())

    def closed(self) -> int:
        return sum(1 for row in self.ledger if row.get("status") == "CLOSED")

    def schedule(self, mint: str, decision: Mapping[str, Any], latest_states: dict[str, dict[str, Any]], execution_delay_ms: float) -> None:
        self.signals += 1
        if mint in self.pending or mint in self.active:
            self.rejections["DUPLICATE"] += 1
            return
        if len(self.pending) + len(self.active) >= core.MAX_CONCURRENT:
            self.rejections["CONCURRENCY"] += 1
            return
        self.pending.add(mint)
        task = asyncio.create_task(self._trade(mint, dict(decision), latest_states, execution_delay_ms))
        self.tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self.tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.task_errors.append(repr(exc))

        task.add_done_callback(done)

    async def _trade(self, mint: str, decision: dict[str, Any], latest_states: dict[str, dict[str, Any]], execution_delay_ms: float) -> None:
        try:
            signal_ns = time.time_ns()
            if execution_delay_ms > 0:
                await asyncio.sleep(execution_delay_ms / 1000.0)
            state = latest_states.get(mint)
            if not valid_state(state):
                self.rejections["NO_VALID_ENTRY_STATE"] += 1
                return
            if len(self.active) >= core.MAX_CONCURRENT:
                self.rejections["CONCURRENCY"] += 1
                return
            stake = min(max(0.0, self.cash - core.RESERVE_SOL), self.equity() * core.POSITION_FRACTION)
            if stake <= 0:
                self.rejections["INSUFFICIENT_CASH"] += 1
                return
            tokens, _ = core.quote_buy(stake, state)
            if tokens <= 0:
                self.rejections["NO_FILL"] += 1
                return
            entry_ns = time.time_ns()
            self.cash -= stake
            position = {
                "mint": mint,
                "stake_sol": stake,
                "tokens": tokens,
                "signal_ns": signal_ns,
                "entry_ns": entry_ns,
                "entry_state_ns": int(state.get("ns", 0)),
                "create_ns": int(decision["create_ns"]),
                "decision_ns": int(decision["decision_ns"]),
                "create_to_decision_ms": (int(decision["decision_ns"]) - int(decision["create_ns"])) / 1_000_000.0,
                "create_to_entry_ms": (entry_ns - int(decision["create_ns"])) / 1_000_000.0,
                "decision_to_entry_ms": (entry_ns - int(decision["decision_ns"])) / 1_000_000.0,
                "prior_appearances": int(decision["prior_appearances"]),
                "prior_wins": int(decision["prior_wins"]),
                "prior_win_rate": decision["prior_win_rate"],
                "buyer_count": len(decision["buyers"]),
                "buyers": list(decision["buyers"]),
                "status": "OPEN",
            }
            self.active[mint] = position
            await asyncio.sleep(core.HOLD_MS / 1000.0)
            exit_state = latest_states.get(mint)
            if not valid_state(exit_state):
                position["status"] = "UNRESOLVED_EXIT"
                self.task_errors.append(f"{mint}:UNRESOLVED_EXIT")
                return
            proceeds = core.quote_sell(tokens, exit_state)
            exit_ns = time.time_ns()
            pnl = proceeds - stake
            self.cash += proceeds
            self.active.pop(mint, None)
            row = dict(position)
            row.update({
                "status": "CLOSED",
                "exit_ns": exit_ns,
                "exit_state_ns": int(exit_state.get("ns", 0)),
                "actual_hold_ms": (exit_ns - entry_ns) / 1_000_000.0,
                "proceeds_sol": proceeds,
                "pnl_sol": pnl,
                "win": pnl > 0,
                "balance_after_sol": self.equity(),
            })
            self.ledger.append(row)
        finally:
            self.pending.discard(mint)

    def summary(self) -> dict[str, Any]:
        rows = [r for r in self.ledger if r.get("status") == "CLOSED"]
        pnls = [float(r["pnl_sol"]) for r in rows]
        wins = sum(p > 0 for p in pnls)
        gains = sum(p for p in pnls if p > 0)
        losses = -sum(p for p in pnls if p < 0)
        peak = self.starting_balance
        max_dd = 0.0
        for row in rows:
            b = float(row.get("balance_after_sol", peak))
            peak = max(peak, b)
            if peak > 0:
                max_dd = max(max_dd, (peak - b) / peak)
        largest = max((p for p in pnls if p > 0), default=0.0)
        return {
            "campaign_starting_balance_sol": self.starting_balance,
            "cash_sol": self.cash,
            "equity_sol": self.equity(),
            "signals": self.signals,
            "closed_trades": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "win_rate": wins / len(rows) if rows else None,
            "net_pnl_sol": sum(pnls),
            "roi_fraction": (self.equity() / self.starting_balance - 1.0) if self.starting_balance else None,
            "profit_factor": gains / losses if losses else (999.0 if gains else None),
            "expectancy_sol": sum(pnls) / len(rows) if rows else None,
            "maximum_drawdown_fraction": max_dd,
            "largest_winner_profit_share": largest / gains if gains else None,
            "active_positions": len(self.active),
            "pending_entries": len(self.pending),
            "rejections": dict(self.rejections),
            "task_errors": list(self.task_errors),
            "unresolved_exposure": any(p.get("status") == "UNRESOLVED_EXIT" for p in self.active.values()),
        }

    def persistence(self) -> dict[str, Any]:
        return {
            "campaign_starting_balance_sol": self.starting_balance,
            "cash_sol": self.cash,
            "signals": self.signals,
            "ledger": self.ledger,
            "rejections": dict(self.rejections),
        }


async def endpoint_worker(url: str, prod: Any, queue: asyncio.Queue[Mapping[str, Any]], stop: asyncio.Event, stats: dict[str, Any]) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=20)
    stat = stats.setdefault(url, {"connections": 0, "messages": 0, "disconnects": 0, "errors": []})
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8 * 1024 * 1024) as ws:
                    stat["connections"] += 1
                    await ws.send_json({
                        "jsonrpc": "2.0",
                        "id": stat["connections"],
                        "method": "logsSubscribe",
                        "params": [{"mentions": [prod.PUMP_PROGRAM_ID]}, {"commitment": "processed"}],
                    })
                    while not stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            stat["messages"] += 1
                            try:
                                await queue.put(json.loads(msg.data))
                            except json.JSONDecodeError:
                                continue
                        elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                            stat["disconnects"] += 1
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stat["disconnects"] += 1
                stat["errors"].append(f"{type(exc).__name__}:{str(exc)[:120]}")
                stat["errors"] = stat["errors"][-10:]
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass


def read_resume(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != VERSION:
        raise RuntimeError("resume state version mismatch")
    if payload.get("rule_version") != core.VERSION:
        raise RuntimeError("resume rule version mismatch")
    if float(payload.get("campaign_starting_balance_sol", DEFAULT_STARTING_BALANCE_SOL)) != DEFAULT_STARTING_BALANCE_SOL:
        raise RuntimeError("resume state is not the 5 SOL campaign")
    return payload


def apply_delta(rep: core.Reputation, delta: Mapping[str, Any]) -> None:
    for wallet, pair in (delta or {}).items():
        rep.appear[str(wallet)] += int(pair[0])
        rep.wins[str(wallet)] += int(pair[1])


def status_payload(started: float, counts: Counter, account: PaperAccount, queue: asyncio.Queue[Any], endpoints: Mapping[str, Any], last_event: float, decisions_recent: deque[dict[str, Any]], terminal: str | None = None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "rule_version": core.VERSION,
        "paper_only": True,
        "no_e4_entry_signal": True,
        "phase": "online",
        "status": terminal or "RUNNING",
        "started_at": started,
        "checked_at": time.time(),
        "elapsed_seconds": time.time() - started,
        "last_event_at": last_event,
        "queue_depth": queue.qsize(),
        "counts": dict(counts),
        "account": account.summary(),
        "frozen_rule": {
            "decision_ms": core.DECISION_MS,
            "hold_ms": core.HOLD_MS,
            "minimum_prior_appearances": core.MIN_APPEARANCES,
            "minimum_prior_win_rate": core.MIN_PRIOR_WIN_RATE,
            "position_fraction": core.POSITION_FRACTION,
            "max_concurrent": core.MAX_CONCURRENT,
        },
        "recent_decisions": list(decisions_recent)[-20:],
        "endpoints": endpoints,
    }


async def run(args: argparse.Namespace) -> int:
    production_root = args.production_root.resolve()
    sys.path.insert(0, str(production_root / "src"))
    prod = load_module("golden_live_prod", production_root / "scripts" / "e4_live_market_stress.py")

    resume = read_resume(args.resume_state)
    rep = core.initialise_reputation_from_corpus(args.corpus)
    apply_delta(rep, resume.get("reputation_delta") or {})
    delta_appear = Counter({k: int(v[0]) for k, v in (resume.get("reputation_delta") or {}).items()})
    delta_wins = Counter({k: int(v[1]) for k, v in (resume.get("reputation_delta") or {}).items()})
    account = PaperAccount(args.starting_balance_sol, resume.get("account"))

    latest_states: dict[str, dict[str, Any]] = {}
    launches: dict[str, dict[str, Any]] = {}
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=20_000)
    stop = asyncio.Event()
    endpoints: dict[str, Any] = {}
    dedupe: set[tuple[str, int, str]] = set()
    counts: Counter = Counter()
    recent_decisions: deque[dict[str, Any]] = deque(maxlen=100)
    task_errors: list[str] = []
    background: set[asyncio.Task[Any]] = set()
    started = time.time()
    last_event = started

    def add_task(coro: Any) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            background.discard(t)
            if not t.cancelled() and t.exception():
                task_errors.append(repr(t.exception()))
        task.add_done_callback(done)

    def checkpoint(terminal: str | None = None) -> None:
        payload = status_payload(started, counts, account, queue, endpoints, last_event, recent_decisions, terminal)
        if task_errors:
            payload["task_errors"] = list(task_errors[-20:])
        atomic_json(args.status, payload)
        delta = {w: [delta_appear[w], delta_wins[w]] for w in delta_appear}
        persistent = {
            "version": VERSION,
            "rule_version": core.VERSION,
            "paper_only": True,
            "campaign_starting_balance_sol": args.starting_balance_sol,
            "account": account.persistence(),
            "reputation_delta": delta,
            "updated_at": time.time(),
        }
        atomic_json(args.state_output, persistent)

    async def settle_reputation(mint: str, buyers: list[str], decision_state: dict[str, Any] | None, decision_ns: int) -> None:
        await asyncio.sleep(core.HOLD_MS / 1000.0)
        exit_state = latest_states.get(mint)
        if not valid_state(decision_state) or not valid_state(exit_state):
            counts["benchmark_skipped"] += 1
            return
        tokens, _ = core.quote_buy(core.BENCHMARK_STAKE_SOL, decision_state)
        if tokens <= 0:
            counts["benchmark_skipped"] += 1
            return
        pnl = core.quote_sell(tokens, exit_state) - core.BENCHMARK_STAKE_SOL
        won = pnl > 0
        for wallet in dict.fromkeys(buyers):
            rep.appear[wallet] += 1
            rep.wins[wallet] += int(won)
            delta_appear[wallet] += 1
            delta_wins[wallet] += int(won)
        counts["benchmark_outcomes"] += 1
        counts["benchmark_wins"] += int(won)

    async def decide(mint: str) -> None:
        launch = launches.get(mint)
        if not launch:
            return
        deadline_ns = int(launch["create_ns"]) + core.DECISION_MS * 1_000_000
        delay = max(0.0, (deadline_ns - time.time_ns()) / 1_000_000_000.0)
        if delay:
            await asyncio.sleep(delay)
        launch = launches.get(mint)
        if not launch:
            return
        create_ns = int(launch["create_ns"])
        buyers = core.unique_early_buyers(launch["events"], str(launch["creator"]), create_ns, core.DECISION_MS)
        counts["decisions"] += 1
        counts["early_buyers_seen"] += len(buyers)
        qualifies, n, w, rate = rep.qualifies(buyers)
        decision_ns = time.time_ns()
        state = latest_states.get(mint)
        decision = {
            "mint": mint,
            "create_ns": create_ns,
            "decision_ns": decision_ns,
            "buyers": buyers,
            "buyer_count": len(buyers),
            "prior_appearances": n,
            "prior_wins": w,
            "prior_win_rate": rate,
            "qualifies": qualifies,
            "valid_decision_state": valid_state(state),
        }
        recent_decisions.append({k: decision[k] for k in ("mint","buyer_count","prior_appearances","prior_win_rate","qualifies")})
        counts["qualified"] += int(qualifies)
        add_task(settle_reputation(mint, buyers, dict(state) if state else None, decision_ns))
        if qualifies:
            account.schedule(mint, decision, latest_states, args.execution_delay_ms)

    workers = [asyncio.create_task(endpoint_worker(url, prod, queue, stop, endpoints)) for url in dict.fromkeys(args.ws_url or prod.DEFAULT_WS_RPCS)]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    checkpoint()
    deadline = time.monotonic() + args.duration_seconds
    last_checkpoint = 0.0
    try:
        while time.monotonic() < deadline and not stop.is_set() and account.closed() < args.target_closed_trades:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                payload = None
            if payload is not None:
                incoming: list[Any] = []
                prod.decode_log_payload(payload, incoming, dedupe)
                if incoming:
                    last_event = time.time()
                    counts["decoded_events"] += len(incoming)
                    grouped: dict[str, list[Any]] = {}
                    for event in incoming:
                        grouped.setdefault(str(event.mint), []).append(event)
                        state = core.state_from_event(event)
                        if state:
                            latest_states[str(event.mint)] = state
                    for mint, events in grouped.items():
                        creates = [e for e in events if core.event_kind(e) == "CREATE"]
                        if creates and mint not in launches:
                            create = creates[0]
                            raw = getattr(create, "raw", {}) if isinstance(getattr(create, "raw", {}), Mapping) else {}
                            if not core.native_quote(raw):
                                counts["unsupported_quote"] += 1
                                continue
                            launches[mint] = {
                                "create_ns": core.event_ns(create),
                                "creator": core.event_creator(create),
                                "events": [],
                            }
                            counts["launches_seen"] += 1
                            add_task(decide(mint))
                        launch = launches.get(mint)
                        if launch:
                            cutoff = int(launch["create_ns"]) + (core.DECISION_MS + core.HOLD_MS + 500) * 1_000_000
                            launch["events"].extend(e for e in events if core.event_ns(e) <= cutoff)
            now = time.monotonic()
            if now - last_checkpoint >= args.heartbeat_seconds:
                # Bound old per-mint state without touching active/pending positions.
                protected = set(account.active) | set(account.pending)
                cutoff_ns = time.time_ns() - 60_000_000_000
                for mint in list(latest_states):
                    if mint not in protected and int(latest_states[mint].get("ns", 0)) < cutoff_ns:
                        latest_states.pop(mint, None)
                        launches.pop(mint, None)
                checkpoint()
                print("GOLDEN_LIVE " + json.dumps({"counts": dict(counts), "account": account.summary()}, separators=(",", ":")), flush=True)
                last_checkpoint = now
            if task_errors or account.task_errors or account.summary()["unresolved_exposure"]:
                stop.set()
    finally:
        stop.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Give normal 2-second exits/outcomes a short chance to settle.
        if background:
            try:
                await asyncio.wait_for(asyncio.gather(*list(background), return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        if account.tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*list(account.tasks), return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    if task_errors or account.task_errors or account.summary()["unresolved_exposure"]:
        terminal = "TECHNICAL_FAILURE"
        rc = 2
    elif account.closed() >= args.target_closed_trades:
        terminal = "SAMPLE_TARGET_REACHED"
        rc = 0
    else:
        terminal = "TIME_LIMIT_REACHED"
        rc = 3
    checkpoint(terminal)
    final = json.loads(args.status.read_text(encoding="utf-8"))
    final["terminal_result_is_not_profitability_certificate"] = True
    final["certification_target_closed_trades"] = args.target_closed_trades
    atomic_json(args.output, final)
    return rc


def self_test() -> None:
    rep = core.Reputation({"good": [10, 8], "bad": [10, 2], core.E4_WALLET: [100, 100]})
    fake = [
        {"kind": "BUY", "received_ns": 1_005_000_000, "trader": "good"},
        {"kind": "BUY", "received_ns": 1_006_000_000, "trader": core.E4_WALLET},
    ]
    buyers = core.unique_early_buyers(fake, "creator", 1_000_000_000, 10)
    assert buyers == ["good"], buyers
    assert rep.qualifies(buyers)[0]
    account = PaperAccount(5.0)
    assert math.isclose(account.equity(), 5.0)
    state = {"vsol": 30.0, "vtok": 1_000_000.0, "rtok": 500_000.0, "complete": False}
    tokens, _ = core.quote_buy(0.0925, state)
    assert tokens > 0 and core.quote_sell(tokens, state) > 0
    print("SELF_TEST_OK")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--production-root", type=Path)
    p.add_argument("--corpus", type=Path)
    p.add_argument("--starting-balance-sol", type=float, default=DEFAULT_STARTING_BALANCE_SOL)
    p.add_argument("--execution-delay-ms", type=float, default=10.0)
    p.add_argument("--target-closed-trades", type=int, default=50)
    p.add_argument("--duration-seconds", type=float, default=18_900.0)
    p.add_argument("--heartbeat-seconds", type=float, default=30.0)
    p.add_argument("--ws-url", action="append", default=[])
    p.add_argument("--resume-state", type=Path)
    p.add_argument("--state-output", type=Path)
    p.add_argument("--status", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        self_test(); return 0
    required = ("production_root","corpus","state_output","status","output")
    missing = [x for x in required if getattr(args, x) is None]
    if missing:
        raise SystemExit("missing required arguments: " + ",".join(missing))
    if not math.isclose(args.starting_balance_sol, 5.0):
        raise SystemExit("this campaign is frozen to a 5 SOL starting paper balance")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
