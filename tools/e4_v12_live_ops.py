#!/usr/bin/env python3
"""Supervised, thesis-frozen ONLINE paper trading. Never signs/broadcasts orders.

The original thresholds are immutable. Historical data is used only to restore
the frozen model and priors, not to search/requalify or manufacture live results.
"""
from __future__ import annotations
import argparse, asyncio, gzip, hashlib, json, math, os, signal, statistics, sys, time
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import aiohttp
import numpy as np
from sklearn.neighbors import NearestNeighbors
from threadpoolctl import threadpool_limits
import e4_v12_dual_prototype_online_live as legacy

VERSION = "e4-v12-supervised-online-v2"
WSOL = "So11111111111111111111111111111111111111112"
CANDIDATES = legacy.CANDIDATES
CONFIG_HASH = hashlib.sha256(json.dumps(CANDIDATES, sort_keys=True).encode()).hexdigest()
ROOT = Path(__file__).resolve().parent
MINT_CURVES = {}
RESERVE_REFRESHER = None

def write_json(path, value):
    legacy.atomic_json(Path(path), value)

class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
    def append(self, kind, payload):
        with (self.directory / (kind + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
            f.flush()

class FastHead:
    """Same Euclidean neighbours, but no thread oversubscription/repeated sorting."""
    def __init__(self, representation, train, target, k):
        self.positive = NearestNeighbors(n_neighbors=k, n_jobs=1).fit(representation[train & target])
        self.negative = NearestNeighbors(n_neighbors=k, n_jobs=1).fit(representation[train & ~target])
        self.reference = self.raw(representation)
        self.sorted_reference = np.sort(self.reference, kind="stable")
    def raw(self, matrix):
        pd = self.positive.kneighbors(matrix)[0].mean(axis=1)
        nd = self.negative.kneighbors(matrix)[0].mean(axis=1)
        return np.log1p(nd) - np.log1p(pd)
    def percentile(self, vector):
        value = float(self.raw(vector.reshape(1, -1))[0])
        i = np.searchsorted(self.sorted_reference, value, side="right") - 1
        return float(np.clip(i / max(1, len(self.reference)-1), 0, 1))

def restore_frozen(base, cstream, path):
    """Load only the two fixed policies, and prime all pre-live known priors."""
    fields = list(base.BASE_NUMERIC) + [f"{s}_0ms" for s in base.WINDOW_STEMS]
    keep = set(fields) | {"mint", "split", "selected_by_e4", "create_ns"}
    policies = sorted({c["policy"] for c in CANDIDATES.values()})
    keep |= {base.policy_pnl_key(l,p) for l in base.LATENCIES for p in policies}
    history = cstream.CausalHistory()
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            rows.append({k:r.get(k) for k in keep})
            history.enrich([r])
    if len(rows) != 66000:
        raise ValueError("frozen corpus row-count mismatch")
    train = np.array([r["split"] == "train" for r in rows])
    medians = {}
    for field in fields:
        vals = [legacy.finite(r.get(field),float("nan")) for r,b in zip(rows,train)
                if b and r.get(field) is not None]
        vals = [v for v in vals if math.isfinite(v)]
        medians[field] = statistics.median(vals) if vals else 0.
    x = np.array([[v for f in fields for v in
                   (legacy.finite(r.get(f),medians[f]) if r.get(f) is not None else medians[f],
                    float(r.get(f) is None))] for r in rows],dtype=np.float32)
    hist = SimpleNamespace(rows=rows, x_general=x, splits=np.array([r["split"] for r in rows]),
                           selected=np.array([bool(r["selected_by_e4"]) for r in rows]),
                           pnl={p:np.array([[r[base.policy_pnl_key(l,p)] for l in base.LATENCIES]
                                           for r in rows],dtype=np.float64) for p in policies})
    legacy.DistanceHead = FastHead
    scorer = legacy.FrozenPrototypeScorer(base, lambda *_:hist, path)
    # A SOFTWARE contract check, not another performance test/search.
    for i in (0, 25796, 28392, 35534, 65999):
        actual = scorer.vector(hist.rows[i])
        if not np.allclose(actual, scorer.historical_representation[i],atol=2e-4,rtol=2e-4):
            raise RuntimeError("batch/single input-vector parity failed")
        if not all(math.isfinite(s) and 0<=s<=1 for s in scorer.score(hist.rows[i]).values()):
            raise RuntimeError("nonfinite/out-of-range score in scorer contract")
    # Same frozen feature names must be present on EVERY online row.
    return scorer, history

class PriceFeed:
    def __init__(self, prod, stale_seconds=300):
        self.prod, self.stale_seconds = prod, stale_seconds
        self.value = None
        self.updated = 0.
        self.source = None
        self.errors = deque(maxlen=10)
    def set_quote(self, value, source, now=None):
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid live SOL/USD")
        self.value, self.source = value, source
        self.updated = time.time() if now is None else now
        self.prod.hardening._SOL_USD = value
    def fresh(self):
        return self.value is not None and time.time()-self.updated <= self.stale_seconds
    def summary(self):
        return {"sol_usd":self.value,"source":self.source,
                "age_seconds":None if not self.updated else round(time.time()-self.updated,2),
                "fresh":self.fresh(),"fallback_used":False,"recent_errors":list(self.errors)}
    async def refresh(self, session):
        sources = [
            ("coinbase","https://api.coinbase.com/v2/prices/SOL-USD/spot"),
            ("coingecko","https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_last_updated_at=true"),
        ]
        for name,url in sources:
            try:
                async with session.get(url,timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    resp.raise_for_status()
                    p = await resp.json()
                if name == "coinbase":
                    if p["data"].get("base","SOL") != "SOL" or p["data"].get("currency") != "USD":
                        raise ValueError("wrong quote currency")
                    value = p["data"]["amount"]
                else:
                    if time.time()-float(p["solana"]["last_updated_at"]) > self.stale_seconds:
                        raise ValueError("stale provider quote")
                    value = p["solana"]["usd"]
                self.set_quote(value,name)
                return True
            except Exception as exc:
                self.errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
        return False
    async def loop(self, stop):
        async with aiohttp.ClientSession() as session:
            while not stop.is_set():
                await self.refresh(session)
                try: await asyncio.wait_for(stop.wait(),timeout=30)
                except asyncio.TimeoutError: pass

class Account:
    def __init__(self,name,build,states,price,evidence,delay_ms=10,target=20,max_age=2):
        self.name,self.config,self.build,self.states,self.price,self.evidence=name,CANDIDATES[name],build,states,price,evidence
        self.delay_ms,self.target,self.max_age=delay_ms,target,max_age
        self.cash=2.; self.signals=0; self.active={}; self.pending=set()
        self.ledger=[]; self.rejections=Counter(); self.tasks=set(); self.errors=[]
        self.entries_paused=False
    def equity(self):
        return self.cash + sum(p["stake_sol"] for p in self.active.values())
    def checkpoint(self):
        write_json(self.evidence.directory/(self.name+"-account.json"),
                   {"summary":self.summary(),"ledger":self.ledger,"active":self.active,
                    "pending":list(self.pending),"config_hash":CONFIG_HASH})
    def quote_state(self,mint):
        state=self.states.get(mint)
        if state is None: raise ValueError("NO_RESERVE_STATE")
        if time.time_ns()-state["ns"] > self.max_age*1e9: raise ValueError("STALE_RESERVE_STATE")
        if state.get("unsupported_quote"): raise ValueError("UNSUPPORTED_QUOTE")
        if state.get("complete"): raise ValueError("MIGRATED_ROUTE_UNSUPPORTED")
        if not all(math.isfinite(float(state.get(k,0))) and state.get(k,0)>0 for k in ("vsol","vtok")):
            raise ValueError("INVALID_RESERVES")
        return dict(state)
    async def resolve_state(self,mint):
        try:
            return self.quote_state(mint)
        except ValueError as exc:
            if str(exc) not in {"NO_RESERVE_STATE","STALE_RESERVE_STATE"} or RESERVE_REFRESHER is None:
                raise
            self.states[mint] = await RESERVE_REFRESHER(mint,self.states.get(mint))
            return self.quote_state(mint)
    def reject(self,mint,reason):
        self.rejections[reason]+=1
        self.evidence.append("execution",dict(thesis=self.name,mint=mint,kind="REJECT",reason=reason,at_ns=time.time_ns()))
        self.checkpoint()
    def schedule(self,mint,score,received_ns,signal_state):
        self.signals+=1
        if self.entries_paused: self.reject(mint,"OPERATIONS_PAUSED");return
        if len(self.ledger)>=self.target: self.reject(mint,"SAMPLE_TARGET_REACHED");return
        if mint in self.pending or mint in self.active: self.reject(mint,"DUPLICATE");return
        if len(self.active)+len(self.pending)>=2: self.reject(mint,"CONCURRENCY");return
        self.pending.add(mint)
        task=asyncio.create_task(self.trade(mint,score,received_ns,signal_state))
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if t.cancelled(): self.errors.append("TRADE_TASK_CANCELLED")
            elif t.exception(): self.errors.append(repr(t.exception()))
        task.add_done_callback(done)
    async def trade(self,mint,score,received_ns,signal_state):
        signal_ns=time.time_ns()
        try:
            await asyncio.sleep(self.delay_ms/1000.)
            if not self.price.fresh(): self.reject(mint,"STALE_SOL_USD");return
            state=await self.resolve_state(mint)
            stake=min(max(0.,self.cash-.03),self.equity()*.0185)
            if stake<=0: self.reject(mint,"INSUFFICIENT_CASH");return
            if state.get("rtok") is None or not math.isfinite(state["rtok"]) or state["rtok"]<=0:
                self.reject(mint,"NO_REAL_TOKEN_LIQUIDITY");return
            tokens,_=self.build.quote_buy(stake,state)
            expected,_=self.build.quote_buy(stake,signal_state)
            if tokens<=0: self.reject(mint,"NO_FILL");return
            if expected>0 and tokens < expected*.92: self.reject(mint,"OUTPUT_SHORTFALL");return
            now=time.time_ns();hold=int(self.config["policy"].removeprefix("hold_").removesuffix("ms"))
            p=dict(mint=mint,score=score,received_ns=received_ns,signal_ns=signal_ns,entry_ns=now,
                   entry_state_ns=state["ns"],entry_state_age_ms=(now-state["ns"])/1e6,
                   create_to_entry_ms=(now-received_ns)/1e6,signal_to_entry_ms=(now-signal_ns)/1e6,
                   stake_sol=stake,tokens=tokens,hold_ms=hold,entry_sol_usd=self.price.value)
            self.cash-=stake;self.active[mint]=p
            self.evidence.append("execution",dict(p,thesis=self.name,kind="PAPER_ENTRY"))
            self.checkpoint()
            await asyncio.sleep(hold/1000.)
            try:
                exit_state=await self.resolve_state(mint)
                proceeds=self.build.quote_sell(tokens,exit_state)
                pnl=proceeds-stake
                self.cash+=proceeds;self.active.pop(mint)
                exit_ns=time.time_ns()
                self.ledger.append(dict(p,exit_ns=exit_ns,exit_state_ns=exit_state["ns"],exit_quote_source=exit_state.get("quote_source","stream"),
                                        actual_hold_ms=(exit_ns-now)/1e6,proceeds_sol=proceeds,pnl_sol=pnl,
                                        win=pnl>0,balance_after_sol=self.equity(),status="CLOSED"))
                self.evidence.append("execution",dict(self.ledger[-1],thesis=self.name,kind="PAPER_EXIT"))
            except ValueError as exc:
                # Do not invent an exit or count an unexecutable quote as a trade.
                self.active[mint]["status"]="UNRESOLVED_EXIT"
                self.errors.append(str(exc))
                self.evidence.append("execution",dict(thesis=self.name,mint=mint,kind="UNRESOLVED_EXIT",reason=str(exc),at_ns=time.time_ns()))
            self.checkpoint()
        except ValueError as exc:
            self.reject(mint,str(exc))
        finally:
            self.pending.discard(mint)
    def summary(self):
        pnls=[r["pnl_sol"] for r in self.ledger];wins=sum(p>0 for p in pnls)
        gross=sum(p for p in pnls if p>0);loss=-sum(p for p in pnls if p<0)
        peak=2.;dd=0.
        for r in self.ledger:
            peak=max(peak,r["balance_after_sol"]);dd=max(dd,(peak-r["balance_after_sol"])/peak)
        return dict(signals=self.signals,closed_trades=len(pnls),wins=wins,losses=sum(p<0 for p in pnls),
                    breakeven=sum(p==0 for p in pnls),win_rate=wins/len(pnls) if pnls else None,
                    net_pnl_sol=sum(pnls),ending_bankroll_sol=self.equity(),cash_sol=self.cash,
                    locked_principal_sol=sum(p["stake_sol"] for p in self.active.values()),
                    profit_factor=gross/loss if loss else None,roi_fraction=sum(pnls)/2,
                    maximum_drawdown_fraction=dd,drawdown_basis="realized_equity_plus_locked_principal",active_positions=len(self.active),pending_entries=len(self.pending),
                    rejections=dict(self.rejections),task_errors=list(self.errors),
                    unpriced_exposure=any(p.get("status")=="UNRESOLVED_EXIT" for p in self.active.values()))

def native_quote(raw):
    return raw.get("quote_mint") in (None,"",WSOL,"11111111111111111111111111111111")

def checked_state(event,build):
    raw=event.raw
    if raw.get("bonding_curve"):MINT_CURVES[event.mint]=raw["bonding_curve"]
    if getattr(event,"complete",False) or getattr(event,"kind","")=="MIGRATION":
        return dict(ns=int(event.received_ns),complete=True,quote_source="stream",vsol=0,vtok=0,rtok=0)
    state=legacy.state_from_live_event(event,build)
    if state:
        raw=event.raw
        state["rtok"]=None if raw.get("real_token_reserves") is None else build.normal_tokens(raw["real_token_reserves"])
        state["unsupported_quote"]=not native_quote(raw)
    return state


async def refresh_curve(mint, previous, prod, build):
    """Request a current native curve only when the streamed quote is stale."""
    from memecoin_bot.realtime.pumpfun import decode_account_data, decode_bonding_curve_account
    curve=MINT_CURVES.get(mint)
    if not curve:raise ValueError("NO_CURVE_ADDRESS_FOR_FRESH_QUOTE")
    config={"encoding":"base64","commitment":"processed"}
    if previous and previous.get("slot",-1)>=0:config["minContextSlot"]=int(previous["slot"])
    errors=[]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        for url in prod.DEFAULT_HTTP_RPCS[:2]:
            try:
                async with session.post(url,json={"jsonrpc":"2.0","id":1,"method":"getAccountInfo",
                                                  "params":[curve,config]}) as r:
                    r.raise_for_status();payload=await r.json()
                if payload.get("error"):raise ValueError("RPC_ACCOUNT_ERROR")
                result=payload["result"];value=result["value"]
                if not value or value.get("owner")!=prod.PUMP_PROGRAM_ID:raise ValueError("CURVE_OWNER_OR_ACCOUNT_INVALID")
                raw=decode_bonding_curve_account(decode_account_data(value["data"]))
                if not raw.get("quote_is_sol"):raise ValueError("NON_SOL_CURVE")
                vsol=build.normal_sol(raw["virtual_sol_reserves"])
                vtok=build.normal_tokens(raw["virtual_token_reserves"])
                if vsol<=0 or vtok<=0:raise ValueError("INVALID_RPC_RESERVES")
                price=vsol/vtok
                return dict(ns=time.time_ns(),slot=result["context"]["slot"],vsol=vsol,vtok=vtok,
                    rtok=build.normal_tokens(raw["real_token_reserves"]),price=price,
                    fdv=price*float(raw["token_total_supply"])/1e6*prod.hardening._SOL_USD,
                    complete=bool(raw["curve_complete"]),unsupported_quote=False,quote_source="fresh_rpc")
            except Exception as exc:errors.append(type(exc).__name__)
    raise ValueError("FRESH_CURVE_QUOTE_UNAVAILABLE:"+",".join(errors))


def validate_row(row,fields):
    missing=[f for f in fields if f not in row]
    invalid=[f for f in fields if row.get(f) is not None and not math.isfinite(legacy.finite(row[f],float("nan")))]
    if missing or invalid: raise ValueError(f"FEATURE_SCHEMA missing={missing} invalid={invalid}")
    for f in ("initial_virtual_sol","initial_virtual_tokens","create_price_sol","create_fdv_usd"):
        if row.get(f,0)<=0:raise ValueError("INVALID_CORE_FEATURE:"+f)

def make_row(create,incoming,build,history):
    # All inputs are a snapshot of the single already-received log notification.
    row=legacy.live_row_from_payload(create,incoming,build)
    history.enrich([row])
    return row

class Monitor:
    def __init__(self,names):
        self.counts={n:Counter() for n in names}
        self.scores={n:deque(maxlen=2000) for n in names}
        self.closest={n:[] for n in names};self.missing=Counter();self.max_score={n:None for n in names}
    def decision(self,name,row,score,reason,compute_ms):
        self.counts[name][reason]+=1
        if score is not None:
            if not math.isfinite(score) or not 0<=score<=1:raise ValueError("INVALID_SCORE")
            self.scores[name].append(score)
            self.max_score[name]=score if self.max_score[name] is None else max(score,self.max_score[name])
            self.closest[name]=sorted(self.closest[name]+[dict(mint=row["mint"],score=score,
                gap=CANDIDATES[name]["threshold"]-score)],key=lambda x:x["score"],reverse=True)[:5]
        return dict(thesis=name,mint=row["mint"],received_ns=row.get("create_ns"),
                    decided_ns=time.time_ns(),score=score,threshold=CANDIDATES[name]["threshold"],
                    reason=reason,compute_ms=compute_ms)
    def summary(self):
        return {n:dict(reasons=dict(self.counts[n]),score_max=self.max_score[n],
                       recent_score_p50=float(np.median(self.scores[n])) if self.scores[n] else None,
                       closest_misses=self.closest[n]) for n in self.counts}

async def endpoint(url,index,prod,queue,stop,counters):
    c=counters.setdefault(str(index),dict(messages=0,connections=0,disconnects=0,errors=[]))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_connect=8)) as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url,heartbeat=10,max_msg_size=8*1024*1024) as ws:
                    c["connections"]+=1
                    await ws.send_json({"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
                                        "params":[{"mentions":[prod.PUMP_PROGRAM_ID]},{"commitment":"processed"}]})
                    while not stop.is_set():
                        m=await asyncio.wait_for(ws.receive(),timeout=30)
                        if m.type==aiohttp.WSMsgType.TEXT:
                            received_ns=time.time_ns()
                            payload=json.loads(m.data)
                            if payload.get("error"):raise ValueError(str(payload["error"]))
                            c["messages"]+=1
                            await queue.put((received_ns,payload))
                        elif m.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("WebSocket closed")
            except asyncio.CancelledError:raise
            except Exception as exc:
                c["disconnects"]+=1;c["errors"]=(c["errors"]+[type(exc).__name__+":"+str(exc)[:120]])[-5:]
                await asyncio.sleep(min(5,0.25*c["disconnects"]))

