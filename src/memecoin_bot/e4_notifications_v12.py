"""Attach V12 notifications to authoritative execution-store state transitions."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from . import e4_final as final
from .notifications import NotificationEvent, NotificationRouter, router_from_env

core = final.core
LOGGER = logging.getLogger("gambit.v12.notifications")
_ROUTER: NotificationRouter | None = router_from_env()


def set_notification_router(router: NotificationRouter | None) -> None:
    global _ROUTER
    _ROUTER = router


def _publish(event: NotificationEvent) -> None:
    if _ROUTER is None:
        return
    _ROUTER.publish(event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_ROUTER.flush_once(), name=f"v12-notify-{event.kind.value.lower()}")


_PREVIOUS_SAVE_POSITION = core.Store.save_position


def _save_position_with_notifications(self: Any, position: Any) -> None:
    _PREVIOUS_SAVE_POSITION(self, position)
    if _ROUTER is None:
        return
    status = str(getattr(getattr(position, "status", None), "value", getattr(position, "status", "")))
    if status != "CLOSED":
        return
    realized = float(getattr(position, "realized_sol", 0.0) or 0.0)
    entry = float(getattr(position, "entry_sol", 0.0) or 0.0)
    signature = str(getattr(position, "close_signature", "") or "")
    if not signature:
        signature = "closed-without-signature"
    _publish(
        NotificationEvent.trade_closed(
            position_id=str(getattr(position, "position_id", "")),
            mint=str(getattr(position, "mint", "")),
            pnl_sol=realized - entry,
            entry_sol=entry,
            realized_sol=realized,
            signature=signature,
        )
    )


_PREVIOUS_RECEIPT = core.Store.receipt


def _receipt_with_notifications(
    self: Any,
    request_id: str,
    signature: str,
    route: str,
    confirmed: bool,
    slot: int | None,
    error: str | None,
    results: Mapping[str, str],
) -> None:
    _PREVIOUS_RECEIPT(
        self,
        request_id,
        signature,
        route,
        confirmed,
        slot,
        error,
        results,
    )
    if _ROUTER is None or not confirmed:
        return
    row = self.conn.execute(
        "SELECT side,amount FROM e4_orders WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if not row or str(row["side"]).upper() != "SWEEP":
        return
    destination = ""
    # The execution store deliberately does not persist private credentials.
    # Vault destination is public wallet metadata and is read from engine env/settings
    # by the live runtime; an unset value is still a valid notification record.
    try:
        import os

        destination = os.getenv("E4_VAULT_WALLET", "") or os.getenv("E4_VAULT", "")
    except Exception:
        destination = ""
    _publish(
        NotificationEvent.storage_sweep(
            request_id=str(request_id),
            amount_sol=float(row["amount"]),
            destination=destination,
            signature=str(signature),
        )
    )


core.Store.save_position = _save_position_with_notifications
core.Store.receipt = _receipt_with_notifications
