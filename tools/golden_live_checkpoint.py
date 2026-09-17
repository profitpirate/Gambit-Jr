#!/usr/bin/env python3
"""Persist the newest clean live-paper checkpoint while a campaign is running.

This is infrastructure only. It never selects, sizes, prices, signs or broadcasts a
trade. It snapshots state only when the collector reports a clean engine with no
open bundle/errors and the verified-forward vector has changed. That makes a
cancelled Actions job resumable without throwing away already verified trades.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

PREFIX = {
    "top4": "docs/research/golden-top4-",
    "loss2s": "docs/research/golden-loss2s-",
}


def call(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def clean_checkpoint(state: dict, status: dict) -> bool:
    eng = state.get("engine") or {}
    if eng.get("errors") or eng.get("bundles"):
        return False
    if eng.get("clean") is False:
        return False
    if status.get("errors") or status.get("active_bundles"):
        return False
    return bool(status.get("verified_forward_counts"))


def fingerprint(status: dict) -> str:
    payload = {
        "verified_forward_counts": status.get("verified_forward_counts") or {},
        "matched_closed": status.get("matched_closed"),
        "journal_head": status.get("journal_head"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def persist(kind: str, root: Path, branch: str, status: dict, state_path: Path) -> None:
    prefix = PREFIX[kind]
    repo = Path.cwd()
    for attempt in range(5):
        tmp = Path(tempfile.mkdtemp(prefix=f"golden-{kind}-checkpoint-"))
        try:
            call(["git", "fetch", "origin", branch], cwd=repo)
            call(["git", "worktree", "add", "--detach", str(tmp), "FETCH_HEAD"], cwd=repo)
            dest = tmp / f"{prefix}state.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(state_path, dest)
            meta = {
                "schema": "golden-live-clean-checkpoint-1",
                "kind": kind,
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "checked_ns": time.time_ns(),
                "verified_forward_counts": status.get("verified_forward_counts"),
                "min_verified_closed_per_model": status.get("min_verified_closed_per_model"),
                "matched_closed": status.get("matched_closed"),
                "journal_head": status.get("journal_head"),
                "source_status_checked_ns": status.get("checked_ns"),
                "clean_engine_required": True,
                "paper_only": True,
                "state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            }
            (tmp / f"{prefix}checkpoint.json").write_text(json.dumps(meta, sort_keys=True) + "\n")
            call(["git", "config", "user.name", "gambit-live-checkpoint"], cwd=tmp)
            call(["git", "config", "user.email", "actions@users.noreply.github.com"], cwd=tmp)
            call(["git", "add", "--", f"{prefix}state.json", f"{prefix}checkpoint.json"], cwd=tmp)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tmp)
            if diff.returncode == 0:
                return
            call(["git", "commit", "-m", f"data({kind}): persist clean live checkpoint [skip ci]"], cwd=tmp)
            pushed = subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=tmp, text=True, capture_output=True)
            if pushed.returncode == 0:
                return
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tmp)], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(tmp, ignore_errors=True)
        time.sleep(1 + attempt)
    raise RuntimeError("unable to persist clean checkpoint after concurrent branch updates")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=sorted(PREFIX), required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--interval", type=float, default=3.0)
    a = p.parse_args()
    stop = a.root / "stop-checkpoint-publisher"
    last = None
    while not stop.exists():
        state_path = a.root / "state.json"
        status_path = a.root / "status.json"
        try:
            if state_path.exists() and status_path.exists():
                status = json.loads(status_path.read_text())
                state = json.loads(state_path.read_text())
                fp = fingerprint(status)
                if fp != last and clean_checkpoint(state, status):
                    persist(a.kind, a.root, a.branch, status, state_path)
                    last = fp
                    print(json.dumps({"checkpoint_persisted": True, "kind": a.kind,
                                      "verified_forward_counts": status.get("verified_forward_counts")}), flush=True)
        except Exception as exc:
            print(json.dumps({"checkpoint_persist_error": type(exc).__name__, "message": str(exc)[:240]}), flush=True)
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
