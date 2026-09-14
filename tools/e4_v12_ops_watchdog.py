#!/usr/bin/env python3
"""Independent OS-process watchdog and live status publisher for thesis tests."""
from __future__ import annotations
import argparse, base64, json, os, signal, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

TERMINAL={"TECHNICAL_FAILURE","OPERATIONS_STOPPED","INSUFFICIENT_SAMPLE","SAMPLE_TARGET_REACHED"}

def inspect_status(s:dict[str,Any],now:float)->tuple[list[str],list[str]]:
    warnings=[];fatal=[]
    age=now-float(s.get("checked_at",0))
    if s.get("status")=="PREPARING":return warnings,fatal
    if age>30:fatal.append("RUNNER_HEARTBEAT_STALE")
    if s.get("status") in TERMINAL:return warnings,fatal
    if s.get("phase")!="online":return warnings,["UNRECOGNISED_OPERATIONAL_STATE"]
    if now-float(s.get("last_event_at",now))>90:fatal.append("LIVE_FEED_STALLED")
    if not s.get("feed",{}).get("fresh"):fatal.append("SOL_USD_STALE")
    counts=s.get("counts",{})
    launches=counts.get("launches_seen",0)
    if counts.get("scored",0)>0 and s.get("score_queue_depth",0)>0 and now-s.get("last_score_at",now)>30:
        fatal.append("SCORING_STALLED")
    if counts.get("invalid_features",0)>=5 and counts.get("invalid_features",0)/max(1,launches)>.1:
        fatal.append("FEATURE_CONTRACT_FAILURE_RATE")
    if s.get("score_queue_depth",0)>32:warnings.append("SCORING_BACKLOG")
    if s.get("scoring_ms_p95",0) and s["scoring_ms_p95"]>100:warnings.append("HIGH_SCORING_LATENCY")
    for name,account in s.get("accounts",{}).items():
        if account.get("task_errors") or account.get("unpriced_exposure"):
            fatal.append(name+":EXECUTION_FAILURE")
        if s.get("elapsed_seconds",0)>=900 and counts.get("scored",0)>=250 and not account.get("signals"):
            warnings.append(name+":NO_SIGNALS_ABOVE_UNCHANGED_THRESHOLD")
        reasons=s.get("scoring",{}).get(name,{}).get("reasons",{})
        if reasons.get("STALE_DECISION",0)>=10 and reasons.get("STALE_DECISION",0)/max(1,counts.get("scored",0))>.1:
            fatal.append(name+":DECISIONS_TOO_LATE")
    fatal.extend(s.get("failures",[]))
    return sorted(set(warnings)),sorted(set(fatal))

def publish(payload,repo,branch,path):
    """Only publish to the same authorised repository; never write secrets."""
    token=os.environ.get("GH_TOKEN")
    if not token:raise RuntimeError("GH_TOKEN missing for operations visibility")
    url=f"https://api.github.com/repos/{repo}/contents/{path}"
    from urllib.parse import quote
    headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json",
             "X-GitHub-Api-Version":"2022-11-28","User-Agent":"Gambit-Operations-Supervisor"}
    sha=None
    try:
        req=urllib.request.Request(url+"?ref="+quote(branch,safe=""),headers=headers)
        with urllib.request.urlopen(req,timeout=8) as r:sha=json.load(r).get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code!=404:raise
    data={"message":"ops(e4-v12): publish supervised live heartbeat [skip ci]","branch":branch,
          "content":base64.b64encode(json.dumps(payload,indent=2,sort_keys=True).encode()).decode()}
    if sha:data["sha"]=sha
    req=urllib.request.Request(url,headers=headers,data=json.dumps(data).encode(),method="PUT")
    with urllib.request.urlopen(req,timeout=8) as r:json.load(r)

def atomic(path,value):
    tmp=path.with_suffix(".tmp");tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n");tmp.replace(path)

