#!/usr/bin/env python3
"""Independent forward-paper developer-library competitor.

It observes fresh Pump launches directly from Solana, causally learns creator outcomes,
and paper-trades only creators that satisfy the tiny rule in gambit_library_policy.py.
It cannot broadcast transactions and does not import or modify the frozen four-model
campaign. Unknown/incomplete market paths remain unknown.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

import aiohttp

from gambit_library_policy import AppendStore, DeveloperLibrary, MODEL, RuleConfig
from golden_horizon_engine import Config, buy_quote, sell_quote, fee_breakdown, valid

RPCS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")
WSS = ("wss://api.mainnet-beta.solana.com", "wss://solana-rpc.publicnode.com")
READ_METHODS = {"getMinimumBalanceForRentExemption", "getRecentPrioritizationFees", "getTransaction"}
VERSION = "gambit-library-forward-paper-v1"
DECISION_MS = 10
MAX_CONCURRENT = 2


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, allow_nan=False, indent=2)
        f.write("\n")
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def state_from_item(item: dict[str, Any], recv_ns: int, sig: str, slot: int) -> dict[str, Any] | None:
    quote = item.get("quote_mint")
    if quote not in (None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112"):
        return None
    if item.get("anchor_event") in ("CompleteEvent", "CompletePumpAmmMigrationEvent"):
        return {"ns": recv_ns, "signature": sig, "slot": slot, "complete": True, "vsol": 0.0, "vtok": 0.0}
    vs = item.get("virtual_sol_reserves", item.get("virtual_quote_reserves"))
    vt = item.get("virtual_token_reserves")
    if not isinstance(vs, int) or not isinstance(vt, int) or vs <= 0 or vt <= 0:
        return None
    rt = item.get("real_token_reserves")
    return {"ns": recv_ns, "signature": sig, "slot": slot, "complete": False,
            "vsol": vs / 1e9, "vtok": vt / 1e6,
            "rtok": rt / 1e6 if isinstance(rt, int) else None}


class ReadRpc:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.seq = 0
        self.errors: list[str] = []

    async def call(self, method: str, params: list[Any], attempts: int = 5) -> Any:
        if method not in READ_METHODS:
            raise ValueError("non-read RPC method blocked")
        last: Exception | None = None
        for i in range(attempts):
            self.seq += 1
            try:
                async with self.session.post(RPCS[i % len(RPCS)], json={"jsonrpc":"2.0","id":self.seq,
                        "method":method,"params":params}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    r.raise_for_status(); body = await r.json()
                if body.get("error"):
                    raise RuntimeError(str(body["error"]))
                return body.get("result")
            except Exception as exc:
                last = exc
                self.errors.append(f"{method}:{type(exc).__name__}:{str(exc)[:120]}")
                await asyncio.sleep(min(2.0, .25 * (i + 1)))
        raise RuntimeError(f"read RPC failed: {method}: {last}")


async def socket_worker(url: str, program_id: str, decoder: Any, queue: asyncio.Queue,
                        stop: asyncio.Event, stats: dict[str, Any], errors: list[str]) -> None:
    st = stats.setdefault(url, {"connections":0,"disconnects":0,"messages":0,"last_receive_ns":None})
    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=10, max_msg_size=8*1024*1024,
                        timeout=aiohttp.ClientWSTimeout(ws_receive=15, ws_close=5)) as ws:
                    st["connections"] += 1
                    await ws.send_json({"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
                                        "params":[{"mentions":[program_id]},{"commitment":"processed"}]})
                    while not stop.is_set():
                        msg = await ws.receive()
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv = time.time_ns(); st["messages"] += 1; st["last_receive_ns"] = recv
                            body = json.loads(msg.data)
                            if body.get("method") != "logsNotification":
                                continue
                            try:
                                queue.put_nowait((recv, url, body))
                            except asyncio.QueueFull:
                                errors.append("FEED_QUEUE_OVERFLOW_DATA_LOSS"); stop.set(); return
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st["last_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
            st["disconnects"] += 1
            await asyncio.sleep(.5)


class Runner:
    def __init__(self, args: argparse.Namespace, decoder: Any, config: Config, master: dict[str, Any]):
        self.args = args; self.decoder = decoder; self.c = config
        self.library = DeveloperLibrary(master, RuleConfig())
        self.events = AppendStore(args.output / "library-events.jsonl")
        self.counts = Counter()
        self.errors: list[str] = []
        self.feed_stats: dict[str, Any] = {}
        self.launches: dict[str, dict[str, Any]] = {}
        self.latest: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.closed: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.seen_sigs: set[tuple[str, str]] = set()
        self.cash = config.starting_sol
        self.peak = config.starting_sol
        self.max_dd = 0.0
        self.started_ns = time.time_ns()
        self.run_id = os.getenv("GITHUB_RUN_ID", f"local-{self.started_ns}")
        self.last_feed_ns = self.started_ns
        self.last_save = 0.0

    def event_id(self, kind: str, mint: str, extra: str = "") -> str:
        return f"{self.run_id}:{kind}:{mint}:{extra}"

    def emit(self, kind: str, mint: str, data: dict[str, Any], extra: str = "") -> None:
        self.events.append({"event_id":self.event_id(kind,mint,extra), "kind":kind, "mint":mint,
                            "observed_ns":time.time_ns(), **data})

    def equity(self) -> float:
        value = self.cash
        for mint, p in self.positions.items():
            if p["status"] not in ("OPEN", "EXIT_PENDING"):
                continue
            s = self.latest.get(mint)
            if valid(s):
                try:
                    gross = sell_quote(p["tokens"], s, p["curve_sol"], -p["tokens"])
                    fees = fee_breakdown(self.c, gross)
                    value += max(0.0, gross - sum(fees.values())) + p["rent"]
                except ValueError:
                    pass
        return value

    def update_dd(self) -> None:
        eq = self.equity(); self.peak = max(self.peak, eq)
        if self.peak:
            self.max_dd = max(self.max_dd, (self.peak - eq) / self.peak)

    def save(self, collector_active: bool = True) -> None:
        wins = sum(1 for p in self.closed if p.get("net_pnl_sol",0)>0)
        losses = sum(1 for p in self.closed if p.get("net_pnl_sol",0)<0)
        status = {
            "version":VERSION, "model":MODEL, "paper_only":True, "real_execution":False,
            "collector_active":collector_active, "started_ns":self.started_ns, "checked_ns":time.time_ns(),
            "cash_sol":self.cash, "equity_sol":self.equity(), "net_pnl_sol":self.cash-self.c.starting_sol,
            "closed_trades":len(self.closed), "wins":wins, "losses":losses,
            "win_rate":wins/len(self.closed) if self.closed else None,
            "active_positions":sum(p["status"] in ("PENDING","OPEN","EXIT_PENDING") for p in self.positions.values()),
            "maximum_mark_to_market_drawdown":self.max_dd, "counts":dict(self.counts),
            "rule":self.library.snapshot()["rule"], "errors":list(dict.fromkeys(self.errors)),
            "feed_stats":self.feed_stats,
            "frozen_four_campaign_touched":False,
            "note":"Independent read-only Solana observer and paper account; no blockchain submission path."
        }
        write_json(self.args.output/"status.json", status)
        write_json(self.args.output/"library-runtime.json", self.library.snapshot())
        write_json(self.args.output/"paper-trades.json", {"model":MODEL,"paper_only":True,"trades":self.closed})
        write_json(self.args.output/"rejections.json", {"model":MODEL,"rejections":self.rejections})
        self.last_save = time.monotonic()

    async def confirm_creation(self, rpc: ReadRpc, mint: str, sig: str, started_sec: int) -> None:
        tx = None
        try:
            for _ in range(6):
                await asyncio.sleep(2)
                tx = await rpc.call("getTransaction", [sig,{"encoding":"json","commitment":"confirmed","maxSupportedTransactionVersion":1}])
                if tx:
                    break
            if not tx or (tx.get("meta") or {}).get("err") is not None:
                self.counts["creation_proof_unknown"] += 1; return
            decoded = self.decoder.anchor_events_from_logs((tx.get("meta") or {}).get("logMessages") or [], self.decoder.PUMP_PROGRAM_ID)
            ok = tx.get("blockTime") is not None and tx["blockTime"] >= started_sec-3 and any(
                x.get("anchor_event")=="CreateEvent" and x.get("mint")==mint for x in decoded)
            if ok:
                self.counts["creation_proof_confirmed"] += 1
                self.emit("CREATION_CONFIRMED",mint,{"signature":sig,"slot":tx.get("slot"),"block_time":tx.get("blockTime")})
            else:
                self.counts["creation_proof_unknown"] += 1
        except Exception as exc:
            self.counts["creation_proof_unknown"] += 1
            self.errors.append(f"creation-proof:{type(exc).__name__}:{str(exc)[:120]}")

    async def decide(self, mint: str) -> None:
        launch = self.launches[mint]
        due = launch["create_ns"] + DECISION_MS*1_000_000
        delay = max(0,(due-time.time_ns())/1e9)
        if delay:
            await asyncio.sleep(delay)
        d = self.library.decide(launch["creator"])
        self.counts["decisions"] += 1
        self.emit("DECISION",mint,{"creator":launch["creator"],"name":launch["name"],"symbol":launch["symbol"],
                  "create_signature":launch["signature"],"decision":d})
        if not d["qualifies"]:
            row={"mint":mint,"creator":launch["creator"],"reason":d["reason"],"decision":d,"ns":time.time_ns()}
            self.rejections.append(row); self.counts["rejected_dev_rule"] += 1
            return
        active = sum(p["status"] in ("PENDING","OPEN","EXIT_PENDING") for p in self.positions.values())
        if active >= MAX_CONCURRENT:
            row={"mint":mint,"creator":launch["creator"],"reason":"CONCURRENCY","decision":d,"ns":time.time_ns()}
            self.rejections.append(row); self.counts["rejected_concurrency"] += 1
            return
        state = self.latest.get(mint)
        if not valid(state):
            row={"mint":mint,"creator":launch["creator"],"reason":"INVALID_DECISION_STATE","decision":d,"ns":time.time_ns()}
            self.rejections.append(row); self.counts["rejected_invalid_state"] += 1
            return
        budget=min(self.cash*self.library.c.position_fraction, self.cash-self.c.reserve_sol)
        curve=(budget-self.c.fixed-self.c.rent_sol)/(1+self.c.percent)
        if curve <= .00001:
            self.rejections.append({"mint":mint,"creator":launch["creator"],"reason":"BANKROLL_TOO_LOW","ns":time.time_ns()})
            self.counts["rejected_bankroll"] += 1; return
        quoted=buy_quote(curve,state)
        now=time.time_ns()
        self.positions[mint]={"mint":mint,"creator":launch["creator"],"status":"PENDING","decision":d,
            "budget":budget,"curve_sol":curve,"rent":self.c.rent_sol,"quoted_tokens":quoted,
            "intent_state":dict(state),"intent_ns":now,"due_ns":now+self.c.inclusion_delay_ms*1_000_000,
            "entry_attempts":0,"fees":{},"actions":[]}
        self.counts["selected"] += 1
        self.emit("TRADE_SELECTED",mint,{"creator":launch["creator"],"budget_sol":budget,"decision":d})

    async def resolve_observation(self, mint: str) -> None:
        launch=self.launches[mint]
        due=launch["create_ns"]+self.library.c.horizon_ms*1_000_000
        delay=max(0,(due-time.time_ns())/1e9)
        if delay:
            await asyncio.sleep(delay)
        s0=launch["state"]; s1=self.latest.get(mint)
        if not valid(s0) or not valid(s1) or int(s1.get("ns",0)) < due:
            self.counts["observation_unknown"] += 1
            self.emit("OBSERVATION_UNKNOWN",mint,{"creator":launch["creator"],"reason":"NO_VALID_3S_STATE"})
            return
        try:
            stake=self.library.c.observation_stake_sol
            tok=buy_quote(stake,s0)
            proceeds=sell_quote(tok,s1,stake,-tok)
            ret=proceeds/stake-1
        except ValueError:
            self.counts["observation_unknown"] += 1
            self.emit("OBSERVATION_UNKNOWN",mint,{"creator":launch["creator"],"reason":"QUOTE_UNAVAILABLE"})
            return
        updated=self.library.update(mint=mint,creator=launch["creator"],return_fraction=ret)
        if updated:
            self.counts["observation_resolved"] += 1
        self.emit("OBSERVATION_RESOLVED",mint,{"creator":launch["creator"],"return_fraction":ret,
                  "outcome":"WIN" if ret>0 else "LOSS" if ret<0 else "BREAKEVEN",
                  "developer_stats_after":self.library.stats(launch["creator"])})

    def add_fees(self, p: dict[str, Any], fees: dict[str,float]) -> None:
        for k,v in fees.items(): p["fees"][k]=p["fees"].get(k,0.0)+v

    def failed_attempt(self, p: dict[str,Any], side: str, s: dict[str,Any], now: int) -> None:
        fees=fee_breakdown(self.c,0,failed=True); total=sum(fees.values())
        self.cash-=total; self.add_fees(p,fees)
        p["actions"].append({"side":side,"outcome":"MODELLED_LANDED_FAILURE","ns":now,"fees":fees,"state":dict(s)})
        self.counts["modelled_landed_failures"] += 1

    def tick_position(self, mint: str, s: dict[str,Any], now: int) -> None:
        p=self.positions.get(mint)
        if not p or p["status"] in ("CLOSED","NO_ENTRY","UNRESOLVED"):
            return
        if p["status"]=="PENDING":
            if now < p["due_ns"]:
                return
            p["entry_attempts"]+=1
            tok=buy_quote(p["curve_sol"],s)
            slip=1-tok/p["quoted_tokens"] if p["quoted_tokens"] else 1
            if slip > self.c.max_slippage_bps/10000:
                self.failed_attempt(p,"BUY",s,now)
                if p["entry_attempts"]>=self.c.max_attempts or now-self.launches[mint]["create_ns"]>self.c.max_entry_age_ms*1_000_000:
                    p["status"]="NO_ENTRY"; self.counts["no_entry"]+=1
                    self.emit("TRADE_NO_ENTRY",mint,{"creator":p["creator"],"fees":p["fees"],"attempts":p["entry_attempts"]})
                else:
                    p["quoted_tokens"]=tok; p["intent_state"]=dict(s); p["intent_ns"]=now
                    p["due_ns"]=now+self.c.retry_ms*1_000_000
                return
            fees=fee_breakdown(self.c,p["curve_sol"]); cost=p["curve_sol"]+sum(fees.values())
            if self.cash < cost+p["rent"]:
                p["status"]="NO_ENTRY"; self.counts["no_entry"]+=1; return
            self.cash-=cost+p["rent"]; self.add_fees(p,fees)
            p.update(status="OPEN",tokens=tok,entry_ns=now,buy_cost=cost,entry_state=dict(s),
                     exit_horizon_ns=now+self.library.c.horizon_ms*1_000_000)
            p["actions"].append({"side":"BUY","outcome":"MODELLED_FILL","ns":now,"fees":fees,"tokens":tok,"state":dict(s)})
            self.counts["entries"] += 1
            self.emit("PAPER_BUY",mint,{"creator":p["creator"],"tokens":tok,"budget_sol":p["budget"],"fees":fees})
            self.update_dd(); return
        if p["status"]=="OPEN" and now>=p["exit_horizon_ns"]:
            gross=sell_quote(p["tokens"],s,p["curve_sol"],-p["tokens"])
            p.update(status="EXIT_PENDING",exit_quote=gross,exit_intent_ns=now,
                     exit_due_ns=now+self.c.inclusion_delay_ms*1_000_000,exit_attempts=0,exit_quote_state=dict(s))
            return
        if p["status"]=="EXIT_PENDING" and now>=p["exit_due_ns"]:
            p["exit_attempts"]+=1
            gross=sell_quote(p["tokens"],s,p["curve_sol"],-p["tokens"])
            slip=1-gross/p["exit_quote"] if p["exit_quote"] else 1
            if slip > self.c.max_slippage_bps/10000:
                self.failed_attempt(p,"SELL",s,now)
                if p["exit_attempts"]>=self.c.max_attempts:
                    p["status"]="UNRESOLVED"; self.counts["unresolved_exit"]+=1
                    self.emit("TRADE_UNRESOLVED",mint,{"creator":p["creator"],"reason":"EXIT_SLIPPAGE_RETRY_LIMIT","fees":p["fees"]})
                else:
                    p["exit_quote"]=gross; p["exit_quote_state"]=dict(s)
                    p["exit_due_ns"]=now+self.c.retry_ms*1_000_000
                return
            fees=fee_breakdown(self.c,gross); proceeds=gross-sum(fees.values())+p["rent"]
            self.cash+=proceeds; self.add_fees(p,fees)
            p["actions"].append({"side":"SELL","outcome":"MODELLED_FILL","ns":now,"fees":fees,"gross_sol":gross,"state":dict(s)})
            p["status"]="CLOSED"; p["exit_ns"]=now; p["gross_sell_sol"]=gross
            p["net_pnl_sol"]=proceeds-p["buy_cost"]-p["rent"]-sum(a.get("fees",{}).get("network_base_sol",0)+a.get("fees",{}).get("priority_sol",0) for a in p["actions"] if a.get("outcome")=="MODELLED_LANDED_FAILURE")
            # cash-before/after is authoritative; recompute trade P&L from explicit flows.
            buy_total=p["curve_sol"]+sum(p["actions"][0]["fees"].values())+p["rent"]
            sell_total=gross-sum(fees.values())+p["rent"]
            failed_fees=sum(sum(a.get("fees",{}).values()) for a in p["actions"] if a.get("outcome")=="MODELLED_LANDED_FAILURE")
            p["net_pnl_sol"]=sell_total-buy_total-failed_fees
            self.closed.append({k:v for k,v in p.items() if k not in ("intent_state",)})
            self.counts["closed_trades"]+=1
            self.emit("PAPER_TRADE_CLOSED",mint,{"creator":p["creator"],"net_pnl_sol":p["net_pnl_sol"],
                      "outcome":"WIN" if p["net_pnl_sol"]>0 else "LOSS" if p["net_pnl_sol"]<0 else "BREAKEVEN",
                      "fees":p["fees"],"holding_ms":(now-p["entry_ns"])/1e6})
            self.update_dd()

    def maintenance(self) -> None:
        now=time.time_ns()
        for mint,p in list(self.positions.items()):
            if p["status"]=="PENDING" and now-self.launches[mint]["create_ns"] > (self.c.max_entry_age_ms+2000)*1_000_000:
                p["status"]="NO_ENTRY"; self.counts["no_entry"]+=1
                self.emit("TRADE_NO_ENTRY",mint,{"creator":p["creator"],"reason":"NO_FRESH_FILL_STATE","fees":p["fees"]})
            elif p["status"] in ("OPEN","EXIT_PENDING") and now-p.get("entry_ns",now) > 20_000_000_000:
                p["status"]="UNRESOLVED"; self.counts["unresolved_exit"]+=1
                self.emit("TRADE_UNRESOLVED",mint,{"creator":p["creator"],"reason":"MARKET_PATH_ENDED_OR_MIGRATED","fees":p["fees"]})
        self.update_dd()


async def run(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True,exist_ok=True)
    master=json.loads(args.master_devs.read_text())
    manifest=json.loads(args.execution_manifest.read_text())
    cfg_values=dict(manifest["execution_model"])
    decoder=load_module("library_pump_decoder",args.decoder)
    queue: asyncio.Queue=asyncio.Queue(maxsize=20000)
    stop=asyncio.Event()
    async with aiohttp.ClientSession() as session:
        rpc=ReadRpc(session)
        rent=await rpc.call("getMinimumBalanceForRentExemption",[manifest["assumed_token_account_bytes"]])
        cfg_values["rent_sol"]=int(rent)/1e9
        c=Config(**cfg_values)
        runner=Runner(args,decoder,c,master)
        started_sec=runner.started_ns//1_000_000_000
        tasks=[asyncio.create_task(socket_worker(url,decoder.PUMP_PROGRAM_ID,decoder,queue,stop,runner.feed_stats,runner.errors)) for url in WSS]
        deadline=time.monotonic()+args.duration
        runner.save(True)
        try:
            while time.monotonic()<deadline and len(runner.closed)<args.target:
                try:
                    recv,provider,msg=await asyncio.wait_for(queue.get(),timeout=1.0)
                except asyncio.TimeoutError:
                    runner.maintenance()
                    if time.monotonic()-runner.last_save>5: runner.save(True)
                    continue
                runner.last_feed_ns=recv
                try:
                    value=msg["params"]["result"]["value"]; sig=value["signature"]
                    if value.get("err") is not None:
                        continue
                    slot=int(msg["params"]["result"]["context"]["slot"])
                    items=decoder.anchor_events_from_logs(value.get("logs") or [],decoder.PUMP_PROGRAM_ID)
                except Exception as exc:
                    runner.errors.append(f"decode-envelope:{type(exc).__name__}:{str(exc)[:100]}"); continue
                for item in items:
                    mint=item.get("mint")
                    if not mint:
                        continue
                    key=(sig,str(item.get("anchor_event")))
                    if key in runner.seen_sigs:
                        continue
                    runner.seen_sigs.add(key)
                    s=state_from_item(item,recv,sig,slot)
                    if s is not None:
                        runner.latest[mint]=s
                        if valid(s): runner.tick_position(mint,s,recv)
                    if item.get("anchor_event")=="CreateEvent" and mint not in runner.launches:
                        ts=item.get("timestamp")
                        if not isinstance(ts,int) or ts<started_sec-3 or abs(recv/1e9-ts)>15 or not valid(s):
                            runner.counts["rejected_nonfresh_or_unquotable_create"]+=1; continue
                        launch={"mint":mint,"create_ns":recv,"signature":sig,"slot":slot,"timestamp":ts,
                                "creator":item.get("creator") or item.get("user") or "",
                                "name":item.get("name") or "","symbol":item.get("symbol") or "","state":dict(s)}
                        runner.launches[mint]=launch; runner.counts["fresh_launches"]+=1
                        runner.emit("FRESH_CREATE",mint,{k:v for k,v in launch.items() if k!="state"})
                        asyncio.create_task(runner.decide(mint))
                        asyncio.create_task(runner.resolve_observation(mint))
                        asyncio.create_task(runner.confirm_creation(rpc,mint,sig,started_sec))
                runner.maintenance()
                if time.monotonic()-runner.last_save>5: runner.save(True)
        finally:
            stop.set()
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.sleep(0)
            runner.maintenance(); runner.errors.extend(rpc.errors[-20:])
            runner.save(False)
            final=json.loads((args.output/"status.json").read_text())
            final.update(status="COMPLETE" if not runner.errors else "COMPLETE_WITH_READ_ERRORS",
                         target=args.target,duration_seconds=args.duration,ended_ns=time.time_ns())
            write_json(args.output/"final.json",final)
    return 0


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--decoder",type=Path,default=Path("src/memecoin_bot/realtime/pumpfun.py"))
    p.add_argument("--master-devs",type=Path,default=Path("docs/research/gambit-library-devs.json"))
    p.add_argument("--execution-manifest",type=Path,default=Path("config/golden-top4-live.json"))
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--duration",type=int,default=14400)
    p.add_argument("--target",type=int,default=100)
    a=p.parse_args()
    if a.duration<60 or a.target<1:
        p.error("unsafe/meaningless run bounds")
    return asyncio.run(run(a))

if __name__=="__main__":
    raise SystemExit(main())
