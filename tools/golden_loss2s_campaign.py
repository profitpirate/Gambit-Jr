"""Run/persist the fresh six-arm 2s-loss A/B without altering top4 evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from golden_loss2s_engine import ARMS, write_json
from golden_loss2s_prepare import prepare
from golden_loss2s_report import report, validate_state

PREFIX = "docs/research/golden-loss2s-"


def command(args, log):
    with log.open("w") as f:
        return subprocess.call(args, stdout=f, stderr=subprocess.STDOUT)


def persist(paths):
    keep = Path("artifacts/persist")
    keep.mkdir(parents=True, exist_ok=True)
    for name, src in paths.items():
        if src.exists():
            shutil.copyfile(src, keep / name)

    branch = os.environ["BRANCH"]
    subprocess.run(["git", "fetch", "origin", branch], check=True)
    subprocess.run(["git", "checkout", "-B", "loss2s-evidence", "FETCH_HEAD"], check=True)

    written = []
    for saved in keep.iterdir():
        dest = Path(PREFIX + saved.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(saved, dest)
        written.append(str(dest))
    if Path("config/golden-loss2s-live.json").exists():
        written.append("config/golden-loss2s-live.json")

    subprocess.run(["git", "config", "user.name", "gambit-loss2s-research"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "actions@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", "--", *written], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode:
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "data(loss2s): preserve forward campaign evidence [skip ci]",
            ],
            check=True,
        )
        subprocess.run(["git", "push", "origin", "HEAD:" + branch], check=True)


def launch(args):
    cfg = prepare()
    root = Path("artifacts/live")
    root.mkdir(parents=True, exist_ok=True)

    existing = Path(PREFIX + "state.json")
    resume = None
    e4resume = None
    if existing.exists():
        previous = json.loads(existing.read_text())
        e = validate_state(previous, require_target=False)
        if previous.get("top4_campaign") != cfg["campaign_name"]:
            raise ValueError("different loss2s campaign checkpoint")
        expected_manifest = hashlib.sha256(
            Path("config/golden-loss2s-live.json").read_bytes()
        ).hexdigest()
        if previous["manifest_hash"] != expected_manifest:
            raise ValueError("manifest mismatch; no account reset allowed")
        if min(e.forward_counts().values()) >= 100:
            print("ALREADY_AT_TARGET_NO_ADDITIONAL_RUN", flush=True)
            return 0
        resume = root / "resume.json"
        shutil.copyfile(existing, resume)
        prior = Path(PREFIX + "e4.json")
        if prior.exists():
            e4resume = root / "e4-resume.json"
            shutil.copyfile(prior, e4resume)

    readiness = json.loads(Path("artifacts/smoke/readiness.json").read_text())
    if readiness["status"] != "SOLANA_AND_E4_PREFLIGHT_PASS":
        raise ValueError("read-only source preflight not passed")

    write_json(
        root / "status.json",
        {
            "status": "INITIALIZING_LIVE_WINDOW",
            "checked_ns": time.time_ns(),
            "models": list(ARMS),
            "starting_sol_per_model": 3,
            "resuming": bool(resume),
            "paper_only": True,
            "actual_profit_sol": None,
            "collector_active": False,
            "preflight": readiness,
            "run_id": os.environ.get("GITHUB_RUN_ID"),
        },
    )

    publog = (root / "publisher.log").open("w")
    publisher = subprocess.Popen(
        [sys.executable, "tools/golden_loss2s_publish.py", "--root", str(root)],
        stdout=publog,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        hp = root / "publisher-health.json"
        if hp.exists() and json.loads(hp.read_text()).get("healthy"):
            break
        if publisher.poll() is not None:
            raise RuntimeError("reporter startup failed")
        time.sleep(1)
    else:
        publisher.terminate()
        raise RuntimeError("no acknowledged reporting channel")

    cmd = [
        sys.executable,
        "tools/golden_loss2s_solana.py",
        "--production-root",
        "production",
        "--corpus",
        args.corpus,
        "--manifest",
        "config/golden-loss2s-live.json",
        "--output",
        str(root),
        "--duration",
        str(args.duration),
        "--window",
        os.environ.get("GITHUB_RUN_ID", "local"),
    ]
    if resume:
        cmd += ["--resume", str(resume)]
    if e4resume:
        cmd += ["--e4-resume", str(e4resume)]

    rc = command(cmd, root / "live.log")
    (root / "stop-publisher").touch()
    try:
        pubrc = publisher.wait(timeout=120)
    except subprocess.TimeoutExpired:
        publisher.terminate()
        pubrc = 2
    publog.close()

    paths = {
        "status.json": root / "status.json",
        "state.json": root / "state.json",
        "e4.json": root / "e4-status.json",
        "activity.json": root / "activity.json",
        "publisher.json": root / "publisher-health.json",
        "readiness.json": Path("artifacts/smoke/readiness.json"),
    }

    error = None
    finished = False
    try:
        if rc or pubrc:
            raise RuntimeError(
                f"collector={rc}, publisher={pubrc}; failed evidence is retained"
            )
        state = json.loads((root / "state.json").read_text())
        engine = validate_state(state, require_target=False)
        finished = min(engine.forward_counts().values()) >= 100
        if finished:
            report(root / "state.json", Path("artifacts/final"))
            paths.update(
                {
                    "final-results.json": Path("artifacts/final/results.json"),
                    "final-report.md": Path("artifacts/final/report.md"),
                }
            )
        status = json.loads((root / "status.json").read_text())
        status.update(
            collector_active=False,
            completion_verified=finished,
            status="COMPLETE_100_PER_MODEL" if finished else "CLEAN_WINDOW_END_INCOMPLETE",
            verified_forward_counts=engine.forward_counts(),
            next_window_must_preserve_state=True,
        )
        write_json(root / "status.json", status)
    except Exception as exc:
        error = str(exc)
        path = root / "status.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        status.update(
            status="BLOCKED_PRESERVED_EVIDENCE",
            collector_active=False,
            completion_verified=False,
            operational_blocker=error,
        )
        write_json(path, status)

    persist(paths)
    if error:
        raise RuntimeError(error)
    print(
        "CAMPAIGN_COMPLETE" if finished else "CLEAN_WINDOW_END_NOT_COMPLETE",
        flush=True,
    )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--corpus")
    p.add_argument("--duration", type=int, default=14400)
    a = p.parse_args()
    if a.prepare:
        prepare()
        return 0
    if not a.corpus:
        p.error("--corpus is required")
    return launch(a)


if __name__ == "__main__":
    raise SystemExit(main())