async def online(args):
    out=Path(args.output_dir);evidence=Evidence(out)
    boot=time.time(); stop=asyncio.Event(); drained=asyncio.Event()
    loop=asyncio.get_running_loop()
    for signum in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(signum,stop.set)
    status_path=out/"status.json"
    write_json(status_path,dict(version=VERSION,status="PREPARING",checked_at=time.time(),started_at=boot,config_hash=CONFIG_HASH))
    sys.path.insert(0,str(Path(args.production_root).resolve()/"src"))
    sys.path.insert(0,str(Path(args.research_root).resolve()))
    prod=legacy.load_module("e4_v12_supervised_decoder",Path(args.production_root)/"scripts/e4_live_market_stress.py")
    from scripts import e4_v12_allout_launch_corpus as build
    from scripts import e4_v12_allout_launch_corpus_stream as cstream
    from scripts import e4_v12_allout_profit_hazard as base
    if legacy.sha256_path(Path(args.corpus))!=legacy.FROZEN_CORPUS_SHA256:
        raise RuntimeError("FROZEN_CORPUS_HASH_MISMATCH")
    threadpool_limits(2)
    scorer,history=await asyncio.to_thread(restore_frozen,base,cstream,Path(args.corpus))
    write_json(out/"freeze.json",dict(config_hash=CONFIG_HASH,config=CANDIDATES,
        corpus_sha256=legacy.FROZEN_CORPUS_SHA256,code_sha256=legacy.sha256_path(Path(__file__)),
        frozen_before_stream_at=time.time(),research_ref="1b5995a2f04b70c49071fba3c2d60e2f85d5d04d",
        paper_only=True,fee_bps=build.FEE_BPS,priority_and_tip_sol=build.PRIORITY_AND_TIP_SOL))
    global RESERVE_REFRESHER
    async def reserve_refresher(mint,previous):
        state=await refresh_curve(mint,previous,prod,build)
        # Never roll back a newer streamed slot received while RPC was in flight.
        current=states.get(mint)
        if current and current.get("slot",-1)>state.get("slot",-1):
            return dict(current)
        return state
    RESERVE_REFRESHER=reserve_refresher
    price=PriceFeed(prod)
    async with aiohttp.ClientSession() as session:
        if not await price.refresh(session):raise RuntimeError("NO_VERIFIED_SOL_USD")
    started=time.time();last_event=started;last_score=started;last_launch=started
    counts=Counter();states={};creators={};early_buyers={};selected=set()
    queue=asyncio.Queue(maxsize=20000);score_queue=asyncio.Queue(maxsize=128)
    accounts={n:Account(n,build,states,price,evidence,args.entry_delay_ms,args.target_closed_trades) for n in CANDIDATES}
    monitor=Monitor(CANDIDATES);endpoints={};seen=set();dedupe=set();failures=[];latencies=deque(maxlen=2000)
    workers=[asyncio.create_task(endpoint(url,i,prod,queue,stop,endpoints)) for i,url in enumerate(args.ws_url or prod.DEFAULT_WS_RPCS)]
    price_task=asyncio.create_task(price.loop(stop))
    reason="INSUFFICIENT_SAMPLE"
    async def score_worker():
        nonlocal last_score
        while not drained.is_set() or not score_queue.empty():
            try: row,state=await asyncio.wait_for(score_queue.get(),timeout=.25)
            except asyncio.TimeoutError:continue
            try:
                t=time.perf_counter()
                scores=await asyncio.to_thread(scorer.score,row)
                compute_ms=(time.perf_counter()-t)*1000
                last_score=time.time();latencies.append(compute_ms);counts["scored"]+=1
                decisions=[]
                for n,score in scores.items():
                    why="BELOW_THRESHOLD" if score<CANDIDATES[n]["threshold"] else "SIGNAL"
                    if not price.fresh():why="STALE_SOL_USD"
                    elif (time.time_ns()-row["create_ns"])/1e6>args.max_decision_age_ms:why="STALE_DECISION"
                    decisions.append(monitor.decision(n,row,score,why,compute_ms))
                    if why=="SIGNAL":accounts[n].schedule(row["mint"],score,row["create_ns"],state)
                evidence.append("decisions",dict(inputs={f:row.get(f) for f in scorer.fields},
                    price=price.summary(),decisions=decisions))
            except Exception as exc:
                failures.append("SCORER:"+repr(exc));stop.set()
            finally:score_queue.task_done()
    sc_task=asyncio.create_task(score_worker())
    def snapshot(final=False):
        return dict(version=VERSION,run_id=os.getenv("GITHUB_RUN_ID"),status=reason if final else "RUNNING",
            phase="online",checked_at=time.time(),started_at=started,elapsed_seconds=time.time()-started,
            config_hash=CONFIG_HASH,paper_only=True,no_retuning=True,
            last_event_at=last_event,last_launch_at=last_launch,last_score_at=last_score,
            counts=dict(counts),feed=price.summary(),queue_depth=queue.qsize(),score_queue_depth=score_queue.qsize(),
            scoring_ms_p95=float(np.quantile(latencies,.95)) if latencies else None,
            accounts={n:a.summary() for n,a in accounts.items()},scoring=monitor.summary(),
            endpoints=endpoints,failures=list(failures),sample_target_per_thesis=args.target_closed_trades,
            wall_time_limit_seconds=args.duration_seconds,
            live_history_outcomes="selection priors updated on received E4 buys; no invented net-PnL outcome priors",
            execution_model="paper reserve quotes; 10ms floor is an assumption, NOT proven transaction landing latency")
    async def heartbeat():
        while not drained.is_set():
            write_json(status_path,snapshot())
            for a in accounts.values():a.checkpoint()
            await asyncio.sleep(5)
    hb=asyncio.create_task(heartbeat())
    write_json(status_path,snapshot())
    try:
        while not stop.is_set() and time.time()-started<args.duration_seconds:
            if all(len(a.ledger)>=args.target_closed_trades for a in accounts.values()):
                reason="SAMPLE_TARGET_REACHED";break
            if any(a.errors for a in accounts.values()):
                failures.append("EXECUTION_TASK_OR_EXIT_FAILURE");reason="TECHNICAL_FAILURE";break
            if hashlib.sha256(json.dumps(CANDIDATES,sort_keys=True).encode()).hexdigest()!=CONFIG_HASH:
                failures.append("THESIS_MUTATED");reason="TECHNICAL_FAILURE";break
            try:received_ns,payload=await asyncio.wait_for(queue.get(),timeout=.2)
            except asyncio.TimeoutError:continue
            incoming=[];prod.decode_log_payload(payload,incoming,dedupe)
            if not incoming:continue
            last_event=time.time();counts["decoded_events"]+=len(incoming)
            for event in incoming:
                event.received_ns=received_ns
                state=checked_state(event,build)
                if state:states[event.mint]=state
            for create in (e for e in incoming if e.kind=="CREATE"):
                if create.mint in seen:continue
                seen.add(create.mint);counts["launches_seen"]+=1;last_launch=time.time()
                row=make_row(create,incoming,build,history)
                creators[create.mint]=row["creator"];early_buyers[create.mint]=row["first_outside_buyers_0ms"]
                rejection=None
                if not native_quote(create.raw):rejection="UNSUPPORTED_QUOTE"
                elif not price.fresh():rejection="STALE_SOL_USD"
                else:
                    try:validate_row(row,scorer.fields)
                    except ValueError as exc:rejection=str(exc);counts["invalid_features"]+=1
                state=states.get(create.mint)
                if state is None:rejection="NO_RESERVE_STATE"
                if rejection:
                    counts[rejection]+=1
                    evidence.append("decisions",dict(mint=create.mint,at_ns=time.time_ns(),reason=rejection,
                        inputs={f:row.get(f) for f in scorer.fields}))
                else:
                    try:score_queue.put_nowait((row,dict(state)))
                    except asyncio.QueueFull:
                        counts["score_queue_overflow"]+=1;failures.append("SCORE_QUEUE_OVERFLOW");stop.set()
            # Observe causal E4 selection only AFTER the current CREATE snapshot.
            for event in incoming:
                if event.kind in build.BUY_KINDS and event.trader==build.E4_WALLET and event.mint not in selected:
                    creator=creators.get(event.mint) or event.creator
                    if creator:
                        selected.add(event.mint);history.creator_selections[creator]+=1;history.creator_landed[creator]+=1
                        history.last_creator_selection[creator]=received_ns
                        for buyer in early_buyers.get(event.mint,[]):
                            history.buyer_selections[buyer]+=1;history.creator_buyer_pairs[(creator,buyer)]+=1
                        counts["live_prior_selection_updates"]+=1
            # Bounded storage: keep active reserves; dedupe is rotated only beyond
            # the operational session's bounded 5.5-hour budget.
            if counts["decoded_events"]%1000<len(incoming):
                active={m for a in accounts.values() for m in list(a.active)+list(a.pending)}
                cutoff=time.time_ns()-300_000_000_000
                for m in list(states):
                    if m not in active and states[m]["ns"]<cutoff:states.pop(m,None)
        if failures:reason="TECHNICAL_FAILURE"
        elif stop.is_set():reason="OPERATIONS_STOPPED"
    finally:
        for a in accounts.values():a.entries_paused=True
        # No new scoring entries; keep market reception while normal timed exits
        # settle, rather than cancelling the stream first.
        drained.set()
        await sc_task
        deadline=time.monotonic()+5
        while any(a.tasks for a in accounts.values()) and time.monotonic()<deadline:
            try:
                received_ns,payload=await asyncio.wait_for(queue.get(),timeout=.02)
                incoming=[];prod.decode_log_payload(payload,incoming,dedupe)
                for event in incoming:
                    event.received_ns=received_ns
                    state=checked_state(event,build)
                    if state:states[event.mint]=state
            except asyncio.TimeoutError:pass
        stop.set()
        for task in workers+[price_task,hb]:task.cancel()
        await asyncio.gather(*workers,price_task,hb,return_exceptions=True)
        for a in accounts.values():
            for task in list(a.tasks):task.cancel()
            await asyncio.gather(*list(a.tasks),return_exceptions=True)
            a.checkpoint()
        final=snapshot(True)
        if any(a.active or a.errors for a in accounts.values()):
            final["status"]="TECHNICAL_FAILURE";reason="TECHNICAL_FAILURE"
        write_json(status_path,final);write_json(out/"result.json",final)
        evidence.append("operations",dict(at=time.time(),status=reason))
    return 0 if reason=="SAMPLE_TARGET_REACHED" else 2 if reason=="INSUFFICIENT_SAMPLE" else 3

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ("research-root","production-root","corpus","output-dir"):p.add_argument("--"+key,required=True)
    p.add_argument("--duration-seconds",type=float,default=19800)
    p.add_argument("--target-closed-trades",type=int,default=20)
    p.add_argument("--entry-delay-ms",type=float,default=10)
    p.add_argument("--max-decision-age-ms",type=float,default=250)
    p.add_argument("--ws-url",action="append")
    args=p.parse_args()
    if args.entry_delay_ms<10 or args.target_closed_trades<1 or not 0<args.duration_seconds<=19800:
        p.error("invalid operational bounds")
    try:return asyncio.run(online(args))
    except Exception as exc:
        write_json(Path(args.output_dir)/"status.json",dict(version=VERSION,status="TECHNICAL_FAILURE",
                    checked_at=time.time(),failures=[repr(exc)],paper_only=True,config_hash=CONFIG_HASH))
        raise
if __name__=="__main__":raise SystemExit(main())
