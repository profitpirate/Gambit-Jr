#!/usr/bin/env python3
"""Golden Management v2 live-market paper runner.

This runner consumes freshly arriving Pump launches from live Solana websocket logs.
It never signs or broadcasts transactions and never touches real funds. Fills are
paper fills against the live bonding-curve state observed at that moment.

The entry thesis remains Golden Buyer Reputation v1. Management v2 changes only:
- conviction-based account sizing (5/8/15/25%);
- structural 30/20% initial reduction;
- dynamic RunnerGuardian exits/scale-outs;
- full path/action/control/post-exit telemetry;
- 3 SOL paper campaign bankroll.

Historical replay is deliberately not supported by this executable. A launch is
eligible only when its CREATE event is first observed after this process started.
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

VERSION = "golden-management-v2-live-v1"
STARTING_BALANCE_SOL = 3.0
MAX_CONCURRENT = 2
POST_EXIT_SECONDS = 20.0
CONTROL_HOLD_MS = 2000.0


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    p = (len(xs) - 1) * q
    lo = int(math.floor(p)); hi = int(math.ceil(p)); w = p - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def valid_state(state: Mapping[str, Any] | None) -> bool:
    return live_v1.valid_state(state)


class ManagedPaperAccount:
    def __init__(self, starting_balance: float, resume: Mapping[str, Any] | None = None):
        if not math.isclose(starting_balance, STARTING_BALANCE_SOL):
            raise ValueError("management v2 campaign is frozen to 3 SOL paper bankroll")
        resume = resume or {}
        self.starting_balance = STARTING_BALANCE_SOL
        self.cash = float(resume.get("cash_sol", STARTING_BALANCE_SOL))
        self.ledger: list[dict[str, Any]] = list(resume.get("ledger") or [])
        self.signals = int(resume.get("signals", 0))
        self.rejections = Counter(resume.get("rejections") or {})
        self.active: dict[str, dict[str, Any]] = {}
        self.pending: set[str] = set()
        self.post_exit: dict[str, dict[str, Any]] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.task_errors: list[str] = []
        self.execution_latencies_ms: list[float] = [float(x) for x in resume.get("execution_latencies_ms", [])][-5000:]
        self._lock = asyncio.Lock()

    def closed(self) -> int:
        return sum(1 for r in self.ledger if r.get("status") == "CLOSED")

    def mark_value(self, p: Mapping[str, Any]) -> float:
        return max(0.0, finite(p.get("mark_remaining_value_sol")))

    def equity(self) -> float:
        return self.cash + sum(self.mark_value(p) for p in self.active.values())

    def schedule(self, mint: str, decision: Mapping[str, Any], latest_states: dict[str, dict[str, Any]], execution_delay_ms: float) -> None:
        self.signals += 1
        if mint in self.pending or mint in self.active:
            self.rejections["DUPLICATE"] += 1
            return
        if len(self.pending) + len(self.active) >= MAX_CONCURRENT:
            self.rejections["CONCURRENCY"] += 1
            return
        self.pending.add(mint)
        task = asyncio.create_task(self._enter_and_manage(mint, dict(decision), latest_states, execution_delay_ms))
        self.tasks.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            self.tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.task_errors.append(repr(exc))
        task.add_done_callback(done)

    async def _enter_and_manage(self, mint: str, decision: dict[str, Any], latest_states: dict[str, dict[str, Any]], execution_delay_ms: float) -> None:
        try:
            signal_ns = time.time_ns()
            if execution_delay_ms > 0:
                await asyncio.sleep(execution_delay_ms / 1000.0)
            async with self._lock:
                state = latest_states.get(mint)
                if not valid_state(state):
                    self.rejections["NO_VALID_ENTRY_STATE"] += 1
                    return
                if len(self.active) >= MAX_CONCURRENT:
                    self.rejections["CONCURRENCY"] += 1
                    return
                cm = mgmt.conviction(decision)
                tier = str(cm["conviction_tier"])
                cfg = mgmt.TIERS[tier]
                equity_before = self.equity()
                stake = min(max(0.0, self.cash - core.RESERVE_SOL), equity_before * cfg.position_fraction)
                if stake <= 0:
                    self.rejections["INSUFFICIENT_CASH"] += 1
                    return
                tokens, _ = core.quote_buy(stake, state)
                if tokens <= 0:
                    self.rejections["NO_FILL"] += 1
                    return
                entry_ns = time.time_ns()
                latency_ms = (entry_ns - int(decision["decision_ns"])) / 1_000_000.0
                self.execution_latencies_ms.append(latency_ms)
                self.execution_latencies_ms = self.execution_latencies_ms[-5000:]
                self.cash -= stake
                immediate_mark = core.quote_sell(tokens, state)
                guardian = mgmt.RunnerGuardian(cfg)
                self.active[mint] = {
                    "mint": mint,
                    "status": "OPEN",
                    "stake_sol": stake,
                    "position_fraction": cfg.position_fraction,
                    "conviction_score": cm["conviction_score"],
                    "conviction_tier": tier,
                    "initial_fraction": cfg.initial_fraction,
                    "original_tokens": tokens,
                    "remaining_tokens": tokens,
                    "realized_proceeds_sol": 0.0,
                    "mark_remaining_value_sol": immediate_mark,
                    "signal_ns": signal_ns,
                    "entry_ns": entry_ns,
                    "entry_state_ns": int(state.get("ns", 0)),
                    "create_ns": int(decision["create_ns"]),
                    "decision_ns": int(decision["decision_ns"]),
                    "create_to_decision_ms": (int(decision["decision_ns"]) - int(decision["create_ns"])) / 1_000_000.0,
                    "create_to_entry_ms": (entry_ns - int(decision["create_ns"])) / 1_000_000.0,
                    "decision_to_entry_ms": latency_ms,
                    "entry_reason": {
                        "rule": core.VERSION,
                        "prior_appearances": int(decision["prior_appearances"]),
                        "prior_wins": int(decision["prior_wins"]),
                        "prior_win_rate": decision["prior_win_rate"],
                        "buyer_count": len(decision["buyers"]),
                        "buyers": list(decision["buyers"]),
                        "conviction": cm,
                    },
                    "guardian": guardian,
                    "actions": [],
                    "path": [],
                    "mfe_fraction": immediate_mark / stake - 1.0,
                    "mae_fraction": immediate_mark / stake - 1.0,
                    "control_v1_2s": None,
                    "equity_before_sol": equity_before,
                }
            await self._manage_loop(mint, latest_states)
        finally:
            self.pending.discard(mint)

    async def _manage_loop(self, mint: str, latest_states: dict[str, dict[str, Any]]) -> None:
        while mint in self.active:
            await asyncio.sleep(0.008)
            async with self._lock:
                p = self.active.get(mint)
                if not p:
                    return
                state = latest_states.get(mint)
                if not valid_state(state):
                    # Do not silently manufacture an exit. Give the feed a short grace period.
                    if (time.time_ns() - int(p["entry_ns"])) / 1e9 > 20.0:
                        p["status"] = "UNRESOLVED_EXIT"
                        self.task_errors.append(f"{mint}:UNRESOLVED_EXIT")
                    continue
                await self._apply_mark_locked(p, state)

    async def _apply_mark_locked(self, p: dict[str, Any], state: Mapping[str, Any]) -> None:
        now_ns = time.time_ns()
        elapsed_ms = (now_ns - int(p["entry_ns"])) / 1_000_000.0
        stake = float(p["stake_sol"])
        original_tokens = float(p["original_tokens"])
        full_mark = core.quote_sell(original_tokens, state)
        full_return = full_mark / stake - 1.0
        remaining_tokens = float(p["remaining_tokens"])
        remaining_mark = core.quote_sell(remaining_tokens, state) if remaining_tokens > 0 else 0.0
        p["mark_remaining_value_sol"] = remaining_mark
        p["mfe_fraction"] = max(float(p["mfe_fraction"]), full_return)
        p["mae_fraction"] = min(float(p["mae_fraction"]), full_return)
        path = p["path"]
        # Preserve every material state change but bound memory.
        if len(path) < 2000:
            path.append({
                "t_ms": elapsed_ms,
                "return_fraction": full_return,
                "remaining_fraction": remaining_tokens / original_tokens if original_tokens else 0.0,
                "state_ns": int(state.get("ns", 0)),
                "vsol": finite(state.get("vsol")),
                "vtok": finite(state.get("vtok")),
            })

        if p.get("control_v1_2s") is None and elapsed_ms >= CONTROL_HOLD_MS:
            p["control_v1_2s"] = {
                "captured_at_ms": elapsed_ms,
                "proceeds_sol": full_mark,
                "pnl_sol": full_mark - stake,
                "return_fraction": full_return,
            }

        action = p["guardian"].on_mark(mgmt.Mark(
            t_ms=elapsed_ms,
            return_fraction=full_return,
            state_ns=int(state.get("ns", 0)),
            vsol=finite(state.get("vsol")),
            vtok=finite(state.get("vtok")),
        ))
        if action is not None:
            await self._execute_action_locked(p, state, action)

    async def _execute_action_locked(self, p: dict[str, Any], state: Mapping[str, Any], action: mgmt.Action) -> None:
        mint = str(p["mint"])
        original_tokens = float(p["original_tokens"])
        remaining_tokens = float(p["remaining_tokens"])
        requested_tokens = original_tokens * float(action.fraction_of_entry)
        sell_tokens = min(remaining_tokens, requested_tokens)
        if action.kind == "EXIT":
            sell_tokens = remaining_tokens
        if sell_tokens <= 0:
            return
        proceeds = core.quote_sell(sell_tokens, state)
        if proceeds < 0:
            raise RuntimeError(f"{mint}:negative proceeds")
        self.cash += proceeds
        p["realized_proceeds_sol"] = float(p["realized_proceeds_sol"]) + proceeds
        p["remaining_tokens"] = max(0.0, remaining_tokens - sell_tokens)
        remaining_mark = core.quote_sell(float(p["remaining_tokens"]), state) if p["remaining_tokens"] > 0 else 0.0
        p["mark_remaining_value_sol"] = remaining_mark
        p["actions"].append({
            "kind": action.kind,
            "reason": action.reason,
            "t_ms": action.t_ms,
            "mark_return": action.mark_return,
            "fraction_of_entry": sell_tokens / original_tokens if original_tokens else 0.0,
            "tokens_sold": sell_tokens,
            "proceeds_sol": proceeds,
            "remaining_fraction": float(p["remaining_tokens"]) / original_tokens if original_tokens else 0.0,
            "state_ns": int(state.get("ns", 0)),
        })
        if p["remaining_tokens"] <= max(1e-9, original_tokens * 1e-9) or action.kind == "EXIT":
            if p["remaining_tokens"] > 0:
                tail = core.quote_sell(float(p["remaining_tokens"]), state)
                self.cash += tail
                p["realized_proceeds_sol"] += tail
                p["actions"].append({
                    "kind": "EXIT",
                    "reason": "FINAL_DUST_FLATTEN",
                    "t_ms": action.t_ms,
                    "mark_return": action.mark_return,
                    "fraction_of_entry": float(p["remaining_tokens"]) / original_tokens if original_tokens else 0.0,
                    "tokens_sold": float(p["remaining_tokens"]),
                    "proceeds_sol": tail,
                    "remaining_fraction": 0.0,
                    "state_ns": int(state.get("ns", 0)),
                })
                p["remaining_tokens"] = 0.0
                p["mark_remaining_value_sol"] = 0.0
            self._close_position_locked(p, state, action.reason)

    def _close_position_locked(self, p: dict[str, Any], state: Mapping[str, Any], exit_reason: str) -> None:
        mint = str(p["mint"])
        exit_ns = time.time_ns()
        proceeds = float(p["realized_proceeds_sol"])
        stake = float(p["stake_sol"])
        pnl = proceeds - stake
        row = {k: v for k, v in p.items() if k != "guardian"}
        row.update({
            "status": "CLOSED",
            "exit_ns": exit_ns,
            "exit_state_ns": int(state.get("ns", 0)),
            "actual_hold_ms": (exit_ns - int(p["entry_ns"])) / 1_000_000.0,
            "exit_reason": exit_reason,
            "proceeds_sol": proceeds,
            "pnl_sol": pnl,
            "return_fraction": pnl / stake if stake else None,
            "win": pnl > 0,
            "balance_after_sol": self.cash + sum(self.mark_value(x) for k, x in self.active.items() if k != mint),
            "post_exit": {
                "window_seconds": POST_EXIT_SECONDS,
                "exit_full_position_return": (core.quote_sell(float(p["original_tokens"]), state) / stake - 1.0) if stake else None,
                "max_return_fraction": None,
                "min_return_fraction": None,
                "went_higher_than_exit": False,
                "went_lower_than_exit": False,
                "complete": False,
            },
        })
        self.ledger.append(row)
        self.active.pop(mint, None)
        self.post_exit[mint] = {
            "deadline_ns": exit_ns + int(POST_EXIT_SECONDS * 1e9),
            "row": row,
            "stake_sol": stake,
            "original_tokens": float(p["original_tokens"]),
        }

    def observe_state(self, mint: str, state: Mapping[str, Any]) -> None:
        tracker = self.post_exit.get(mint)
        if not tracker or not valid_state(state):
            return
        now = time.time_ns()
        row = tracker["row"]
        post = row["post_exit"]
        mark = core.quote_sell(float(tracker["original_tokens"]), state)
        ret = mark / float(tracker["stake_sol"]) - 1.0
        post["max_return_fraction"] = ret if post["max_return_fraction"] is None else max(float(post["max_return_fraction"]), ret)
        post["min_return_fraction"] = ret if post["min_return_fraction"] is None else min(float(post["min_return_fraction"]), ret)
        exit_ret = finite(post.get("exit_full_position_return"))
        post["went_higher_than_exit"] = bool(post["went_higher_than_exit"] or ret > exit_ret + 1e-12)
        post["went_lower_than_exit"] = bool(post["went_lower_than_exit"] or ret < exit_ret - 1e-12)
        if now >= int(tracker["deadline_ns"]):
            post["complete"] = True
            self.post_exit.pop(mint, None)

    def finalize_post_exit(self) -> None:
        now = time.time_ns()
        for mint, tracker in list(self.post_exit.items()):
            if now >= int(tracker["deadline_ns"]):
                tracker["row"]["post_exit"]["complete"] = True
                self.post_exit.pop(mint, None)

    def summary(self) -> dict[str, Any]:
        rows = [r for r in self.ledger if r.get("status") == "CLOSED"]
        pnls = [finite(r.get("pnl_sol")) for r in rows]
        wins = sum(x > 0 for x in pnls)
        gains = sum(x for x in pnls if x > 0); losses = -sum(x for x in pnls if x < 0)
        peak = self.starting_balance; max_dd = 0.0
        for r in rows:
            b = finite(r.get("balance_after_sol"), peak)
            peak = max(peak, b)
            if peak > 0:
                max_dd = max(max_dd, (peak - b) / peak)
        lat = list(self.execution_latencies_ms)
        tiers = Counter(str(r.get("conviction_tier")) for r in rows)
        control_pnls = [finite((r.get("control_v1_2s") or {}).get("pnl_sol")) for r in rows if r.get("control_v1_2s")]
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
            "roi_fraction": self.equity() / self.starting_balance - 1.0,
            "profit_factor": gains / losses if losses else (999.0 if gains else None),
            "expectancy_sol": sum(pnls) / len(rows) if rows else None,
            "maximum_drawdown_fraction": max_dd,
            "active_positions": len(self.active),
            "pending_entries": len(self.pending),
            "tier_counts": dict(tiers),
            "rejections": dict(self.rejections),
            "task_errors": list(self.task_errors),
            "unresolved_exposure": any(p.get("status") == "UNRESOLVED_EXIT" for p in self.active.values()),
            "execution_latency_ms": {
                "count": len(lat),
                "median": percentile(lat, 0.50),
                "p95": percentile(lat, 0.95),
                "p99": percentile(lat, 0.99),
                "max": max(lat) if lat else None,
            },
            "v1_control_net_pnl_sol": sum(control_pnls) if control_pnls else None,
            "post_exit_tracking_pending": len(self.post_exit),
        }

    def persistence(self) -> dict[str, Any]:
        # Active positions are intentionally not resumable across CI windows.
        # A clean window must finish/flatten all active positions before persistence.
        return {
            "cash_sol": self.cash,
            "signals": self.signals,
            "ledger": self.ledger,
            "rejections": dict(self.rejections),
            "execution_latencies_ms": self.execution_latencies_ms[-5000:],
        }


def read_resume(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != VERSION:
        raise RuntimeError("v2 resume version mismatch")
    if data.get("rule_version") != core.VERSION:
        raise RuntimeError("Golden entry rule mismatch")
    if data.get("management_version") != mgmt.VERSION:
        raise RuntimeError("management version mismatch")
    if not math.isclose(float(data.get("campaign_starting_balance_sol", 0)), STARTING_BALANCE_SOL):
        raise RuntimeError("resume bankroll is not 3 SOL")
    return data


def apply_delta(rep: core.Reputation, delta: Mapping[str, Any]) -> None:
    for wallet, pair in (delta or {}).items():
        rep.appear[str(wallet)] += int(pair[0]); rep.wins[str(wallet)] += int(pair[1])


def status_payload(started: float, counts: Counter, account: ManagedPaperAccount, queue: asyncio.Queue[Any], endpoints: Mapping[str, Any], last_event: float, recent: deque[dict[str, Any]], terminal: str | None = None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "rule_version": core.VERSION,
        "management_version": mgmt.VERSION,
        "paper_only": True,
        "real_money_execution": False,
        "market_data_mode": "LIVE_SOLANA_WEBSOCKET_FRESH_LAUNCHES_ONLY",
        "historical_replay": False,
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
        "entry_rule": {
            "decision_ms": core.DECISION_MS,
            "minimum_prior_appearances": core.MIN_APPEARANCES,
            "minimum_prior_win_rate": core.MIN_PRIOR_WIN_RATE,
        },
        "management_tiers": {k: {"position_fraction": v.position_fraction, "initial_fraction": v.initial_fraction, "max_hold_ms": v.max_hold_ms} for k, v in mgmt.TIERS.items()},
        "recent_decisions": list(recent)[-20:],
        "endpoints": endpoints,
    }


async def run(args: argparse.Namespace) -> int:
    production_root = args.production_root.resolve()
    sys.path.insert(0, str(production_root / "src"))
    prod = live_v1.load_module("golden_v2_prod", production_root / "scripts" / "e4_live_market_stress.py")
    resume = read_resume(args.resume_state)
    rep = core.initialise_reputation_from_corpus(args.corpus)
    apply_delta(rep, resume.get("reputation_delta") or {})
    delta_appear = Counter({k: int(v[0]) for k, v in (resume.get("reputation_delta") or {}).items()})
    delta_wins = Counter({k: int(v[1]) for k, v in (resume.get("reputation_delta") or {}).items()})
    account = ManagedPaperAccount(STARTING_BALANCE_SOL, resume.get("account"))

    latest_states: dict[str, dict[str, Any]] = {}
    launches: dict[str, dict[str, Any]] = {}
    queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=30_000)
    stop = asyncio.Event(); endpoints: dict[str, Any] = {}; dedupe: set[tuple[str, int, str]] = set()
    counts: Counter = Counter(); recent: deque[dict[str, Any]] = deque(maxlen=100)
    task_errors: list[str] = []; background: set[asyncio.Task[Any]] = set()
    started = time.time(); session_started_ns = time.time_ns(); last_event = started

    def add_task(coro: Any) -> None:
        task = asyncio.create_task(coro); background.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            background.discard(t)
            if not t.cancelled() and t.exception():
                task_errors.append(repr(t.exception()))
        task.add_done_callback(done)

    def checkpoint(terminal: str | None = None) -> None:
        account.finalize_post_exit()
        payload = status_payload(started, counts, account, queue, endpoints, last_event, recent, terminal)
        if task_errors:
            payload["task_errors"] = list(task_errors[-20:])
        atomic_json(args.status, payload)
        delta = {w: [delta_appear[w], delta_wins[w]] for w in delta_appear}
        atomic_json(args.state_output, {
            "version": VERSION,
            "rule_version": core.VERSION,
            "management_version": mgmt.VERSION,
            "paper_only": True,
            "campaign_starting_balance_sol": STARTING_BALANCE_SOL,
            "account": account.persistence(),
            "reputation_delta": delta,
            "updated_at": time.time(),
        })

    async def settle_reputation(mint: str, buyers: list[str], decision_state: dict[str, Any] | None) -> None:
        await asyncio.sleep(core.HOLD_MS / 1000.0)
        exit_state = latest_states.get(mint)
        if not valid_state(decision_state) or not valid_state(exit_state):
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
            "valid_decision_state": valid_state(state),
        }
        recent.append({k: decision[k] for k in ("mint", "buyer_count", "prior_appearances", "prior_win_rate", "qualifies")})
        counts["qualified"] += int(qualifies)
        add_task(settle_reputation(mint, buyers, dict(state) if state else None))
        if qualifies:
            account.schedule(mint, decision, latest_states, args.execution_delay_ms)

    workers = [asyncio.create_task(live_v1.endpoint_worker(url, prod, queue, stop, endpoints)) for url in dict.fromkeys(args.ws_url or prod.DEFAULT_WS_RPCS)]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass

    checkpoint(); deadline = time.monotonic() + args.duration_seconds; last_checkpoint = 0.0
    try:
        while time.monotonic() < deadline and not stop.is_set() and account.closed() < args.target_closed_trades:
            try: payload = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError: payload = None
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
                        if creates and mint not in launches:
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
                            cutoff = int(launch["create_ns"]) + 25_000_000_000
                            launch["events"].extend(e for e in events if core.event_ns(e) <= cutoff)
            now = time.monotonic()
            if now - last_checkpoint >= args.heartbeat_seconds:
                protected = set(account.active) | set(account.pending) | set(account.post_exit)
                cutoff_ns = time.time_ns() - 90_000_000_000
                for mint in list(latest_states):
                    if mint not in protected and int(latest_states[mint].get("ns", 0)) < cutoff_ns:
                        latest_states.pop(mint, None); launches.pop(mint, None)
                checkpoint(); last_checkpoint = now
                print("GOLDEN_V2_LIVE " + json.dumps({"counts": dict(counts), "account": account.summary()}, separators=(",", ":")), flush=True)
            if task_errors or account.task_errors or account.summary()["unresolved_exposure"]:
                stop.set()
    finally:
        stop.set()
        for w in workers: w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Management positions must be allowed to flatten cleanly before state persistence.
        end_wait = time.monotonic() + 20.0
        while account.active and time.monotonic() < end_wait:
            await asyncio.sleep(0.05)
        if background:
            try: await asyncio.wait_for(asyncio.gather(*list(background), return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError: pass
        if account.tasks:
            try: await asyncio.wait_for(asyncio.gather(*list(account.tasks), return_exceptions=True), timeout=3.0)
            except asyncio.TimeoutError: pass

    if account.active:
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
    atomic_json(args.output, final)
    return rc


def self_test() -> None:
    mgmt.self_test()
    a = ManagedPaperAccount(3.0)
    assert math.isclose(a.equity(), 3.0)
    assert mgmt.TIERS["BASE"].position_fraction == 0.05
    assert mgmt.TIERS["MEDIUM"].position_fraction == 0.08
    assert mgmt.TIERS["HIGH"].position_fraction == 0.15
    assert mgmt.TIERS["EXCEPTIONAL"].position_fraction == 0.25
    state = {"ns": 1, "vsol": 30.0, "vtok": 1_000_000.0, "rtok": 500_000.0, "complete": False}
    tokens, _ = core.quote_buy(0.75, state)
    assert tokens > 0
    assert core.quote_sell(tokens * 0.2, state) >= 0
    print("GOLDEN_MANAGEMENT_V2_LIVE_SELF_TEST_OK")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--production-root", type=Path)
    p.add_argument("--corpus", type=Path)
    p.add_argument("--starting-balance-sol", type=float, default=STARTING_BALANCE_SOL)
    p.add_argument("--execution-delay-ms", type=float, default=8.0)
    p.add_argument("--target-closed-trades", type=int, default=50)
    p.add_argument("--duration-seconds", type=float, default=16_200.0)
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
    required = ("production_root", "corpus", "state_output", "status", "output")
    missing = [x for x in required if getattr(args, x) is None]
    if missing: raise SystemExit("missing required arguments: " + ",".join(missing))
    if not math.isclose(args.starting_balance_sol, STARTING_BALANCE_SOL):
        raise SystemExit("v2 campaign starting balance must be exactly 3 SOL")
    if args.execution_delay_ms < 0 or args.execution_delay_ms >= 10.0:
        raise SystemExit("paper execution delay must be >=0 and <10ms")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
