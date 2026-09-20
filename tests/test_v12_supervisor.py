from __future__ import annotations

import json
import time
from pathlib import Path

from scripts.v12_supervisor import Supervisor


def make_supervisor(tmp_path: Path, *, crash_limit: int = 3) -> Supervisor:
    return Supervisor(
        ["python", "-c", "pass"],
        heartbeat=tmp_path / "heartbeat.json",
        kill_switch=tmp_path / "KILL",
        stale_seconds=1.0,
        crash_limit=crash_limit,
        crash_window_seconds=60.0,
        max_backoff_seconds=1.0,
    )


def test_supervisor_detects_crash_loop_and_persists_kill_switch(tmp_path: Path) -> None:
    supervisor = make_supervisor(tmp_path)
    assert supervisor._record_crash() is False
    assert supervisor._record_crash() is False
    assert supervisor._record_crash() is True
    supervisor._trip_crash_loop()
    payload = json.loads((tmp_path / "KILL").read_text())
    assert payload["reason"] == "supervisor_crash_loop"
    assert payload["crashes"] == 3


def test_supervisor_reads_heartbeat_age_from_atomic_payload(tmp_path: Path) -> None:
    supervisor = make_supervisor(tmp_path)
    now = time.time_ns()
    supervisor.heartbeat.write_text(json.dumps({"ts_ns": now}))
    age = supervisor._heartbeat_age()
    assert 0 <= age < 1.0
