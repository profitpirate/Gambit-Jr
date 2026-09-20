from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(name: str) -> str:
    return (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")


def test_trader_unit_requires_marketdata_and_aligns_supervisor_paths() -> None:
    unit = text("gambit-v12.service")
    assert "Requires=gambit-v12-marketdata.service" in unit
    assert "After=network-online.target gambit-v12-marketdata.service" in unit
    assert "Environment=V12_HEARTBEAT_PATH=/run/gambit/v12-heartbeat.json" in unit
    assert "Environment=V12_KILL_SWITCH_PATH=/run/gambit/V12_KILL" in unit
    assert "Environment=V12_INSTANCE_LOCK=/run/gambit/v12-live.lock" in unit
    assert "--heartbeat /run/gambit/v12-heartbeat.json" in unit


def test_marketdata_unit_child_and_supervisor_share_heartbeat() -> None:
    unit = text("gambit-v12-marketdata.service")
    assert (
        "Environment=V12_MARKETDATA_HEARTBEAT="
        "/run/gambit/v12-marketdata-heartbeat.json"
    ) in unit
    assert "--heartbeat /run/gambit/v12-marketdata-heartbeat.json" in unit
    assert "Before=gambit-v12.service gambit-v12-orchestrator.service" in unit


def test_orchestrator_requires_canonical_marketdata() -> None:
    unit = text("gambit-v12-orchestrator.service")
    assert "Requires=gambit-v12-marketdata.service" in unit
    assert "V12_MARKETDATA_DB=/var/lib/gambit/marketdata.db" in unit
    assert "V12_ACCOUNTS_ROOT=/var/lib/gambit/accounts" in unit
    assert "-m memecoin_bot.v12_account_orchestrator" in unit


def test_all_production_units_are_sandboxed() -> None:
    for name in (
        "gambit-v12.service",
        "gambit-v12-marketdata.service",
        "gambit-v12-orchestrator.service",
        "gambit-v12-portal.service",
    ):
        unit = text(name)
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=true" in unit
        assert "UMask=0077" in unit
        assert "RestrictSUIDSGID=true" in unit
