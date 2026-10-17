import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_health import assessment


@pytest.mark.parametrize(
    ("updated_ns", "expected_stale"),
    [(100_040_000_000, False), (80_000_000_000, True), (105_000_000_000, True)],
)
def test_status_age_uses_time_after_the_snapshot_is_read(
    monkeypatch, tmp_path, updated_ns, expected_stale
):
    path = Path(__file__).resolve().parents[1] / "deployment/gambit-monitor-collector.py"
    spec = importlib.util.spec_from_file_location("timing_collector", path)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    samples = iter([100_000_000_000, 100_080_000_000])
    monkeypatch.setattr(collector.time, "time_ns", lambda: next(samples))
    monkeypatch.setattr(collector, "service_status", lambda: {"ActiveState": "active"})
    # The trading worker writes a fresh heartbeat while service collection runs.
    monkeypatch.setattr(collector, "status_snapshot", lambda path: {"updated_ns": updated_ns})
    monkeypatch.setattr(collector, "database_snapshot", lambda *args: {})
    monkeypatch.setattr(collector, "audit_tail", lambda *args: {})
    monkeypatch.setattr(collector, "health_history", list)
    report = collector.collect(tmp_path)
    stale = any("status is stale" in item for item in assessment(report, []))
    assert stale is expected_stale
    if not expected_stale:
        assert report["status_age_seconds"] == 0.04
        assert report["observed_ns"] == 100_080_000_000


def test_health_history_only_exports_sanitized_health_records(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "deployment/gambit-monitor-collector.py"
    spec = importlib.util.spec_from_file_location("history_collector", path)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    secret = "https://provider.invalid/?token=SECRET"
    row = {
        "event": "OPERATOR_HEALTH_CHECK",
        "status_age_seconds": 0.4,
        "status_updated_ns": 100,
        "collector_observed_ns": 400000100,
        "problems": ["Trading status is stale or missing", secret],
        "providers": [{"provider_index": 0, "health": "AVAILABLE", "url": secret}],
        "traceback": secret,
    }

    def run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/journalctl", "-u", "gambit-operator"]
        assert kwargs["timeout"] == 8
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                [
                    secret,
                    "not json",
                    json.dumps({"event": "UNRELATED", "secret": secret}),
                    json.dumps(row),
                ]
            ),
        )

    monkeypatch.setattr(collector.subprocess, "run", run)
    result = collector.health_history()
    assert len(result) == 1
    assert result[0]["status_age_seconds"] == 0.4
    assert result[0]["problems"] == [
        "Trading status is stale or missing",
        "Unrecognized health problem",
    ]
    assert "SECRET" not in json.dumps(result)


def test_monitor_records_the_age_used_for_its_decision(monkeypatch, tmp_path, capsys):
    from memecoin_bot import v12_prearmed_health as health

    report = {
        "status_age_seconds": 12.5,
        "observed_ns": 12500000100,
        "status": {"updated_ns": 100},
        "service": {"ActiveState": "active"},
        "execution_worker_running": True,
    }
    backend = SimpleNamespace(stage=tmp_path, status=lambda: report, trading_expected=lambda: True)
    monkeypatch.setenv("E4_PRIMARY_RPC_URL", "")
    monkeypatch.setenv("E4_FALLBACK_RPC_URLS", "")
    monkeypatch.setattr(health.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))

    async def send(*args):
        return None

    async def stop_after_first(*args):
        raise RuntimeError("END_TEST_CYCLE")

    monkeypatch.setattr(health.Notices, "send", send)
    monkeypatch.setattr(health.asyncio, "sleep", stop_after_first)
    with pytest.raises(RuntimeError, match="END_TEST_CYCLE"):
        asyncio.run(health.monitor(backend))
    saved = json.loads((tmp_path / "operator-data/health.json").read_text())
    journal = json.loads(capsys.readouterr().out)
    for document in (saved, journal):
        assert document["status_age_seconds"] == 12.5
        assert document["status_updated_ns"] == 100
        assert document["collector_observed_ns"] == 12500000100
        assert document["problems"] == ["Trading status is stale or missing"]


def test_disk_space_guard_remains_independent_of_heartbeat_age():
    report = {"service": {"ActiveState": "active"}, "status_age_seconds": 0.5}
    assert assessment(report, [], free_bytes=500_000_000) == ["Server disk has less than 1 GB free"]


def test_release_update_preserves_only_an_intact_monitor_override(tmp_path):
    import hashlib

    path = Path(__file__).resolve().parents[1] / "deployment/install-monitor-collector.py"
    spec = importlib.util.spec_from_file_location("monitor_install", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    bundled = tmp_path / "bundled.py"
    assert installer.select_collector(tmp_path, bundled) == bundled
    payload = b"# tested monitor patch\n"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "monitor-releases" / digest / "collector.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    record = tmp_path / "operator-data/monitor-install.json"
    record.parent.mkdir()
    record.write_text(json.dumps({"sha256": digest}))
    assert installer.select_collector(tmp_path, bundled) == source
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="CHECKSUM_FAILED"):
        installer.select_collector(tmp_path, bundled)
    record.write_text(json.dumps({"sha256": "../../outside"}))
    with pytest.raises(ValueError, match="OVERRIDE_INVALID"):
        installer.select_collector(tmp_path, bundled)
