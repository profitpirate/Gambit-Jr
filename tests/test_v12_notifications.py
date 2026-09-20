from __future__ import annotations

from pathlib import Path

import pytest

from memecoin_bot.notifications import (
    NotificationEvent,
    NotificationKind,
    NotificationOutbox,
    NotificationRouter,
)


class CaptureSink:
    name = "capture"

    def __init__(self):
        self.events = []

    async def deliver(self, event):
        self.events.append(event)


def test_notification_outbox_is_idempotent(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "notify.db")
    try:
        event = NotificationEvent.trade_closed(
            position_id="p1",
            mint="mint",
            pnl_sol=0.1,
            entry_sol=0.3,
            realized_sol=0.4,
            signature="sig",
        )
        outbox.enqueue(event, ["capture"])
        outbox.enqueue(event, ["capture"])
        assert len(outbox.due()) == 1
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_router_delivers_close_and_sweep_events_once(tmp_path: Path) -> None:
    outbox = NotificationOutbox(tmp_path / "notify.db")
    sink = CaptureSink()
    router = NotificationRouter(outbox, [sink])
    try:
        win = NotificationEvent.trade_closed(
            position_id="p1",
            mint="mint",
            pnl_sol=0.1,
            entry_sol=0.3,
            realized_sol=0.4,
            signature="sig",
        )
        sweep = NotificationEvent.storage_sweep(
            request_id="r1",
            amount_sol=1.2,
            destination="storage",
            signature="sweep-sig",
        )
        router.publish(win)
        router.publish(sweep)
        assert await router.flush_once() == 2
        assert await router.flush_once() == 0
        assert [event.kind for event in sink.events] == [
            NotificationKind.TRADE_CLOSED_WIN,
            NotificationKind.STORAGE_SWEEP,
        ]
        assert outbox.status() == {"DELIVERED": 2}
    finally:
        outbox.close()
