"""Reproducible OFFLINE certification. Synthetic data is never live evidence."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import platform
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from golden_full3s_v2 import Runtime, SingleEngine, Config, base, ARM, BASELINE_COMMIT
from golden_full3s_policy import EvidenceIndex, Selector
from golden_full3s_store import Store, encode, digest
from test_golden_full3s_v2 import T0, state, decision, outcome


def stress(paths: int) -> dict:
    rng=random.Random(317_20260916)
    closed=failed=blocked=events=0
    start=time.perf_counter()
    for i in range(paths):
        e=SingleEngine();mint=f"fixture-{i}";d=decision(mint,tier=rng.choice(tuple(base.TIERS)))
        assert e.submit(d,state(),T0)
        x=30.0;y=1e9
        scenario=i%8
        for dt in range(0,24501,250):
            ns=T0+dt*1_000_000
            if dt and dt<=4000:
                r=rng.uniform(-.075,.075)
                if scenario==1 and dt==500:r=-.40
                if scenario==2 and dt==500:r=.65
                if scenario==3 and dt==3250:r=-.45
                if scenario==4:r=0
                x=max(.01,x*(1+r));y=max(1.0,y/(1+r))
            s=state(ns,x,y)
            # Synthetic outages are fed explicitly, never backfilled as live.
            feed=ns-4_000_000_000 if scenario==5 and 2500<=dt<=4250 else ns
            if scenario==6 and dt==750:
                s["complete"]=True
            if scenario==7 and dt==500:
                s=state(T0+1,300,1e7)
            e.tick(ns,{mint:s},feed);events+=1
            assert e.accounts[ARM]["cash"]>=-1e-8
            for b in e.bundles.values():
                p=b["positions"][ARM]
                assert 0<=p["remaining"]<=p["original"]+1e-7
            if e.errors:break
        if e.errors:
            assert not e.accepting
            assert e.bundles
            blocked+=1
        else:
            e.validate()
            closed+=e.matched_closed()
            assert not e.bundles
            for p in e.accounts[ARM]["ledger"]:
                acts=[a for a in p["actions"] if a["outcome"]=="MODELLED_FILL"]
                assert [a["side"] for a in acts]==["BUY","SELL"]
                assert p["horizon_ms"]==3000 and p["hold_ms"]>=3250
                assert all(a["actual_transaction_signature"] is None for a in p["actions"])
        failed+=sum(a["outcome"]=="MODELLED_LANDED_FAILURE" for p in e.accounts[ARM]["ledger"] for a in p["actions"])
    return {"status":"PASS","seed":317_20260916,"synthetic_paths":paths,
            "synthetic_event_ticks":events,"synthetic_closed_positions":closed,
            "expected_fail_closed_migration_or_exhaustion_paths":blocked,
            "modeled_failed_attempts_in_closed_fixtures":failed,
            "elapsed_seconds":time.perf_counter()-start,"live_coins_or_trades":0}


def kill_child(path: str, stage: str, event: dict) -> None:
    s=Store(path)
    def trans(snap):
        r=Runtime.restore(snap,process_restart=False)
        if event["kind"]=="submit":r.submit(event["decision"],event["state"],event["now"])
        else:r.tick(event["now"],event["states"],event["now"])
        return r.snapshot()
    def crash(where):
        if where==stage:os._exit(77)
    s.apply("crash-test",event,trans,fault=crash)
    os._exit(0)


def chaos(repetitions: int) -> dict:
    total=0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(repetitions):
            for lifecycle in ("ENTRY_INTENT","ENTRY_FILL","EXIT_INTENT","EXIT_FILL"):
                r=Runtime(fixture_mode=True)
                if lifecycle!="ENTRY_INTENT":r.submit(decision(),state(),T0)
                if lifecycle in ("EXIT_INTENT","EXIT_FILL"):
                    ns=T0+250_000_000;r.tick(ns,{"fixture-coin":state(ns)},ns)
                if lifecycle=="EXIT_FILL":
                    # Fresh quotes throughout avoid manufacturing a feed gap.
                    for dt in range(500,3251,250):
                        ns=T0+dt*1_000_000;r.tick(ns,{"fixture-coin":state(ns)},ns)
                now={"ENTRY_INTENT":T0,"ENTRY_FILL":T0+250_000_000,"EXIT_INTENT":T0+3_250_000_000,"EXIT_FILL":T0+3_500_000_000}[lifecycle]
                event={"kind":"submit","now":now,"decision":decision(),"state":state()} if lifecycle=="ENTRY_INTENT" else {"kind":"tick","now":now,"states":{"fixture-coin":state(now)}}
                for fault in ("BEFORE_TRANSITION","BEFORE_COMMIT","AFTER_COMMIT"):
                    path=Path(tmp)/f"{i}-{lifecycle}-{fault}.db"
                    original=r.snapshot();s=Store(path,original);s.close()
                    cmd=[sys.executable,__file__,"--kill-worker",str(path),"--fault",fault,"--event",json.dumps(event)]
                    result=subprocess.run(cmd,timeout=20,capture_output=True,text=True)
                    assert result.returncode==77,(result.returncode,result.stderr)
                    s=Store(path);s.verify()
                    changed=s.read()
                    if fault!="AFTER_COMMIT":assert encode(changed)==encode(original)
                    else:
                        assert s.verify()["events"]==1
                        assert not s.apply("crash-test",event,lambda x:x)
                        # Restoring any open state retains inventory and marks interruption.
                        recovered=Runtime.restore(changed)
                        for engine in (recovered.candidate,recovered.observer):
                            for b in engine.bundles.values():
                                assert b["mint"] in engine.uncertain_mints
                    s.close();total+=1
    return {"status":"PASS","actual_process_terminations":total,"lifecycle_boundaries":4,
            "commit_fault_points":3,"scope":"SQLite journal on one host, not validator or server failover"}


def latency(probes: int) -> dict:
    import golden_horizon_engine as original
    normal=original.Engine(original.Config())
    single=SingleEngine()
    d=decision();normal.submit(d,state(),T0);single.submit(d,state(),T0)
    ns=T0+250_000_000
    normal.tick(ns,{d["mint"]:state(ns)},ns);single.tick(ns,{d["mint"]:state(ns)},ns)
    old=[];new=[]
    for i in range(probes):
        now=ns+(i+1)*1000;s={d["mint"]:state(now)}
        if i%2:
            a=time.perf_counter_ns();single.tick(now,s,now);new.append((time.perf_counter_ns()-a)/1e6)
            a=time.perf_counter_ns();normal.tick(now,s,now);old.append((time.perf_counter_ns()-a)/1e6)
        else:
            a=time.perf_counter_ns();normal.tick(now,s,now);old.append((time.perf_counter_ns()-a)/1e6)
            a=time.perf_counter_ns();single.tick(now,s,now);new.append((time.perf_counter_ns()-a)/1e6)
    e=EvidenceIndex(Config().fingerprint(),fixture_mode=True)
    for i in range(100):e.add(outcome(i))
    sel=Selector(e);q={"supported":True,"ns":T0,"impact_bps":40,"roundtrip_drag_bps":550}
    times=[]
    for _ in range(probes):
        a=time.perf_counter_ns();sel.evaluate(d,q,.25,T0);times.append((time.perf_counter_ns()-a)/1e6)
    commit=[]
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(Path(tmp)/"bench.db",Runtime(fixture_mode=True).snapshot())
        for i in range(300):
            a=time.perf_counter_ns();store.apply(str(i),{"counter":i},lambda s:s);commit.append((time.perf_counter_ns()-a)/1e6)
        store.verify();store.close()
    return {"measurement":"LOCAL_CPU_AND_LOCAL_DURABLE_COMMIT_ONLY","old_twelve_arm_tick_ms":base.quantiles(old),
            "new_single_arm_tick_ms":base.quantiles(new),"cost_aligned_selection_ms":base.quantiles(times),
            "durable_empty_runtime_snapshot_commit_ms":base.quantiles(commit),
            "local_tick_median_speedup_vs_twelve_arms":base.quantiles(old)["median"]/base.quantiles(new)["median"],
            "note":"Workload reduction is twelve arms to one; not a like-for-like 12x algorithm or network speed claim.",
            "configured_inclusion_delay_ms":250,"measured_chain_execution_ms":None,"measured_axiom_execution_ms":None}


def main() -> int:
    p=argparse.ArgumentParser();p.add_argument("--output",type=Path,default=Path("artifacts/full3s-v2"))
    p.add_argument("--paths",type=int,default=10000);p.add_argument("--crash-repetitions",type=int,default=10)
    p.add_argument("--probes",type=int,default=3000);p.add_argument("--kill-worker");p.add_argument("--fault");p.add_argument("--event")
    a=p.parse_args()
    if a.kill_worker:kill_child(a.kill_worker,a.fault,json.loads(a.event));return 0
    a.output.mkdir(parents=True,exist_ok=True)
    report={"status":"BUILD_VERIFICATION_IN_PROGRESS","model":"FULL_3S_V2","baseline_commit":BASELINE_COMMIT,
            "python":sys.version,"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,
            "live_testing_started":False,"live_trades":0,"profitability_validated":False}
    report["stress"]=stress(a.paths)
    print(json.dumps(report["stress"]),flush=True)
    report["chaos"]=chaos(a.crash_repetitions)
    print(json.dumps(report["chaos"]),flush=True)
    report["latency"]=latency(a.probes)
    assert report["latency"]["new_single_arm_tick_ms"]["p95"]<5.0
    assert report["latency"]["cost_aligned_selection_ms"]["p95"]<5.0
    # Local regression ceilings are not promises about another host/provider.
    report["status"]="OFFLINE_BUILD_STRESS_PASS"
    report["sources"]={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in
        sorted(Path("tools").glob("golden_full3s_*.py"))}
    (a.output/"certification.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    print(json.dumps(report["latency"]),flush=True)
    return 0


if __name__=="__main__":raise SystemExit(main())
