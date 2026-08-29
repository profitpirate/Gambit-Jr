from __future__ import annotations

import pytest

from scripts.v15_load_soak import run


@pytest.mark.asyncio
async def test_bounded_load_burst_restart_and_duplicate_soak(tmp_path):
    result = await run(tmp_path / "soak.db", events=300, queue_size=32, burst_multiplier=3)
    assert result["state"] == "PASS"
    assert result["queue"]["dropped"] == 64
    assert result["duplicate_replays_suppressed"] == 300
    assert result["duplicate_event_keys"] == 0
    assert result["state_reconciliation"]["difference"] == 0
    assert result["cpu_seconds"] > 0
    assert result["single_core_cpu_percent"] > 0
