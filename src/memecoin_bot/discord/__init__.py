from __future__ import annotations

import asyncio
import os

from .cupsey_watch import schedule_cupsey_watch
from .notifier import DiscordNotifier as _DiscordNotifier
from .notifier import NullNotifier as _NullNotifier

_CUPSEY_TASK: asyncio.Task[None] | None = None


def _channel_override(fallback: int | None) -> int | None:
    raw = os.getenv("CUPSEY_ALERT_CHANNEL_ID", "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _arm_cupsey_watch(
    token: str | None,
    channel_id: int | None,
    webhook_url: str | None,
) -> None:
    global _CUPSEY_TASK
    if _CUPSEY_TASK is not None and not _CUPSEY_TASK.done():
        return
    task = schedule_cupsey_watch(
        discord_token=token or os.getenv("DISCORD_TOKEN"),
        channel_id=_channel_override(channel_id),
        webhook_url=webhook_url or os.getenv("DISCORD_WEBHOOK_URL"),
    )
    if task is not None:
        _CUPSEY_TASK = task


class DiscordNotifier(_DiscordNotifier):
    def __init__(
        self,
        token: str | None,
        channel_id: int | None,
        webhook_url: str | None,
        timeout: float = 10,
    ) -> None:
        super().__init__(token, channel_id, webhook_url, timeout)
        _arm_cupsey_watch(token, channel_id, webhook_url)


class NullNotifier(_NullNotifier):
    def __init__(self) -> None:
        super().__init__()
        raw_channel = os.getenv("CUPSEY_ALERT_CHANNEL_ID") or os.getenv("DISCORD_CHANNEL_ID")
        try:
            channel_id = int(raw_channel) if raw_channel else None
        except ValueError:
            channel_id = None
        _arm_cupsey_watch(
            os.getenv("DISCORD_TOKEN"),
            channel_id,
            os.getenv("DISCORD_WEBHOOK_URL"),
        )


__all__ = ["DiscordNotifier", "NullNotifier", "schedule_cupsey_watch"]
