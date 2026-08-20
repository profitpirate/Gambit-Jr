from __future__ import annotations

import unittest

from memecoin_bot.service import IntelligenceService
from tests.helpers import create_signal, store, temp_db_path


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


if __name__ == "__main__":
    unittest.main()
