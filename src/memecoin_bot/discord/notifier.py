from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any


class NullNotifier:
    async def send(self, content: str | dict[str, Any]) -> str | None:
        return "shadow-not-sent"


class DiscordNotifier:
    def __init__(
        self,
        token: str | None,
        channel_id: int | None,
        webhook_url: str | None,
        timeout: float = 10,
    ):
        self.token = token
        self.channel_id = channel_id
        self.webhook_url = webhook_url
        self.timeout = timeout
        if webhook_url:
            self.url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
            self.authorization = None
        elif token:
            self.url = (
                f"https://discord.com/api/v10/channels/{channel_id}/messages" if channel_id else ""
            )
            self.authorization = f"Bot {token}"
        else:
            raise ValueError("Discord requires webhook URL or both bot token and channel ID")

    async def send_to(self, channel_id: int, content: str | dict[str, Any]) -> str | None:
        if not self.token:
            return await self.send(content)
        return await self._send_url(
            f"https://discord.com/api/v10/channels/{channel_id}/messages", content
        )

    async def send(self, content: str | dict[str, Any]) -> str | None:
        if not self.url:
            raise RuntimeError("Discord destination is not configured; use send_to")
        return await self._send_url(self.url, content)

    async def _send_url(self, url: str, content: str | dict[str, Any]) -> str | None:
        message = (
            content
            if isinstance(content, dict)
            else {"content": content, "allowed_mentions": {"parse": []}}
        )
        payload = json.dumps(message).encode()
        for attempt in range(4):

            def perform(url: str = url) -> tuple[int, bytes, dict[str, str]]:
                headers = {
                    "Content-Type": "application/json",
                    "User-Agent": "DiscordBot (memecoin-intelligence, 1.0)",
                }
                if self.authorization:
                    headers["Authorization"] = self.authorization
                request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        return response.status, response.read(), dict(response.headers)
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read(), dict(exc.headers)

            status, body, headers = await asyncio.to_thread(perform)
            if status in (200, 201, 204):
                if not body:
                    return None
                return str(json.loads(body).get("id"))
            if status == 429 and attempt < 3:
                try:
                    delay = float(json.loads(body).get("retry_after", 1))
                except (ValueError, json.JSONDecodeError):
                    delay = float(headers.get("Retry-After", "1"))
                await asyncio.sleep(min(delay, 30))
                continue
            raise RuntimeError(f"Discord HTTP {status}: {body[:300].decode(errors='replace')}")
        raise RuntimeError("Discord retry limit exceeded")
