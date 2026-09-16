"""Explicitly authorized fresh-live market PAPER experiment, four models + E4.

Uses the existing read-only Solana collector; no Axiom credentials, wallet
signers, funded swaps, historical price reconstruction, or auto-relaxed filters.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
import aiohttp
import golden_horizon_live as collector
from golden_top4_engine import CampaignEngine, ARMS, VERSION, write_json
from golden_top4_e4 import Observer, WALLET

async def verify_creation(rpc, decoder, decision, session_started):
    sig = decision["create_signature"]
    tx = None
    for _ in range(8):
        await asyncio.sleep(2)
        tx = await rpc.call("getTransaction", [sig, {"encoding":"json", "commitment":"confirmed", "maxSupportedTransactionVersion":1}])
        if tx: break
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        raise RuntimeError("creation not confirmed: "+sig)
    decoded = decoder.anchor_events_from_logs((tx.get("meta") or {}).get("logMessages") or [], decoder.PUMP_PROGRAM_ID)
    bt = tx.get("blockTime")
    if not any(x.get("anchor_event")=="CreateEvent" and x.get("mint")==decision["mint"] for x in decoded) or bt is None or bt < session_started//1_000_000_000-3:
        raise RuntimeError("nonfresh/unverified creation: "+decision["mint"])
    return {"verified":True, "mint":decision["mint"], "signature":sig, "slot":tx["slot"],
        "block_time":bt, "verified_at_ns":time.time_ns(), "confirmation":"confirmed",
        "creation_transaction_actual_fee_sol":tx["meta"]["fee"]/1e9,
        "fee_attribution":"external creator transaction, NOT a Gambit fee"}

def verify_manifest(path):
    m = json.loads(path.read_text())
    if m["models"] != list(ARMS) or m["execution"] != "PAPER_ONLY" or m["target_per_model"] != 100:
        raise ValueError("unregistered experiment")
    for name, digest in m["implementation_sha256"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise ValueError("unfrozen experiment source: "+name)
    frozen = json.loads(Path("docs/research/golden-full3s-v2-freeze.json").read_text())
    for name, digest in frozen["source_sha256"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen FULL_3S_V2 source changed: "+name)
    return m

async def run(args):
    m = verify_manifest(args.manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.resume:
        previous = json.loads(args.resume.read_text())
        if previous.get("top4_campaign") != m["campaign_name"]:
            raise ValueError("attempted reuse of another campaign")
    CampaignEngine.output = args.output
    CampaignEngine.smoke = args.smoke
    CampaignEngine.require_reporter = not args.smoke
    original_source = Path(collector.__file__).read_text()
    before = "time.monotonic() >= deadline or engine.matched_closed() >= args.target"
    after = "time.monotonic() >= deadline or engine.matched_closed() >= args.target or (out / 'stop-request').exists()"
    if original_source.count(before) != 1:
        raise RuntimeError("unreviewed collector stop predicate")
    exec(compile(original_source.replace(before, after), collector.__file__, "exec"), collector.__dict__)
    collector.Engine = CampaignEngine
    collector.ARMS = ARMS
    collector.VERSION = VERSION
    collector.verify_creation = verify_creation
    collector.READ_METHODS.add("getSignaturesForAddress")
    old_journal = collector.Journal
    old_write = collector.write_json
    observer = None
    monitor_task = None
    monitor_stop = asyncio.Event()
    e4_rpc_journal = old_journal(args.output/"e4-rpc-observations.jsonl")
    decoder = collector.load_module("top4_e4_decoder", args.production_root/"src/memecoin_bot/realtime/pumpfun.py")
    previous_e4 = None
    if args.e4_resume and args.e4_resume.exists():
        previous_e4 = json.loads(args.e4_resume.read_text())

    async with aiohttp.ClientSession() as e4_session:
        e4_rpc = collector.Rpc(e4_session, e4_rpc_journal)
        class SessionJournal(old_journal):
            def add(self, kind, data, durable=False):
                nonlocal observer, monitor_task
                super().add(kind, data, durable)
                if kind == "SESSION" and observer is None:
                    started = int(data["id"].rsplit(":", 1)[1])
                    observer = Observer(e4_rpc, decoder, args.output, started, previous_e4)
                    monitor_task = asyncio.create_task(observer.run(monitor_stop))

        def publish_local(path, data):
            if path.name in ("status.json", "final.json") and isinstance(data, dict):
                engine = CampaignEngine.last_instance
                if engine is not None:
                    data = dict(data)
                    data.update(models=list(ARMS), min_verified_closed_per_model=engine.matched_closed(),
                        verified_forward_counts=engine.forward_counts(),
                        matched_closed_field_semantics="minimum verified per-model count; not identical selection",
                        v2_live_adapter_enabled=not args.smoke,
                        collector_scope="DIRECT_SOLANA_NEW_CREATIONS_NOT_AUTHENTICATED_AXIOM",
                        axiom_listing_verified=None,
                        local_four_model_tick_ms=collector.quantiles(engine.local_ticks),
                        local_four_model_selection_ms=collector.quantiles(engine.local_selection),
                        per_action_reporting_file="activity.json", total_report_events=len(engine.events),
                        operator_stop_requested=(args.output/'stop-request').exists(),
                        e4_monitor={"wallet":WALLET, "checked_ns":observer.checked_ns if observer else None,
                                    "coverage_complete":observer.complete_pages if observer else False,
                                    "actual_metrics_file":"e4-status.json", "used_in_selection":False})
            if path.name == "state.json":
                data = dict(data); data["top4_campaign"] = m["campaign_name"]
            old_write(path, data)

        collector.Journal = SessionJournal
        collector.write_json = publish_local
        rc = 2
        try:
            rc = await collector.run(args)
        finally:
            monitor_stop.set()
            if monitor_task:
                try: await asyncio.wait_for(monitor_task, timeout=120)
                except asyncio.TimeoutError:
                    monitor_task.cancel()
                    await asyncio.gather(monitor_task, return_exceptions=True)
                    if observer:
                        observer.errors.append({"error":"E4_FINAL_DRAIN_TIMEOUT"}); observer.save()
            e4_rpc_journal.close()
            collector.Journal = old_journal; collector.write_json = old_write
    if args.smoke:
        if not observer or observer.checked_ns == 0 or not observer.complete_pages:
            raise RuntimeError("E4 read-only monitor preflight did not pass")
        write_json(args.output/"top4-readiness.json", {"status":"LIVE_FEED_AND_E4_PREFLIGHT_PASS",
            "synthetic_data":False, "funded_execution":False,
            "paper_trades_in_smoke":0, "models":list(ARMS),
            "actual_axiom_data_access":False, "axiom_listing_verified":None,
            "market_smoke":json.loads((args.output/"smoke-pass.json").read_text()),
            "e4_checked_ns":observer.checked_ns, "e4_paginated_read_pass":observer.complete_pages})
    return rc

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--production-root",type=Path,required=True)
    p.add_argument("--corpus",type=Path,required=True)
    p.add_argument("--manifest",type=Path,default=Path("config/golden-top4-live.json"))
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--resume",type=Path)
    p.add_argument("--e4-resume",type=Path)
    p.add_argument("--window",default="1")
    p.add_argument("--duration",type=int,default=14400)
    p.add_argument("--target",type=int,default=100)
    p.add_argument("--smoke",action="store_true")
    a=p.parse_args()
    if a.target != 100:
        p.error("the minimum is frozen at 100 verified trades per model")
    return asyncio.run(run(a))

if __name__=="__main__":
    raise SystemExit(main())
