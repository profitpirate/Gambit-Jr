from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from memecoin_bot.realtime.events import CanonicalEvent


class TokenLaneExecutor:
    """Bounded token-sharded workers: same token ordered, different tokens concurrent."""

    def __init__(self, lane_count: int, *, queue_size: int = 1_024):
        if lane_count <= 0 or queue_size <= 0:
            raise ValueError("lane count and queue size must be positive")
        self.lane_count = lane_count
        self.queues = [asyncio.Queue[CanonicalEvent](maxsize=queue_size) for _ in range(lane_count)]
        self.processed = 0
        self.failures = 0

    def lane_for(self, token: str) -> int:
        digest = hashlib.blake2b(str(token).encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.lane_count

    async def run(
        self,
        *,
        claim: Callable[[int], list[CanonicalEvent]],
        handle: Callable[[CanonicalEvent], Awaitable[Any]],
        fail: Callable[[str, str], None],
        wake: asyncio.Event,
        stop: asyncio.Event,
        batch_size: int,
        on_error: Callable[[CanonicalEvent, Exception], None] | None = None,
    ) -> None:
        async def lane_worker(queue: asyncio.Queue[CanonicalEvent]) -> None:
            while not stop.is_set() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                try:
                    await handle(event)
                    self.processed += 1
                except Exception as exc:  # noqa: BLE001 - event lease must be released
                    self.failures += 1
                    fail(event.event_id, str(exc))
                    if on_error:
                        on_error(event, exc)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(lane_worker(queue)) for queue in self.queues]
        try:
            while not stop.is_set():
                rows = claim(batch_size)
                if not rows:
                    wake.clear()
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                    continue
                for event in rows:
                    await self.queues[self.lane_for(event.canonical_token)].put(event)
        finally:
            await asyncio.gather(*(queue.join() for queue in self.queues))
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
