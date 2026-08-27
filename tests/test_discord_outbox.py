from __future__ import annotations

import asyncio
import logging
import unittest

from memecoin_bot.service import IntelligenceService
from tests.helpers import create_signal, settings, store, temp_db_path


class FailingNotifier:
    async def send(self, content: str) -> str:
        raise RuntimeError("discord unavailable")


class DiscordFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_failure_does_not_lose_signal_state(self) -> None:
        with temp_db_path() as path:
            db = store(path)
            signal_id = create_signal(db)
            service = object.__new__(IntelligenceService)
            service.store = db
            service.notifier = FailingNotifier()
            import logging
            service.log = logging.getLogger("test")
            sent = await service.flush_outbox()
            self.assertEqual(sent, 0)
            self.assertIsNotNone(db.signal(signal_id))
            self.assertEqual(len(db.pending_outbox()), 1)
            self.assertEqual(db.pending_outbox()[0]["attempts"], 1)
            db.close()

    async def test_concurrent_flushers_lease_each_event_once(self) -> None:
        class SlowNotifier:
            def __init__(self):
                self.calls = 0

            async def send(self, _content):
                self.calls += 1
                await asyncio.sleep(0.02)
                return "remote"

        with temp_db_path() as path:
            db = store(path)
            create_signal(db)
            notifier = SlowNotifier()
            service = object.__new__(IntelligenceService)
            service.store = db
            service.notifier = notifier
            service.settings = settings(path)
            service.log = logging.getLogger("test-outbox-concurrency")
            results = await asyncio.gather(service.flush_outbox(), service.flush_outbox())
            self.assertEqual(sum(results), 1)
            self.assertEqual(notifier.calls, 1)
            self.assertEqual(len(db.pending_outbox()), 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
