#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def py(*parts: str) -> list[str]:
    return [sys.executable, *parts]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run complete Gambit E4 V10 pipelines")
    value.add_argument("--social", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--learner", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--live", action="store_true")
    value.add_argument("--social-config", type=Path, default=Path("models/e4/e4-social-sources.json"))
    value.add_argument("--restart-delay", type=float, default=1.0)
    return value


def main() -> int:
    args = parser().parse_args()
    children: dict[str, subprocess.Popen[str]] = {}
    stopping = False

    def spawn(name: str, command: list[str]) -> None:
        print(f"starting {name}: {' '.join(command)}", flush=True)
        children[name] = subprocess.Popen(command, text=True, env=os.environ.copy())

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        for child in children.values():
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    command = py("-m", "memecoin_bot.e4_exec_v10_complete")
    if args.live:
        command.append("--live")
    spawn("execution", command)
    if args.social:
        spawn("social", py("scripts/e4_social_stream_v10.py", "--config", str(args.social_config)))
    if args.learner:
        spawn("learner", py("scripts/e4_creator_learner_v10.py"))

    while not stopping:
        for name, child in tuple(children.items()):
            code = child.poll()
            if code is None:
                continue
            if name == "execution":
                print(f"execution exited code={code}; stopping companion pipelines", flush=True)
                stop()
                break
            print(f"{name} exited code={code}; restarting", flush=True)
            time.sleep(max(0.1, args.restart_delay))
            if name == "social":
                spawn(name, py("scripts/e4_social_stream_v10.py", "--config", str(args.social_config)))
            elif name == "learner":
                spawn(name, py("scripts/e4_creator_learner_v10.py"))
        time.sleep(0.1)

    deadline = time.monotonic() + 5.0
    for child in children.values():
        if child.poll() is None:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
    execution = children.get("execution")
    return execution.returncode if execution and execution.returncode is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
