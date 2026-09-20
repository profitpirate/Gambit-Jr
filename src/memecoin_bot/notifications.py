"""Reliable outbound notification infrastructure for automated V12.

Events are persisted before delivery. Trading never waits on Discord or phone
push availability, and idempotency prevents duplicate close/sweep alerts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from memecoin_bot.discord.notifier import DiscordNotifier


class NotificationKind(StrEnum):
    TRADE_CLOSED_WIN = "TRADE_CLOSED_WIN"
    TRADE_CLOSED_LOSS = "TRADE_CLOSED_LOSS"
    STORAGE_SWEEP = "STORAGE_SWEEP"


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_id: str
    kind: NotificationKind
    created_ns: int
    payload: dict[str, Any]

    @classmethod
    def trade_closed(
        cls,
        *,
        position_id: str,
        mint: str,
        pnl_sol: float,
        entry_sol: float,
        realized_sol: float,
        signature: str,
    ) -> NotificationEvent:
        kind = (
            NotificationKind.TRADE_CLOSED_WIN
            if pnl_sol > 0
            else NotificationKind.TRADE_CLOSED_LOSS
        )
        event_id = hashlib.sha256(
            f"trade-close|{position_id}|{signature}".encode()
        ).hexdigest()
        return cls(
            event_id=event_id,
            kind=kind,
            created_ns=time.time_ns(),
            payload={
                "position_id": position_id,
                "mint": mint,
                "pnl_sol": float(pnl_sol),
                "entry_sol": float(entry_sol),
                "realized_sol": float(realized_sol),
                "signature": signature,
                "outcome": "WIN" if pnl_sol > 0 else "LOSS",
            },
        )

    @classmethod
    def storage_sweep(
        cls,
        *,
        request_id: str,
        amount_sol: float,
        destination: str,
        signature: str,
    ) -> NotificationEvent:
        event_id = hashlib.sha256(
            f"storage-sweep|{request_id}|{signature}".encode()
        ).hexdigest()
        return cls(
            event_id=event_id,
            kind=NotificationKind.STORAGE_SWEEP,
            created_ns=time.time_ns(),
            payload={
                "request_id": request_id,
                "amount_sol": float(amount_sol),
                "destination": destination,
                "signature": signature,
            },
        )


class NotificationSink(Protocol):
    name: str

    async def deliver(self, event: NotificationEvent) -> None: ...


class NotificationOutbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notification_events(
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                created_ns INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_delivery(
                event_id TEXT NOT NULL,
                sink TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ns INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                delivered_ns INTEGER,
                PRIMARY KEY(event_id, sink),
                FOREIGN KEY(event_id) REFERENCES notification_events(event_id)
            );
            """
        )
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    def enqueue(self, event: NotificationEvent, sinks: list[str]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notification_events(event_id,kind,created_ns,payload_json) VALUES(?,?,?,?)",
            (
                event.event_id,
                event.kind.value,
                event.created_ns,
                json.dumps(event.payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        for sink in sinks:
            self.conn.execute(
                "INSERT OR IGNORE INTO notification_delivery(event_id,sink,state,next_attempt_ns) VALUES(?,?,?,0)",
                (event.event_id, sink, "PENDING"),
            )

    def due(self, limit: int = 100) -> list[tuple[NotificationEvent, str, int]]:
        rows = self.conn.execute(
            """
            SELECT e.*,d.sink,d.attempts
            FROM notification_delivery d
            JOIN notification_events e USING(event_id)
            WHERE d.state!='DELIVERED' AND d.next_attempt_ns<=?
            ORDER BY e.created_ns
            LIMIT ?
            """,
            (time.time_ns(), int(limit)),
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                (
                    NotificationEvent(
                        event_id=row["event_id"],
                        kind=NotificationKind(row["kind"]),
                        created_ns=int(row["created_ns"]),
                        payload=json.loads(row["payload_json"]),
                    ),
                    str(row["sink"]),
                    int(row["attempts"]),
                )
            )
        return result

    def delivered(self, event_id: str, sink: str) -> None:
        self.conn.execute(
            """
            UPDATE notification_delivery
            SET state='DELIVERED',attempts=attempts+1,delivered_ns=?,last_error=NULL
            WHERE event_id=? AND sink=?
            """,
            (time.time_ns(), event_id, sink),
        )

    def failed(self, event_id: str, sink: str, attempts: int, error: str) -> None:
        delay_seconds = min(300, 2 ** min(8, attempts + 1))
        self.conn.execute(
            """
            UPDATE notification_delivery
            SET state='RETRY',attempts=attempts+1,next_attempt_ns=?,last_error=?
            WHERE event_id=? AND sink=?
            """,
            (
                time.time_ns() + int(delay_seconds * 1e9),
                str(error)[:1000],
                event_id,
                sink,
            ),
        )

    def status(self) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in self.conn.execute(
                "SELECT state,COUNT(*) FROM notification_delivery GROUP BY state"
            )
        }


def format_event(event: NotificationEvent) -> str:
    p = event.payload
    if event.kind in {
        NotificationKind.TRADE_CLOSED_WIN,
        NotificationKind.TRADE_CLOSED_LOSS,
    }:
        icon = "✅" if event.kind == NotificationKind.TRADE_CLOSED_WIN else "❌"
        return (
            f"{icon} V12 trade closed — {p['outcome']}\n"
            f"Mint: {p['mint']}\n"
            f"P&L: {float(p['pnl_sol']):+.6f} SOL\n"
            f"Entry: {float(p['entry_sol']):.6f} SOL\n"
            f"Signature: {p['signature']}"
        )
    return (
        "🏦 V12 profit sweep confirmed\n"
        f"Amount: {float(p['amount_sol']):.6f} SOL\n"
        f"Storage wallet: {p['destination']}\n"
        f"Signature: {p['signature']}"
    )


class DiscordDmSink:
    name = "discord_dm"

    def __init__(self, notifier: DiscordNotifier, discord_user_id: int):
        self.notifier = notifier
        self.discord_user_id = int(discord_user_id)

    async def deliver(self, event: NotificationEvent) -> None:
        await self.notifier.send_dm(self.discord_user_id, format_event(event))


class DiscordChannelSink:
    name = "discord_channel"

    def __init__(self, notifier: DiscordNotifier, channel_id: int):
        self.notifier = notifier
        self.channel_id = int(channel_id)

    async def deliver(self, event: NotificationEvent) -> None:
        await self.notifier.send_to(self.channel_id, format_event(event))


class PushoverPhoneSink:
    """Native phone push through Pushover; credentials are supplied only at runtime."""

    name = "phone_push"

    def __init__(
        self,
        *,
        app_token: str,
        user_key: str,
        endpoint: str = "https://api.pushover.net/1/messages.json",
        timeout: float = 5.0,
    ):
        self.app_token = app_token
        self.user_key = user_key
        self.endpoint = endpoint
        self.timeout = timeout

    async def deliver(self, event: NotificationEvent) -> None:
        values = urllib.parse.urlencode(
            {
                "token": self.app_token,
                "user": self.user_key,
                "title": "Gambit V12",
                "message": format_event(event),
                "priority": 0,
            }
        ).encode()

        def send() -> None:
            request = urllib.request.Request(
                self.endpoint,
                data=values,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.status not in (200, 201):
                        raise RuntimeError(f"push HTTP {response.status}: {body[:300]!r}")
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    f"push HTTP {exc.code}: {exc.read()[:300]!r}"
                ) from exc

        await asyncio.to_thread(send)


class NotificationRouter:
    def __init__(
        self,
        outbox: NotificationOutbox,
        sinks: list[NotificationSink],
    ):
        self.outbox = outbox
        self.sinks = {sink.name: sink for sink in sinks}

    def publish(self, event: NotificationEvent) -> None:
        self.outbox.enqueue(event, list(self.sinks))

    async def flush_once(self, limit: int = 100) -> int:
        delivered = 0
        for event, sink_name, attempts in self.outbox.due(limit):
            sink = self.sinks.get(sink_name)
            if sink is None:
                self.outbox.failed(
                    event.event_id,
                    sink_name,
                    attempts,
                    "notification sink is not configured",
                )
                continue
            try:
                await sink.deliver(event)
            except (RuntimeError, OSError, ValueError, urllib.error.URLError) as exc:
                self.outbox.failed(event.event_id, sink_name, attempts, str(exc))
            else:
                self.outbox.delivered(event.event_id, sink_name)
                delivered += 1
        return delivered


def router_from_env() -> NotificationRouter | None:
    if str(os.getenv("V12_NOTIFICATIONS_ENABLED", "")).lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    sinks: list[NotificationSink] = []
    token = os.getenv("DISCORD_TOKEN")
    discord_user = os.getenv("V12_DISCORD_USER_ID")
    discord_channel = os.getenv("V12_DISCORD_NOTIFICATION_CHANNEL_ID")
    if token and (discord_user or discord_channel):
        notifier = DiscordNotifier(token, None, None)
        if discord_user:
            sinks.append(DiscordDmSink(notifier, int(discord_user)))
        if discord_channel:
            sinks.append(DiscordChannelSink(notifier, int(discord_channel)))
    if os.getenv("PUSHOVER_APP_TOKEN") and os.getenv("PUSHOVER_USER_KEY"):
        sinks.append(
            PushoverPhoneSink(
                app_token=os.environ["PUSHOVER_APP_TOKEN"],
                user_key=os.environ["PUSHOVER_USER_KEY"],
            )
        )
    if not sinks:
        return None
    return NotificationRouter(
        NotificationOutbox(
            Path(os.getenv("V12_NOTIFICATION_DB", "data/v12-notifications.db"))
        ),
        sinks,
    )
