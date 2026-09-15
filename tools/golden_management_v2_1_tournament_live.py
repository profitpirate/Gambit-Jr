#!/usr/bin/env python3
"""True-online four-arm Golden management tournament (paper execution only).

Consumes freshly arriving Solana Pump launches from live websocket logs and feeds
exactly the same qualifying Golden signal/market path to four independent paper
accounts, each starting at 3 SOL:
  FULL_2S_CONTROL, PARTIALS_2S, V2_CURRENT, FLOW_V2_1.

No transaction is signed or broadcast.  E4 is observed only when its wallet
appears on the same live coin; E4 never affects qualification, sizing or exits.
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
import golden_management_v2 as v2
import golden_management_v2_1 as v21

VERSION = "golden-management-v2.1-tournament-live-v1"
STARTING_SOL = 3.0
MAX_CONCURRENT_SIGNALS = 2
POST_EXIT_SECONDS = 20.0
DRAIN_SECONDS = 75.0


def finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def percentile(values: list[float], q: float) -> float | None:
    if not values: return None
    xs = sorted(values)
    if len(xs) == 1: return xs[0]
    p = (len(xs)-1)*q; lo = int(math.floor(p)); hi = int(math.ceil(p)); w = p-lo
    return xs[lo]*(1-w)+xs[hi]*w


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)+"\n", encoding="utf-8")
    tmp.replace(path)


def valid_state(state: Mapping[str, Any] | None) -> bool:
    return live_v1.valid_state(state)


class ArmAccount:
    def __init__(self, name: str, resume: Mapping[str, Any] | None = None):
        self.name = name
        r = resume or {}
        self.cash = float(r.get("cash_sol", STARTING_SOL))
        self.ledger: list[dict[str, Any]] = list(r.get("ledger") or [])
        self.execution_latencies_ms: list[float] = [float(x) for x in r.get("execution_latencies_ms", [])][-5000:]

    def equity(self, active: Mapping[str, Any]) -> float:
        return self.cash + sum(finite(p.get("mark_remaining_value_sol")) for p in active.values() if p.get("arm") == self.name)

    def summary(self, active: Mapping[str, Any]) -> dict[str, Any]:
        rows = [r for r in self.ledger if r.get("status") == "CLOSED"]
        pnls = [finite(r.get("pnl_sol")) for r in rows]
        wins = sum(x > 0 for x in pnls); gains = sum(x for x in pnls if x > 0); losses = -sum(x for x in pnls if x < 0)
        peak = STARTING_SOL; dd = 0.0
        for r in rows:
            b = finite(r.get("balance_after_sol"), peak); peak = max(peak, b)
            if peak > 0: dd = max(dd, (peak-b)/peak)
        lat = self.execution_latencies_ms
        return {
            "starting_balance_sol": STARTING_SOL,
            "cash_sol": self.cash,
            "equity_sol": self.equity(active),
            "closed_trades": len(rows),
            "wins": wins,
            "losses": len(rows)-wins,
            "win_rate": wins/len(rows) if rows else None,
            "net_pnl_sol": sum(pnls),
            "roi_fraction": self.equity(active)/STARTING_SOL-1.0,
            "profit_factor": gains/losses if losses else (999.0 if gains else None),
            "expectancy_sol": sum(pnls)/len(rows) if rows else None,
            "maximum_drawdown_fraction": dd,
            "execution_latency_ms": {
                "count": len(lat), "median": percentile(lat,.50), "p95": percentile(lat,.95),
                "p99": percentile(lat,.99), "max": max(lat) if lat else None,
            },
        }

    def persistence(self) -> dict[str, Any]:
        return {"cash_sol": self.cash, "ledger": self.ledger, "execution_latencies_ms": self.execution_latencies_ms[-5000:]}


class Tournament:
    def __init__(self, evidence: v21.PriorEvidenceBook, resume: Mapping[str, Any] | None = None):
        r = resume or {}
        self.evidence = evidence
        self.accounts = {name: ArmAccount(name, (r.get("arms") or {}).get(name)) for name in v21.TOURNAMENT_ARMS}
        self.active: dict[str, dict[str, Any]] = {}
        self.post_exit: dict[str, dict[str, Any]] = {}
        self.signals = int(r.get("signals", 0))
        self.rejections = Counter(r.get("rejections") or {})
        self.execution_delay_ms = 6.0
        self._lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.task_errors: list[str] = []

    def cohort_closed(self) -> int:
        return min((len([r for r in a.ledger if r.get("status") == "CLOSED"]) for a in self.accounts.values()), default=0)

    def clean(self) -> bool:
        return not self.active and not self.post_exit and not self.tasks

    def persistence(self) -> dict[str, Any]:
        return {
            "version": VERSION, "paper_only": True, "starting_balance_sol": STARTING_SOL,
            "signals": self.signals, "rejections": dict(self.rejections),
            "arms": {k: v.persistence() for k, v in self.accounts.items()},
        }

    def summaries(self) -> dict[str, Any]:
        flat: dict[str, Any] = {}
        for bundle in self.active.values():
            for name, p in bundle["positions"].items(): flat[f"{bundle['mint']}:{name}"] = p
        return {name: acc.summary(flat) for name, acc in self.accounts.items()}

    def schedule(self, mint: str, decision: Mapping[str, Any], latest_states: dict[str, dict[str, Any]], delay_ms: float) -> None:
        self.signals += 1
        if mint in self.active or mint in self.post_exit:
            self.rejections["DUPLICATE"] += 1; return
        if len(self.active) >= MAX_CONCURRENT_SIGNALS:
            self.rejections["CONCURRENCY"] += 1; return
        task = asyncio.create_task(self._enter(mint, dict(decision), latest_states, delay_ms))
        self.tasks.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            self.tasks.discard(t)
            if not t.cancelled() and t.exception(): self.task_errors.append(repr(t.exception()))
        task.add_done_callback(done)

    async def _enter(self, mint: str, decision: dict[str, Any], latest_states: dict[str, dict[str, Any]], delay_ms: float) -> None:
        if delay_ms > 0: await asyncio.sleep(delay_ms/1000.0)
        async with self._lock:
            if len(self.active) >= MAX_CONCURRENT_SIGNALS:
                self.rejections["CONCURRENCY"] += 1; return
            state = latest_states.get(mint)
            if not valid_state(state): self.rejections["NO_VALID_ENTRY_STATE"] += 1; return
            entry_ns = time.time_ns()
            current_conv = v2.conviction(decision)
            enhanced = v21.enhanced_conviction(decision, self.evidence)
            tracker = v21.FlowTracker()
            positions: dict[str, dict[str, Any]] = {}
            for name in v21.TOURNAMENT_ARMS:
                conv = enhanced if name == "FLOW_V2_1" else current_conv
                tier = str(conv["conviction_tier"]); cfg = v2.TIERS[tier]
                acc = self.accounts[name]
                stake = min(max(0.0, acc.cash-core.RESERVE_SOL), acc.cash*cfg.position_fraction)
                if stake <= 0: raise RuntimeError(f"{name}:{mint}:insufficient cash")
                tokens, _ = core.quote_buy(stake, state)
                if tokens <= 0: raise RuntimeError(f"{name}:{mint}:no fill")
                acc.cash -= stake
                latency = (entry_ns-int(decision["decision_ns"]))/1_000_000.0
                acc.execution_latencies_ms.append(latency); acc.execution_latencies_ms = acc.execution_latencies_ms[-5000:]
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
                    "entry_ns": entry_ns, "entry_state_ns": int(state.get("ns",0)),
                    "create_ns": int(decision["create_ns"]), "decision_ns": int(decision["decision_ns"]),
                    "create_to_decision_ms": (int(decision["decision_ns"])-int(decision["create_ns"]))/1e6,
                    "decision_to_entry_ms": latency, "create_to_entry_ms": (entry_ns-int(decision["create_ns"]))/1e6,
                    "entry_reason": dict(decision), "guardian": guardian, "actions": [], "path": [],
                    "mfe_fraction": immediate/stake-1.0, "mae_fraction": immediate/stake-1.0,
                }
            self.active[mint] = {
                "mint": mint, "decision": decision, "entry_ns": entry_ns,
                "tracker": tracker, "positions": positions, "last_state_ns": -1,
                "e4_events": [],
            }
        await self._manage(mint, latest_states)

    async def _manage(self, mint: str, latest_states: dict[str, dict[str, Any]]) -> None:
        while mint in self.active:
            await asyncio.sleep(0.004)
            async with self._lock:
                bundle = self.active.get(mint)
                if not bundle: return
                state = latest_states.get(mint)
                if not valid_state(state):
                    age = (time.time_ns()-int(bundle["entry_ns"]))/1e9
                    if age > 25: self.task_errors.append(f"{mint}:UNRESOLVED_MARKET_STATE")
                    continue
                elapsed = (time.time_ns()-int(bundle["entry_ns"]))/1e6
                state_ns = int(state.get("ns",0))
                timer_due = any(self._timer_due(p, elapsed) for p in bundle["positions"].values() if p.get("status") == "OPEN")
                if state_ns == int(bundle["last_state_ns"]) and not timer_due: continue
                bundle["last_state_ns"] = state_ns
                for p in list(bundle["positions"].values()):
                    if p.get("status") == "OPEN": self._apply_mark(bundle, p, state, elapsed)
                if all(p.get("status") == "CLOSED" for p in bundle["positions"].values()):
                    self._finish_bundle(bundle, state)

    def _timer_due(self, p: Mapping[str, Any], elapsed: float) -> bool:
        g = p.get("guardian")
        if isinstance(g, v21.FullTwoSecondGuardian): return elapsed >= v21.ANCHOR_MS and not g.closed
        if isinstance(g, v21.TwoSecondPartialsGuardian):
            return (not g.initial_done and elapsed >= g.config.initial_target_ms) or elapsed >= v21.ANCHOR_MS
        if isinstance(g, v21.FlowAwareGuardian):
            return (not g.initial_done and elapsed >= g.config.initial_target_ms) or elapsed >= v21.ANCHOR_MS or elapsed >= g.config.max_hold_ms
        if isinstance(g, v2.RunnerGuardian):
            return (not g.initial_done and elapsed >= g.config.initial_target_ms) or elapsed >= g.config.max_hold_ms
        return False

    def ingest_event(self, mint: str, event: Any, state: Mapping[str, Any] | None) -> None:
        bundle = self.active.get(mint)
        if not bundle: return
        trader = core.event_trader(event)
        if trader == core.E4_WALLET:
            bundle["e4_events"].append({
                "t_ms": (core.event_ns(event)-int(bundle["entry_ns"]))/1e6,
                "kind": core.event_kind(event), "sol_amount": finite(getattr(event,"sol_amount",0.0)),
                "token_amount": finite(getattr(event,"token_amount",0.0)),
            })
        kind = core.event_kind(event)
        if kind not in {"BUY","SELL","PUMPSWAP_BUY","PUMPSWAP_SELL"}: return
        t_ms = (core.event_ns(event)-int(bundle["entry_ns"]))/1e6
        if t_ms < 0: return
        flow_pos = bundle["positions"].get("FLOW_V2_1")
        if not flow_pos or not state or not valid_state(state): return
        full_mark = core.quote_sell(float(flow_pos["original_tokens"]), state)
        ret = full_mark/float(flow_pos["stake_sol"])-1.0
        raw = getattr(event,"raw",{}) if isinstance(getattr(event,"raw",{}),Mapping) else {}
        sol_amount = finite(getattr(event,"sol_amount",0.0))
        token_amount = finite(getattr(event,"token_amount",0.0))
        if sol_amount <= 0: sol_amount = core.normal_sol(raw.get("sol_amount",0.0))
        if token_amount <= 0: token_amount = core.normal_tokens(raw.get("token_amount",0.0))
        bundle["tracker"].ingest(v21.FlowEvent(
            t_ms=t_ms, kind=kind, trader=trader, sol_amount=sol_amount, token_amount=token_amount,
            return_fraction=ret, is_creator=(trader == str(bundle["decision"].get("creator") or "")),
            vsol=finite(state.get("vsol")), vtok=finite(state.get("vtok")), views=None,
        ))

    def _apply_mark(self, bundle: dict[str, Any], p: dict[str, Any], state: Mapping[str, Any], elapsed: float) -> None:
        stake = float(p["stake_sol"]); original = float(p["original_tokens"]); remain = float(p["remaining_tokens"])
        full_mark = core.quote_sell(original, state); ret = full_mark/stake-1.0
        p["mfe_fraction"] = max(float(p["mfe_fraction"]), ret); p["mae_fraction"] = min(float(p["mae_fraction"]), ret)
        p["mark_remaining_value_sol"] = core.quote_sell(remain,state) if remain > 0 else 0.0
        if len(p["path"]) < 3000:
            p["path"].append({"t_ms":elapsed,"return_fraction":ret,"remaining_fraction":remain/original if original else 0.0,
                              "state_ns":int(state.get("ns",0)),"vsol":finite(state.get("vsol")),"vtok":finite(state.get("vtok"))})
        action = p["guardian"].on_mark(v2.Mark(elapsed, ret, int(state.get("ns",0)), finite(state.get("vsol")), finite(state.get("vtok"))))
        if action: self._execute_action(p, state, action)

    def _execute_action(self, p: dict[str, Any], state: Mapping[str, Any], action: v2.Action) -> None:
        original = float(p["original_tokens"]); remain = float(p["remaining_tokens"])
        amount = remain if action.kind == "EXIT" else min(remain, original*float(action.fraction_of_entry))
        if amount <= 0: return
        proceeds = core.quote_sell(amount,state); acc = self.accounts[str(p["arm"])]
        acc.cash += proceeds; p["realized_proceeds_sol"] += proceeds; p["remaining_tokens"] = max(0.0, remain-amount)
        p["mark_remaining_value_sol"] = core.quote_sell(float(p["remaining_tokens"]),state) if p["remaining_tokens"] > 0 else 0.0
        p["actions"].append({"kind":action.kind,"reason":action.reason,"t_ms":action.t_ms,"mark_return":action.mark_return,
                             "fraction_of_entry":amount/original if original else 0.0,"tokens_sold":amount,
                             "proceeds_sol":proceeds,"remaining_fraction":float(p["remaining_tokens"])/original if original else 0.0,
                             "state_ns":int(state.get("ns",0))})
        if action.kind == "EXIT" or p["remaining_tokens"] <= max(1e-9, original*1e-9): self._close_arm(p,state,action.reason)

    def _close_arm(self, p: dict[str, Any], state: Mapping[str, Any], reason: str) -> None:
        if p.get("remaining_tokens",0) > 0:
            tail = core.quote_sell(float(p["remaining_tokens"]),state); self.accounts[str(p["arm"])].cash += tail; p["realized_proceeds_sol"] += tail
            p["remaining_tokens"] = 0.0; p["mark_remaining_value_sol"] = 0.0
        stake = float(p["stake_sol"]); pnl = float(p["realized_proceeds_sol"])-stake; exit_ns = time.time_ns()
        p["status"] = "CLOSED"; p["exit_ns"] = exit_ns; p["exit_reason"] = reason; p["exit_state_ns"] = int(state.get("ns",0))
        p["actual_hold_ms"] = (exit_ns-int(p["entry_ns"]))/1e6; p["proceeds_sol"] = float(p["realized_proceeds_sol"])
        p["pnl_sol"] = pnl; p["return_fraction"] = pnl/stake if stake else None; p["win"] = pnl > 0
        p["post_exit"] = {"window_seconds":POST_EXIT_SECONDS,"exit_full_position_return":core.quote_sell(float(p["original_tokens"]),state)/stake-1.0,
                          "max_return_fraction":None,"min_return_fraction":None,"went_higher_than_exit":False,"went_lower_than_exit":False,"complete":False}

    def _finish_bundle(self, bundle: dict[str, Any], state: Mapping[str, Any]) -> None:
        mint = str(bundle["mint"])
        for name,p in bundle["positions"].items():
            row = {k:v for k,v in p.items() if k != "guardian"}
            if isinstance(p.get("guardian"), v21.FlowAwareGuardian): row["flow_decisions"] = list(p["guardian"].decisions)
            row["e4_same_coin_events"] = list(bundle["e4_events"])
            self.accounts[name].ledger.append(row)
        for name,acc in self.accounts.items():
            acc.ledger[-1]["balance_after_sol"] = acc.cash
        self.active.pop(mint,None)
        self.post_exit[mint] = {"deadline_ns":time.time_ns()+int(POST_EXIT_SECONDS*1e9),"rows":{name:self.accounts[name].ledger[-1] for name in v21.TOURNAMENT_ARMS}}

    def observe_post_exit(self, mint: str, state: Mapping[str, Any]) -> None:
        t = self.post_exit.get(mint)
        if not t or not valid_state(state): return
        for name,row in t["rows"].items():
            mark = core.quote_sell(float(row["original_tokens"]),state); ret = mark/float(row["stake_sol"])-1.0; post=row["post_exit"]
            post["max_return_fraction"] = ret if post["max_return_fraction"] is None else max(float(post["max_return_fraction"]),ret)
            post["min_return_fraction"] = ret if post["min_return_fraction"] is None else min(float(post["min_return_fraction"]),ret)
            exit_ret = finite(post.get("exit_full_position_return")); post["went_higher_than_exit"] |= ret > exit_ret+1e-12; post["went_lower_than_exit"] |= ret < exit_ret-1e-12
        if time.time_ns() >= int(t["deadline_ns"]):
            for row in t["rows"].values(): row["post_exit"]["complete"] = True
            self.post_exit.pop(mint,None)

    def finalize_post_exit(self) -> None:
        now=time.time_ns()
        for mint,t in list(self.post_exit.items()):
            if now >= int(t["deadline_ns"]):
                for row in t["rows"].values(): row["post_exit"]["complete"] = True
                self.post_exit.pop(mint,None)


def read_resume(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists(): return {}
    d=json.loads(path.read_text(encoding="utf-8"))
    if d.get("version") != VERSION or d.get("paper_only") is not True or float(d.get("starting_balance_sol",0)) != STARTING_SOL:
        raise RuntimeError("tournament resume mismatch")
    return d


def status_payload(started: float, counts: Counter, t: Tournament, queue: asyncio.Queue[Any], endpoints: Mapping[str,Any], accepting: bool, terminal: str | None=None) -> dict[str,Any]:
    return {
        "version":VERSION,"paper_only":True,"real_money_execution":False,"historical_replay":False,
        "market_data_mode":"LIVE_SOLANA_WEBSOCKET_FRESH_LAUNCHES_ONLY","status":terminal or "RUNNING",
        "started_at":started,"checked_at":time.time(),"elapsed_seconds":time.time()-started,
        "accepting_new_entries":accepting,"counts":dict(counts),"signals":t.signals,"cohort_closed":t.cohort_closed(),
        "target_cohort_closed":100,"rejections":dict(t.rejections),"task_errors":list(t.task_errors),
        "active_signals":len(t.active),"post_exit_pending":len(t.post_exit),"queue_depth":queue.qsize(),
        "arms":t.summaries(),"endpoints":endpoints,
        "e4_mode":"OBSERVATIONAL_ONLY_SAME_COIN_EVENTS_WHEN_PRESENT",
    }


async def run(args: argparse.Namespace) -> int:
    prod_root=args.production_root.resolve(); sys.path.insert(0,str(prod_root/"src"))
    prod=live_v1.load_module("golden_v21_prod",prod_root/"scripts"/"e4_live_market_stress.py")
    resume=read_resume(args.resume_state); rep=core.initialise_reputation_from_corpus(args.corpus); evidence=v21.PriorEvidenceBook.from_frozen_corpus(args.corpus)
    delta_appear=Counter(); delta_wins=Counter()
    for w,pair in (resume.get("reputation_delta") or {}).items(): rep.appear[w]+=int(pair[0]); rep.wins[w]+=int(pair[1]); delta_appear[w]+=int(pair[0]); delta_wins[w]+=int(pair[1])
    tournament=Tournament(evidence,resume); tournament.execution_delay_ms=args.execution_delay_ms
    latest_states:dict[str,dict[str,Any]]={}; launches:dict[str,dict[str,Any]]={}; queue:asyncio.Queue[Mapping[str,Any]]=asyncio.Queue(maxsize=40000)
    stop=asyncio.Event(); endpoints:dict[str,Any]={}; dedupe:set[tuple[str,int,str]]=set(); counts:Counter=Counter(); background:set[asyncio.Task[Any]]=set(); bg_errors:list[str]=[]
    started=time.time(); session_started_ns=time.time_ns(); accepting=True; hard_deadline=time.monotonic()+args.duration_seconds; drain_deadline:float|None=None; last_checkpoint=0.0

    def add_task(coro:Any)->None:
        task=asyncio.create_task(coro); background.add(task)
        def done(x:asyncio.Task[Any])->None:
            background.discard(x)
            if not x.cancelled() and x.exception(): bg_errors.append(repr(x.exception()))
        task.add_done_callback(done)

    def checkpoint(terminal:str|None=None)->None:
        tournament.finalize_post_exit(); payload=status_payload(started,counts,tournament,queue,endpoints,accepting,terminal)
        if bg_errors: payload["background_errors"]=bg_errors[-20:]
        atomic_json(args.status,payload)
        state=tournament.persistence(); state["reputation_delta"]={w:[delta_appear[w],delta_wins[w]] for w in delta_appear}; state["updated_at"]=time.time(); atomic_json(args.state_output,state)

    async def settle_rep(mint:str,buyers:list[str],decision_state:dict[str,Any]|None)->None:
        await asyncio.sleep(core.HOLD_MS/1000.0); exit_state=latest_states.get(mint)
        if not valid_state(decision_state) or not valid_state(exit_state): counts["benchmark_skipped"]+=1; return
        tok,_=core.quote_buy(core.BENCHMARK_STAKE_SOL,decision_state)
        if tok<=0: counts["benchmark_skipped"]+=1; return
        won=core.quote_sell(tok,exit_state)-core.BENCHMARK_STAKE_SOL>0
        for w in dict.fromkeys(buyers): rep.appear[w]+=1; rep.wins[w]+=int(won); delta_appear[w]+=1; delta_wins[w]+=int(won)
        counts["benchmark_outcomes"]+=1; counts["benchmark_wins"]+=int(won)

    async def decide(mint:str)->None:
        launch=launches.get(mint)
        if not launch:return
        deadline=int(launch["create_ns"])+core.DECISION_MS*1_000_000; delay=max(0.0,(deadline-time.time_ns())/1e9)
        if delay: await asyncio.sleep(delay)
        launch=launches.get(mint)
        if not launch:return
        create_ns=int(launch["create_ns"]); buyers=core.unique_early_buyers(launch["events"],str(launch["creator"]),create_ns,core.DECISION_MS)
        counts["decisions"]+=1; counts["early_buyers_seen"]+=len(buyers); qualifies,n,w,rate=rep.qualifies(buyers); state=latest_states.get(mint); decision_ns=time.time_ns()
        create_state=launch.get("create_state") or {}; early_vsol_delta=(finite((state or {}).get("vsol"))-finite(create_state.get("vsol"))) if state and create_state else 0.0
        decision={"mint":mint,"create_ns":create_ns,"decision_ns":decision_ns,"buyers":buyers,"buyer_count":len(buyers),"prior_appearances":n,"prior_wins":w,"prior_win_rate":rate,
                  "qualifies":qualifies,"creator":str(launch["creator"]),"early_vsol_delta":early_vsol_delta}
        counts["qualified"]+=int(qualifies); add_task(settle_rep(mint,buyers,dict(state) if state else None))
        if qualifies:
            if accepting:tournament.schedule(mint,decision,latest_states,args.execution_delay_ms)
            else:counts["qualified_after_cutoff"]+=1

    workers=[asyncio.create_task(live_v1.endpoint_worker(url,prod,queue,stop,endpoints)) for url in dict.fromkeys(args.ws_url or prod.DEFAULT_WS_RPCS)]
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        try:loop.add_signal_handler(sig,stop.set)
        except NotImplementedError:pass
    checkpoint()
    try:
        while not stop.is_set():
            now=time.monotonic()
            if accepting and (now>=hard_deadline or tournament.cohort_closed()>=args.target_closed_trades):
                accepting=False; drain_deadline=now+DRAIN_SECONDS; counts["entry_cutoff_reached"]+=1
            if not accepting:
                tournament.finalize_post_exit()
                if not tournament.active and not tournament.post_exit and not tournament.tasks: break
                if drain_deadline is not None and now>=drain_deadline: tournament.task_errors.append("CLEAN_DRAIN_TIMEOUT"); break
            try:payload=await asyncio.wait_for(queue.get(),timeout=.20)
            except asyncio.TimeoutError:payload=None
            if payload is not None:
                incoming:list[Any]=[]; prod.decode_log_payload(payload,incoming,dedupe)
                if incoming:
                    counts["decoded_events"]+=len(incoming); grouped:dict[str,list[Any]]={}
                    for event in incoming:
                        mint=str(event.mint); grouped.setdefault(mint,[]).append(event); state=core.state_from_event(event)
                        if state:
                            latest_states[mint]=state; tournament.observe_post_exit(mint,state)
                        tournament.ingest_event(mint,event,state)
                    for mint,events in grouped.items():
                        creates=[e for e in events if core.event_kind(e)=="CREATE"]
                        if creates and mint not in launches and accepting:
                            create=creates[0]; create_ns=core.event_ns(create)
                            if create_ns<session_started_ns:counts["rejected_not_fresh"]+=1;continue
                            raw=getattr(create,"raw",{}) if isinstance(getattr(create,"raw",{}),Mapping) else {}
                            if not core.native_quote(raw):counts["unsupported_quote"]+=1;continue
                            launches[mint]={"create_ns":create_ns,"creator":core.event_creator(create),"events":[],"create_state":core.state_from_event(create)}; counts["launches_seen"]+=1; add_task(decide(mint))
                        if mint in launches:
                            cutoff=int(launches[mint]["create_ns"])+60_000_000_000; launches[mint]["events"].extend(e for e in events if core.event_ns(e)<=cutoff)
            tournament.finalize_post_exit(); now=time.monotonic()
            if now-last_checkpoint>=args.heartbeat_seconds:
                checkpoint(); last_checkpoint=now; print("GOLDEN_V21_TOURNAMENT "+json.dumps({"counts":dict(counts),"cohort":tournament.cohort_closed(),"arms":tournament.summaries()},separators=(",",":")),flush=True)
            if bg_errors or tournament.task_errors: stop.set()
    finally:
        stop.set()
        for w in workers:w.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        if background:
            try:await asyncio.wait_for(asyncio.gather(*list(background),return_exceptions=True),timeout=3)
            except asyncio.TimeoutError:pass
        if tournament.tasks:
            try:await asyncio.wait_for(asyncio.gather(*list(tournament.tasks),return_exceptions=True),timeout=3)
            except asyncio.TimeoutError:pass
    tournament.finalize_post_exit()
    if tournament.active or tournament.post_exit:terminal="TECHNICAL_FAILURE_EXPOSURE";rc=2
    elif bg_errors or tournament.task_errors:terminal="TECHNICAL_FAILURE";rc=2
    elif tournament.cohort_closed()>=args.target_closed_trades:terminal="SAMPLE_TARGET_REACHED";rc=0
    else:terminal="TIME_LIMIT_REACHED";rc=3
    checkpoint(terminal); final=json.loads(args.status.read_text()); final["requirement_met"]=tournament.cohort_closed()>=args.target_closed_trades; atomic_json(args.output,final); return rc


def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--production-root",type=Path,required=True); p.add_argument("--corpus",type=Path,required=True); p.add_argument("--resume-state",type=Path)
    p.add_argument("--state-output",type=Path,required=True); p.add_argument("--status",type=Path,required=True); p.add_argument("--output",type=Path,required=True); p.add_argument("--starting-balance-sol",type=float,default=3.0)
    p.add_argument("--target-closed-trades",type=int,default=100); p.add_argument("--duration-seconds",type=float,default=15900); p.add_argument("--heartbeat-seconds",type=float,default=30); p.add_argument("--execution-delay-ms",type=float,default=6.0); p.add_argument("--ws-url",action="append")
    a=p.parse_args();
    if not math.isclose(a.starting_balance_sol,STARTING_SOL): raise SystemExit("tournament is frozen to 3 SOL per paper arm")
    return asyncio.run(run(a))


if __name__=="__main__": raise SystemExit(main())
