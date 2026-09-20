from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from memecoin_bot.notifications import (
    NotificationEvent,
    NotificationOutbox,
    NotificationRouter,
)
from memecoin_bot.v12_route_health import RouteHealthStore
from memecoin_bot.v12_safety import CircuitBreaker, SafetyMode, SafetyStore
from memecoin_bot.v12_vault_signer import VaultRef, VaultTransitSigner
from memecoin_bot.v12_watchdogs import WatchdogConfig, WatchdogManager


class _Store:
    def __init__(self, conn):
        self.conn = conn


class _Rpc:
    async def call(self, method, params):
        del params
        if method == "getSlot":
            return 1
        if method == "getBlockTime":
            return int(time.time())
        raise RuntimeError(method)

    async def balance(self, wallet):
        del wallet
        return 10.0


def _watchdog(tmp_path: Path, *, positions=None):
    safety = SafetyStore(tmp_path / "safety.db")
    breaker = CircuitBreaker(safety)
    route_health = RouteHealthStore(safety.conn)
    exits: list[str] = []

    async def emergency_exit(reason: str) -> None:
        exits.append(reason)

    engine = SimpleNamespace(
        positions=positions or {},
        pending_entries=set(),
        pending_exits=set(),
        tokens={},
        sender=SimpleNamespace(routes=[("primary", "https://route.invalid")]),
        rpc=_Rpc(),
        signer=SimpleNamespace(wallet="wallet"),
        settings=SimpleNamespace(execution_db=tmp_path / "execution.db"),
        store=_Store(safety.conn),
    )
    manager = WatchdogManager(
        engine,
        breaker,
        route_health,
        emergency_exit=emergency_exit,
        config=WatchdogConfig(
            database_check_seconds=0,
            rpc_check_seconds=999,
            balance_check_seconds=999,
            event_warn_seconds=999,
            event_halt_seconds=999,
            heartbeat_path=tmp_path / "heartbeat.json",
            kill_switch_path=tmp_path / "KILL",
        ),
    )
    return safety, manager, exits


@pytest.mark.asyncio
async def test_disk_exhaustion_forces_exit_only_without_corrupting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safety, manager, exits = _watchdog(tmp_path)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=99, free=1),
    )
    await manager._check_database_disk(time.time_ns())
    snapshot = safety.snapshot()
    assert snapshot.mode == SafetyMode.EXIT_ONLY
    assert snapshot.reason == "disk_space_low"
    assert exits == []


@pytest.mark.asyncio
async def test_database_integrity_failure_halts_and_emergency_exits(
    tmp_path: Path,
) -> None:
    position = SimpleNamespace()
    safety, manager, exits = _watchdog(
        tmp_path,
        positions={"mint": position},
    )

    class BrokenConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.DatabaseError("simulated corruption")

    manager.engine.store = _Store(BrokenConnection())
    await manager._check_database_disk(time.time_ns())

    snapshot = safety.snapshot()
    assert snapshot.mode == SafetyMode.HALTED
    assert snapshot.reason == "database_integrity_failure"
    assert exits == ["watchdog_database_failure"]


@pytest.mark.asyncio
async def test_vault_signer_outage_fails_closed() -> None:
    signer = VaultTransitSigner(
        VaultRef("transit", "user-1"),
        address="https://vault.invalid",
        token="test-token",
    )

    async def unavailable(self, method, path, payload=None):
        del self, method, path, payload
        raise RuntimeError("simulated Vault outage")

    signer._request = MethodType(unavailable, signer)
    with pytest.raises(RuntimeError, match="simulated Vault outage"):
        await signer.public_key()


class _FailingSink:
    name = "provider"

    async def deliver(self, event):
        del event
        raise RuntimeError("provider unavailable")


class _RecoveringSink:
    name = "provider"

    def __init__(self):
        self.delivered = 0

    async def deliver(self, event):
        del event
        self.delivered += 1


@pytest.mark.asyncio
async def test_notification_outage_is_durable_and_recovers_without_duplicate(
    tmp_path: Path,
) -> None:
    outbox = NotificationOutbox(tmp_path / "notifications.db")
    router = NotificationRouter(outbox, [_FailingSink()])
    event = NotificationEvent.trade_closed(
        position_id="p1",
        mint="mint",
        pnl_sol=-0.1,
        entry_sol=0.5,
        realized_sol=0.4,
        signature="sig",
    )
    try:
        router.publish(event)
        assert await router.flush_once() == 0
        assert outbox.status() == {"RETRY": 1}

        # Force the backoff window due, simulating provider recovery later.
        outbox.conn.execute(
            "UPDATE notification_delivery SET next_attempt_ns=0 WHERE event_id=?",
            (event.event_id,),
        )
        recovered = _RecoveringSink()
        router.sinks["provider"] = recovered

        assert await router.flush_once() == 1
        assert recovered.delivered == 1
        assert outbox.status() == {"DELIVERED": 1}

        # Exactly-once delivery state prevents a second notification.
        assert await router.flush_once() == 0
        assert recovered.delivered == 1
    finally:
        outbox.close()