def run(args):
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    statusfile=out/"status.json";opsfile=out/"supervisor.json"
    if statusfile.exists():raise RuntimeError("refusing to overwrite existing live account session")
    command=args.command
    if command and command[0]=="--":command=command[1:]
    if not command:raise RuntimeError("no runner command")
    child=subprocess.Popen(command,start_new_session=True)
    began=time.time();last_publish=0.;publish_failures=0;last_signals=0;last_signal_at=began
    last_signal_scored=0;last_score_count=0;stop_reason=None;term_at=None;final=None
    try:
        while True:
            now=time.time();s={}
            if statusfile.exists():
                try:s=json.loads(statusfile.read_text())
                except (OSError,json.JSONDecodeError):pass
            warnings,fatal=inspect_status(s,now) if s else ([],[])
            if s.get("status")=="PREPARING" and now-began>300:fatal.append("MODEL_STARTUP_TIMEOUT")
            if not s and now-began>60:fatal.append("RUNNER_NEVER_PUBLISHED_HEARTBEAT")
            total_signals=sum(a.get("signals",0) for a in s.get("accounts",{}).values())
            scored=s.get("counts",{}).get("scored",0)
            if total_signals>last_signals:
                last_signals=total_signals;last_signal_at=now;last_signal_scored=scored
            if s.get("phase")=="online" and not last_score_count and scored:
                last_signal_at=now;last_signal_scored=scored
            last_score_count=scored
            if now-last_signal_at>=args.no_signal_stop_seconds and scored-last_signal_scored>=args.no_signal_stop_launches:
                fatal.append("NO_SIGNAL_RESOURCE_GUARD")
            if stop_reason: fatal.append(stop_reason)
            payload={"supervisor_checked_at":now,"runner_pid":child.pid,"run_id":os.environ.get("GITHUB_RUN_ID"),
                     "runner":s,"warnings":warnings,"fatal":sorted(set(fatal)),
                     "supervisor_state":"STOPPING" if term_at else "WATCHING",
                     "last_signal_age_seconds":now-last_signal_at,
                     "resource_limits":{"no_signal_stop_seconds":args.no_signal_stop_seconds,
                                        "no_signal_stop_launches":args.no_signal_stop_launches,
                                        "max_runner_seconds":args.max_runner_seconds},
                     "publication_failures":publish_failures}
            atomic(opsfile,payload)
            if now-last_publish>=args.publish_seconds or child.poll() is not None or (fatal and term_at is None):
                if args.repo:
                    try:
                        publish(payload,args.repo,args.branch,args.status_path)
                        publish_failures=0
                    except Exception as exc:
                        publish_failures+=1
                        print("::warning::Live-status publication failed: "+type(exc).__name__,flush=True)
                last_publish=now
            if publish_failures>=3:fatal.append("LIVE_VISIBILITY_UNAVAILABLE")
            if now-began>args.max_runner_seconds:fatal.append("RESOURCE_WALL_CLOCK_LIMIT")
            if fatal and term_at is None and child.poll() is None:
                stop_reason=fatal[0];term_at=now
                print("::error::Operations supervisor stopping runner: "+",".join(fatal),flush=True)
                os.killpg(child.pid,signal.SIGTERM)
            if term_at and now-term_at>15 and child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL)
            if child.poll() is not None:
                final=payload
                break
            time.sleep(args.poll_seconds)
    finally:
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
    final=final or {}
    final["supervisor_state"]="FINISHED";final["runner_exit_code"]=child.returncode
    final["stop_reason"]=stop_reason
    s=json.loads(statusfile.read_text()) if statusfile.exists() else {}
    final["runner"]=s
    final["sample_complete"]=bool(s.get("status")=="SAMPLE_TARGET_REACHED" and
        all(a.get("closed_trades",0)>=s.get("sample_target_per_thesis",20) for a in s.get("accounts",{}).values())
        and len(s.get("accounts",{}))==2 and not stop_reason)
    final["performance_pass"]=False  # Completion is NEVER a performance certificate.
    final["result_class"]="FORWARD_SAMPLE_READY_FOR_REVIEW" if final["sample_complete"] else "INCOMPLETE_OR_BLOCKED"
    atomic(opsfile,final)
    if args.repo:
        try:publish(final,args.repo,args.branch,args.status_path)
        except Exception as exc:print("::warning::Final publication failed: "+type(exc).__name__,flush=True)
    return 0 if final["sample_complete"] else 3

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir",required=True)
    p.add_argument("--repo",default=os.environ.get("GITHUB_REPOSITORY",""))
    p.add_argument("--branch",default="codex/e4-v12-ops-supervised-live")
    p.add_argument("--status-path",default="docs/research/e4-v12-live-ops-status.json")
    p.add_argument("--poll-seconds",type=float,default=5)
    p.add_argument("--publish-seconds",type=float,default=60)
    p.add_argument("--no-signal-stop-seconds",type=float,default=3600)
    p.add_argument("--no-signal-stop-launches",type=int,default=1000)
    p.add_argument("--max-runner-seconds",type=float,default=20400)
    p.add_argument("command",nargs=argparse.REMAINDER)
    return run(p.parse_args())
if __name__=="__main__":raise SystemExit(main())
