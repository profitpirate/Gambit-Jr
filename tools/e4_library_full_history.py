#!/usr/bin/env python3
"""Build a conservative E4 trade/dev library from full wallet history.

Read-only. Never signs or broadcasts. It scans the E4 wallet's entire RPC-visible
history, reconstructs complete single-token SOL cycles, joins known launch/dev
metadata from the frozen 66k launch corpus, and stores simple outcome views.
Unknown/incomplete evidence is preserved as UNKNOWN rather than guessed.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp

WALLET = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WSOL = "So11111111111111111111111111111111111111112"
RPCS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")
SCHEMA = "gambit-library-e4-v1"


def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.replace(tmp, path)


def finite(x: Any, default: float = 0.0) -> float:
    try:
        y = float(x)
    except (TypeError, ValueError):
        return default
    return y if math.isfinite(y) else default


class Rpc:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session; self.i = 0; self.req = 0

    async def call(self, method: str, params: list[Any], tries: int = 10) -> Any:
        last = None
        for n in range(tries):
            url = RPCS[(self.i + n) % len(RPCS)]; self.req += 1
            payload = {"jsonrpc": "2.0", "id": self.req, "method": method, "params": params}
            try:
                async with self.s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=40)) as r:
                    text = await r.text()
                    if r.status == 429:
                        await asyncio.sleep(min(8, .4 * (2 ** min(n, 4)))); continue
                    if r.status != 200:
                        last = RuntimeError(f"HTTP {r.status}: {text[:160]}"); continue
                    data = json.loads(text)
                    if data.get("error"):
                        code = data["error"].get("code")
                        if code in (-32005, -32603, -32701):
                            last = RuntimeError(str(data["error"])); await asyncio.sleep(.25 + .3*n); continue
                        raise RuntimeError(str(data["error"]))
                    self.i = (self.i + n) % len(RPCS); return data.get("result")
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                last = e; await asyncio.sleep(.25 + .3*n)
        raise RuntimeError(f"RPC {method} failed: {last}")


async def full_signatures(rpc: Rpc, checkpoint: Path) -> list[dict[str, Any]]:
    existing = []; before = None
    if checkpoint.exists():
        raw = json.loads(checkpoint.read_text()); existing = raw.get("rows", []); before = raw.get("before")
        if raw.get("complete"): return existing
    seen = {r["signature"] for r in existing}; pages = 0
    while True:
        opts: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before: opts["before"] = before
        page = await rpc.call("getSignaturesForAddress", [WALLET, opts]); pages += 1
        if not page:
            atomic_json(checkpoint, {"wallet": WALLET, "rows": existing, "before": before, "complete": True, "pages": pages}); return existing
        for row in page:
            if row["signature"] not in seen: existing.append(row); seen.add(row["signature"])
        before = page[-1]["signature"]
        atomic_json(checkpoint, {"wallet": WALLET, "rows": existing, "before": before, "complete": False, "pages": pages})
        print(json.dumps({"phase":"signatures", "count":len(existing), "oldest_block_time":page[-1].get("blockTime")}), flush=True)
        if len(page) < 1000:
            atomic_json(checkpoint, {"wallet": WALLET, "rows": existing, "before": before, "complete": True, "pages": pages}); return existing
        await asyncio.sleep(.15)


def keys_of(tx: dict[str, Any]) -> list[str]:
    keys = [x.get("pubkey") if isinstance(x, dict) else x for x in tx["transaction"]["message"].get("accountKeys", [])]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + loaded.get("writable", []) + loaded.get("readonly", [])


def parse_tx(tx: dict[str, Any], sigrow: dict[str, Any]) -> dict[str, Any]:
    meta = tx.get("meta") or {}; keys = keys_of(tx)
    if WALLET not in keys:
        return {"signature": sigrow["signature"], "block_time": tx.get("blockTime"), "slot": tx.get("slot"), "kind":"NOT_WALLET"}
    idx = keys.index(WALLET); balances = {}
    for phase in ("pre", "post"):
        vals: Counter = Counter(); decs: dict[str,int] = {}
        for b in meta.get(phase+"TokenBalances", []) or []:
            if b.get("owner") != WALLET: continue
            a = b.get("uiTokenAmount") or {}
            try: raw = int(a.get("amount", "0"))
            except Exception: raw = 0
            mint = b.get("mint")
            if mint: vals[mint] += raw; decs[mint] = int(a.get("decimals",0))
        balances[phase] = vals, decs
    pre, post = balances["pre"][0], balances["post"][0]; decs = {**balances["pre"][1], **balances["post"][1]}
    deltas = {m:{"pre_raw":pre[m],"post_raw":post[m],"delta_raw":post[m]-pre[m],"decimals":decs.get(m,0)} for m in set(pre)|set(post) if post[m] != pre[m]}
    native = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9 if idx < len(meta.get("preBalances",[])) and idx < len(meta.get("postBalances",[])) else 0.0
    wrapped = deltas.get(WSOL,{}).get("delta_raw",0) / 1e9; liquid = native + wrapped
    fee_payer = keys[0] if keys else None; fee = meta.get("fee",0)/1e9 if fee_payer == WALLET else 0.0
    assets = {m:d for m,d in deltas.items() if m != WSOL}; failed = meta.get("err") is not None
    kind = "FAILED_TRANSACTION" if failed else "UNCLASSIFIED"; mint = None
    if not failed and len(assets) == 1:
        mint, ch = next(iter(assets.items()))
        if ch["delta_raw"] > 0 and liquid < -max(.00005, fee): kind = "TRADE_BUY"
        elif ch["delta_raw"] < 0 and liquid > max(.00005, fee): kind = "TRADE_SELL"
    return {"signature": sigrow["signature"], "slot": tx.get("slot"), "block_time": tx.get("blockTime"), "kind": kind, "mint": mint,
            "wallet_liquid_cashflow_sol": liquid, "actual_chain_fee_paid_sol": fee, "chain_error": meta.get("err"), "token_deltas": deltas, "fee_payer": fee_payer}


async def all_transactions(rpc: Rpc, sigs: list[dict[str,Any]], checkpoint: Path) -> list[dict[str,Any]]:
    prior = []; done = set()
    if checkpoint.exists():
        raw = json.loads(checkpoint.read_text()); prior = raw.get("rows", []); done = {r["signature"] for r in prior}
    ordered = sorted(sigs, key=lambda r: (r.get("slot",0), r.get("signature",""))); missing = []
    for sr in ordered:
        sig = sr["signature"]
        if sig in done: continue
        tx = await rpc.call("getTransaction", [sig, {"encoding":"json", "commitment":"confirmed", "maxSupportedTransactionVersion":1}])
        if tx is None:
            missing.append(sig); row = {"signature":sig,"slot":sr.get("slot"),"block_time":sr.get("blockTime"),"kind":"MISSING_TRANSACTION"}
        else: row = parse_tx(tx, sr)
        prior.append(row); done.add(sig)
        if len(prior) % 50 == 0:
            atomic_json(checkpoint, {"wallet":WALLET,"rows":prior,"missing":missing,"complete":False})
            print(json.dumps({"phase":"transactions","processed":len(prior),"total":len(ordered),"counts":dict(Counter(x["kind"] for x in prior))}), flush=True)
        await asyncio.sleep(.025)
    atomic_json(checkpoint, {"wallet":WALLET,"rows":prior,"missing":missing,"complete":True}); return prior


def load_corpus(path: Path) -> dict[str,dict[str,Any]]:
    out = {}; wanted = {"mint","creator","name","symbol","create_ns","create_signature","create_fdv_usd","create_price_sol",
        "max_multiple_1000ms","max_multiple_2000ms","max_multiple_3000ms","max_multiple_5000ms","max_multiple_10000ms","max_multiple_15000ms",
        "max_multiple_30000ms","max_multiple_60000ms","max_multiple_120000ms","max_multiple_300000ms"}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line); mint = r.get("mint")
            if mint: out[mint] = {k:r.get(k) for k in wanted if k in r}
    return out


def reconstruct_cycles(rows: list[dict[str,Any]], meta: dict[str,dict[str,Any]]) -> tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    open_by_mint = {}; cycles = []; seq = 0
    for r in sorted(rows, key=lambda x:(x.get("slot") or 0, x.get("signature") or "")):
        if r.get("kind") not in ("TRADE_BUY","TRADE_SELL") or not r.get("mint"): continue
        mint = r["mint"]; ch = r.get("token_deltas",{}).get(mint)
        if not ch: continue
        p = open_by_mint.get(mint)
        if p is None:
            seq += 1; p = {"id":f"E4:{mint}:{seq}","source":"E4","dev_source_label":"E4","mint":mint,
                "basis_known":r["kind"]=="TRADE_BUY" and ch.get("pre_raw")==0,"entry_time":None,"final_exit_time":None,"cashflow_sol":0.0,"fees_sol":0.0,
                "buy_count":0,"sell_count":0,"signatures":[],"token_balance_raw":ch.get("pre_raw",0),"entry_cost_sol":0.0,"sell_proceeds_sol":0.0,
                "entry_signature":None,"final_exit_signature":None}; open_by_mint[mint] = p
        if p["token_balance_raw"] != ch.get("pre_raw"): p["basis_known"] = False
        cf = finite(r.get("wallet_liquid_cashflow_sol")); p["cashflow_sol"] += cf; p["fees_sol"] += finite(r.get("actual_chain_fee_paid_sol"))
        p["signatures"].append(r["signature"]); p["token_balance_raw"] = ch.get("post_raw",0)
        if r["kind"] == "TRADE_BUY":
            p["buy_count"] += 1; p["entry_cost_sol"] += max(0.0,-cf)
            if p["entry_time"] is None: p["entry_time"] = r.get("block_time"); p["entry_signature"] = r["signature"]
        else: p["sell_count"] += 1; p["sell_proceeds_sol"] += max(0.0,cf)
        if ch.get("post_raw") == 0:
            p["final_exit_time"] = r.get("block_time"); p["final_exit_signature"] = r["signature"]; md = meta.get(mint,{})
            p.update({"creator":md.get("creator"),"name":md.get("name"),"symbol":md.get("symbol"),"create_ns":md.get("create_ns"),"create_signature":md.get("create_signature"),"create_fdv_usd":md.get("create_fdv_usd")})
            p["pnl_sol"] = p["cashflow_sol"] if p["basis_known"] else None
            p["realized_outcome"] = "WIN" if p["pnl_sol"] is not None and p["pnl_sol"]>0 else "LOSS" if p["pnl_sol"] is not None and p["pnl_sol"]<0 else "BREAKEVEN" if p["pnl_sol"] == 0 else "UNKNOWN"
            p["roi"] = p["pnl_sol"]/p["entry_cost_sol"] if p["pnl_sol"] is not None and p["entry_cost_sol"]>0 else None
            p["hold_seconds"] = p["final_exit_time"]-p["entry_time"] if p["final_exit_time"] and p["entry_time"] else None
            p["post_exit"] = {"coverage":"CORPUS_FIXED_HORIZONS" if md else "UNKNOWN"}
            if md and p["final_exit_time"] and md.get("create_ns"):
                exit_ms = max(0,(p["final_exit_time"] - md["create_ns"]/1e9)*1000); horizons=[1000,2000,3000,5000,10000,15000,30000,60000,120000,300000]
                later=[h for h in horizons if h > exit_ms and md.get(f"max_multiple_{h}ms") is not None]
                p["post_exit"]["exit_ms_from_create"] = exit_ms; p["post_exit"]["later_horizon_max_multiples"] = {str(h):md.get(f"max_multiple_{h}ms") for h in later}
                vals=[finite(md.get(f"max_multiple_{h}ms"),0) for h in later]; p["post_exit"]["max_launch_multiple_after_exit_horizon"] = max(vals) if vals else None
            cycles.append(p); del open_by_mint[mint]
    return cycles, list(open_by_mint.values())


def build_dev_index(cycles: list[dict[str,Any]]) -> dict[str,dict[str,Any]]:
    devs = {}
    for c in cycles:
        dev = c.get("creator")
        if not dev or c.get("pnl_sol") is None: continue
        d = devs.setdefault(dev,{"dev":dev,"label":"E4","known_complete_trades":0,"wins":0,"losses":0,"total_pnl_sol":0.0,"mints":[]})
        d["known_complete_trades"] += 1; d["wins"] += int(c["pnl_sol"]>0); d["losses"] += int(c["pnl_sol"]<0); d["total_pnl_sol"] += c["pnl_sol"]; d["mints"].append(c["mint"])
    for d in devs.values():
        n=d["known_complete_trades"]; d["win_rate"] = d["wins"]/n if n else None
        d["golden_bunch"] = bool(n>=3 and d["wins"]>=3 and d["win_rate"]>=.70 and d["total_pnl_sol"]>0)
    return devs


def views(cycles: list[dict[str,Any]], devs: dict[str,dict[str,Any]]) -> dict[str,list[str]]:
    out={"golden_bunch":[],"wins":[],"losses":[],"wins_after_exit":[],"losses_after_exit":[]}
    for c in cycles:
        if devs.get(c.get("creator"),{}).get("golden_bunch"): out["golden_bunch"].append(c["id"])
        if c.get("realized_outcome")=="WIN": out["wins"].append(c["id"])
        if c.get("realized_outcome")=="LOSS": out["losses"].append(c["id"])
        mx=c.get("post_exit",{}).get("max_launch_multiple_after_exit_horizon")
        if mx is not None and mx >= 1.50: out["wins_after_exit"].append(c["id"])
        elif mx is not None and mx < 1.05: out["losses_after_exit"].append(c["id"])
    return out


async def main_async(args: argparse.Namespace) -> int:
    out=args.output; out.mkdir(parents=True, exist_ok=True); corpus=load_corpus(args.corpus)
    async with aiohttp.ClientSession(headers={"User-Agent":"gambit-jr-e4-library/1"}) as s:
        rpc=Rpc(s); sigs=await full_signatures(rpc,out/"e4-signatures-checkpoint.json"); txrows=await all_transactions(rpc,sigs,out/"e4-transactions-checkpoint.json")
    cycles, open_cycles = reconstruct_cycles(txrows, corpus); devs=build_dev_index(cycles); cats=views(cycles,devs); counts=Counter(r["kind"] for r in txrows)
    result={"schema":SCHEMA,"built_at_ns":time.time_ns(),"wallet":WALLET,"rpc_visible_signature_count":len(sigs),"transaction_counts":dict(counts),
        "complete_cycles":len(cycles),"open_or_unknown_cycles":len(open_cycles),"known_basis_cycles":sum(c.get("pnl_sol") is not None for c in cycles),
        "known_wins":sum(c.get("realized_outcome")=="WIN" for c in cycles),"known_losses":sum(c.get("realized_outcome")=="LOSS" for c in cycles),
        "known_net_pnl_sol":sum(c.get("pnl_sol") or 0 for c in cycles),"creator_coverage":sum(bool(c.get("creator")) for c in cycles),
        "golden_devs":sum(d["golden_bunch"] for d in devs.values()),"categories":{k:len(v) for k,v in cats.items()},
        "limits":["RPC-visible history only; missing/pruned RPC history is not invented.","Only conservative single-token SOL cashflow cycles are called trades; ambiguous multi-asset activity remains unclassified.","Corpus post-exit views use fixed launch horizons and are not exact relative-to-exit ticks.","Creator/dev is known only where the retained launch corpus maps the mint; unknown creators remain UNKNOWN."],
        "phase":"E4_LIBRARY_PHASE1_COMPLETE"}
    atomic_json(out/"gambit-library-e4-summary.json",result); atomic_json(out/"gambit-library-e4-trades.json",{"schema":SCHEMA,"trades":cycles,"open_or_unknown":open_cycles,"categories":cats}); atomic_json(out/"gambit-library-e4-devs.json",{"schema":SCHEMA,"devs":devs})
    print(json.dumps(result,sort_keys=True),flush=True); return 0


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--corpus",type=Path,required=True); ap.add_argument("--output",type=Path,required=True)
    return asyncio.run(main_async(ap.parse_args()))

if __name__=="__main__": raise SystemExit(main())
