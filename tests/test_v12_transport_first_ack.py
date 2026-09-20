from __future__ import annotations

import asyncio
import time
from types import MethodType, SimpleNamespace

import pytest

from memecoin_bot import e4_sub10ms_transport_final_v12 as transport


class FakeRpc:
    def __init__(self):
        self.confirm_started = asyncio.Event()
        self.slow_finished = asyncio.Event()

    async def confirm(self, _signature: str, _timeout: float):
        self.confirm_started.set()
        return True, 123, None


def settings():
    return SimpleNamespace(
        route_urls={"fast": "https://fast.invalid", "slow": "https://slow.invalid"},
        direct_rpc_route=False,
        rpc_url="https://rpc.invalid",
        route_headers={},
        route_stagger_ms=25,
        confirmation_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_confirmation_starts_on_first_accepted_route() -> None:
    rpc = FakeRpc()
    sender = transport.FinalPersistentRouteSender(settings(), rpc)

    async def fake_send(self, index, name, url, tx, signature):
        del self, index, url, tx
        now = time.time_ns()
        if name == "slow":
            await asyncio.sleep(0.05)
            rpc.slow_finished.set()
        else:
            await asyncio.sleep(0.001)
        return transport.core.RouteResult(name, now, time.time_ns(), True, signature)

    sender._send = MethodType(fake_send, sender)
    task = asyncio.create_task(sender.submit("wire", "signature"))
    await asyncio.wait_for(rpc.confirm_started.wait(), 0.03)
    assert not rpc.slow_finished.is_set()
    route, confirmed, slot, error, results = await task
    assert confirmed is True
    assert slot == 123
    assert error is None
    assert route == "fast"
    assert len(results) == 2


def test_signed_payload_serialization_is_cached_by_route_kind() -> None:
    sender = transport.FinalPersistentRouteSender(settings(), SimpleNamespace())
    first = sender._payload_bytes("fast", "wire", "signature")
    second = sender._payload_bytes("slow", "wire", "signature")
    assert first is second
    assert b'"sendTransaction"' in first
