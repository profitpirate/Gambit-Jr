#!/usr/bin/env python3
"""Independent watchdog for Golden Buyer Reputation live-paper sessions."""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def inspect(s: dict[str, Any], now: float) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    fatal: list[str] = []
    if not s:
        return warnings, fatal
    if now - float(s.get("checked_at", 0)) > 60:
        fatal.append("RUNNER_HEARTBEAT_STALE")
    if s.get("status") != "RUNNING":
        return warnings, fatal
    if now - float(s.get("last_event_at", now)) > 90:
        fatal.append("LIVE_FEED_STALLED")
    counts = s.get("counts", {})
    launches = int(counts.get("launches_seen", 0))
    decisions = int(counts.get("decisions", 0))
    outcomes = int(counts.get("benchmark_outcomes", 0))
    buyers = int(counts.get("early_buyers_seen", 0))
    if s.get("queue_depth", 0) > 1000:
        warnings.append("MARKET_QUEUE_BACKLOG")
    if s.get("queue_depth", 0) > 5000:
        fatal.append("MARKET_QUEUE_OVERFLOW_RISK")
    if launches >= 250 and decisions < launches * 0.8:
        fatal.append("DECISION_PIPELINE_MISSING_LAUNCHES")
    if decisions >= 250 and buyers == 0:
        fatal.append("EARLY_BUYER_INPUT_PIPELINE_EMPTY")
    if decisions >= 250 and outcomes < decisions * 0.5:
        fatal.append("REPUTATION_OUTCOME_PIPELINE_STALLED")
    account = s.get("account", {})
    if account.get("task_errors") or account.get("unresolved_exposure"):
        fatal.append("PAPER_EXECUTION_FAILURE")
    if decisions >= 1000 and int(account.get("signals", 0)) == 0:
        warnings.append("NO_SIGNALS_YET_RULE_UNCHANGED")
    return sorted(set(warnings)), sorted(set(fatal))


def atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def publish(payload: dict[str, Any], repo: str, branch: str, path: str) -> None:
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN missing")
    from urllib.parse import quote
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Gambit-Golden-Thesis-Watchdog",
    }
    sha = None
    try:
        req = urllib.request.Request(url + "?ref=" + quote(branch, safe=""), headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            sha = json.load(response).get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    body = {
        "message": "ops(golden): publish live paper heartbeat [skip ci]",
        "branch": branch,
        "content": base64.b64encode(json.dumps(payload, indent=2, sort_keys=True).encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode(), method="PUT")
    with urllib.request.urlopen(req, timeout=8) as response:
        json.load(response)


def run(args: argparse.Namespace) -> int:
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    status_path = out / "status.json"
    supervisor_path = out / "supervisor.json"
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise RuntimeError("runner command missing")
    child = subprocess.Popen(cmd, start_new_session=True)
    began = time.time()
    last_publish = 0.0
    publish_failures = 0
    stop_reason = None
    term_at = None
    last_payload: dict[str, Any] = {}
    try:
        while True:
            now = time.time()
            status: dict[str, Any] = {}
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    status = {}
            warnings, fatal = inspect(status, now) if status else ([], [])
            if not status and now - began > 180:
                fatal.append("RUNNER_NEVER_PUBLISHED_STATUS")
            if publish_failures >= 3:
                fatal.append("LIVE_VISIBILITY_UNAVAILABLE")
            if now - began > args.max_runner_seconds and child.poll() is None:
                fatal.append("SUPERVISOR_WALL_CLOCK_LIMIT")
            payload = {
                "supervisor_checked_at": now,
                "supervisor_state": "STOPPING" if term_at else "WATCHING",
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "runner_pid": child.pid,
                "runner": status,
                "warnings": warnings,
                "fatal": sorted(set(fatal)),
                "publication_failures": publish_failures,
                "stop_reason": stop_reason,
            }
            atomic(supervisor_path, payload)
            last_payload = payload
            if now - last_publish >= args.publish_seconds or child.poll() is not None or (fatal and term_at is None):
                try:
                    if args.repo:
                        publish(payload, args.repo, args.branch, args.status_path)
                    publish_failures = 0
                except Exception as exc:
                    publish_failures += 1
                    print(f"::warning::watchdog publication failed: {type(exc).__name__}", flush=True)
                last_publish = now
            if fatal and term_at is None and child.poll() is None:
                stop_reason = fatal[0]
                term_at = now
                print("::error::watchdog stopping live paper runner: " + ",".join(fatal), flush=True)
                os.killpg(child.pid, signal.SIGTERM)
            if term_at and now - term_at > 15 and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            if child.poll() is not None:
                break
            time.sleep(args.poll_seconds)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    final_status = {}
    if status_path.exists():
        try:
            final_status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    last_payload.update({
        "supervisor_state": "FINISHED",
        "runner_exit_code": child.returncode,
        "runner": final_status,
        "stop_reason": stop_reason,
        "sample_complete": bool(final_status.get("status") == "SAMPLE_TARGET_REACHED" and final_status.get("account", {}).get("closed_trades", 0) >= args.target_closed_trades and not stop_reason),
        "result_class": "FORWARD_SAMPLE_READY_FOR_REVIEW" if final_status.get("status") == "SAMPLE_TARGET_REACHED" and not stop_reason else "INCOMPLETE_OR_BLOCKED",
        "performance_pass": False,
    })
    atomic(supervisor_path, last_payload)
    try:
        if args.repo:
            publish(last_payload, args.repo, args.branch, args.status_path)
    except Exception as exc:
        print(f"::warning::final watchdog publication failed: {type(exc).__name__}", flush=True)
    if stop_reason:
        return 2
    if final_status.get("status") == "TECHNICAL_FAILURE":
        return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--branch", default="codex/golden-thesis-buyer-reputation-v1")
    p.add_argument("--status-path", default="docs/research/golden-buyer-reputation-live-status.json")
    p.add_argument("--poll-seconds", type=float, default=5.0)
    p.add_argument("--publish-seconds", type=float, default=120.0)
    p.add_argument("--max-runner-seconds", type=float, default=19_500.0)
    p.add_argument("--target-closed-trades", type=int, default=50)
    p.add_argument("command", nargs=argparse.REMAINDER)
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
