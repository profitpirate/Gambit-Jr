"""Host-level V12 supervisor with heartbeat/hang/crash-loop protection."""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Supervisor:
    def __init__(
        self,
        command: list[str],
        *,
        heartbeat: Path,
        kill_switch: Path,
        stale_seconds: float,
        crash_limit: int,
        crash_window_seconds: float,
        max_backoff_seconds: float,
    ):
        self.command = command
        self.heartbeat = heartbeat
        self.kill_switch = kill_switch
        self.stale_seconds = stale_seconds
        self.crash_limit = crash_limit
        self.crash_window_seconds = crash_window_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.child: subprocess.Popen[bytes] | None = None
        self.stop = False
        self.crashes: collections.deque[float] = collections.deque()

    def request_stop(self, *_: object) -> None:
        self.stop = True
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def _heartbeat_age(self) -> float:
        if not self.heartbeat.exists():
            return float("inf")
        try:
            payload = json.loads(self.heartbeat.read_text(encoding="utf-8"))
            ts_ns = int(payload.get("ts_ns") or 0)
            if ts_ns > 0:
                return max(0.0, (time.time_ns() - ts_ns) / 1e9)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return max(0.0, time.time() - self.heartbeat.stat().st_mtime)

    def _terminate_hung(self) -> None:
        if not self.child or self.child.poll() is not None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)

    def _record_crash(self) -> bool:
        now = time.monotonic()
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > self.crash_window_seconds:
            self.crashes.popleft()
        return len(self.crashes) >= self.crash_limit

    def _trip_crash_loop(self) -> None:
        self.kill_switch.parent.mkdir(parents=True, exist_ok=True)
        self.kill_switch.write_text(
            json.dumps(
                {
                    "reason": "supervisor_crash_loop",
                    "crashes": len(self.crashes),
                    "ts_ns": time.time_ns(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(self.kill_switch, 0o600)

    def run(self) -> int:
        backoff = 1.0
        while not self.stop:
            if self.kill_switch.exists():
                return 2
            self.heartbeat.unlink(missing_ok=True)
            started = time.monotonic()
            self.child = subprocess.Popen(self.command)
            while not self.stop and self.child.poll() is None:
                time.sleep(1.0)
                runtime = time.monotonic() - started
                if runtime > max(20.0, self.stale_seconds * 2):
                    age = self._heartbeat_age()
                    if age > self.stale_seconds:
                        self._terminate_hung()
                        break

            if self.stop:
                self._terminate_hung()
                return 0

            code = self.child.poll() if self.child else 1
            runtime = time.monotonic() - started
            if runtime >= 300:
                backoff = 1.0
                self.crashes.clear()
            elif self._record_crash():
                self._trip_crash_loop()
                return int(code or 1)

            time.sleep(backoff)
            backoff = min(self.max_backoff_seconds, backoff * 2.0)
        return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--heartbeat",
        type=Path,
        default=Path(os.getenv("V12_HEARTBEAT_PATH", "run/v12-heartbeat.json")),
    )
    value.add_argument(
        "--kill-switch",
        type=Path,
        default=Path(os.getenv("V12_KILL_SWITCH_PATH", "run/V12_KILL")),
    )
    value.add_argument("--stale-seconds", type=float, default=20.0)
    value.add_argument("--crash-limit", type=int, default=5)
    value.add_argument("--crash-window-seconds", type=float, default=600.0)
    value.add_argument("--max-backoff-seconds", type=float, default=60.0)
    value.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        default=[],
    )
    return value


def main() -> int:
    args = parser().parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [
            sys.executable,
            "-m",
            "memecoin_bot.e4_exec",
            "run",
            "--live",
        ]
    supervisor = Supervisor(
        command,
        heartbeat=args.heartbeat,
        kill_switch=args.kill_switch,
        stale_seconds=args.stale_seconds,
        crash_limit=args.crash_limit,
        crash_window_seconds=args.crash_window_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
    )
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
