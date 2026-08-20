from __future__ import annotations

import unittest

from memecoin_bot.models import MarketSnapshot, iso
from memecoin_bot.tracking import SignalTracker
from tests.helpers import create_signal, settings, store, temp_db_path


class StaticMarket:
    def __init__(self, market_cap: float):
        self.market_cap = market_cap

    async def market_snapshot(self, token: str, chain: str = "solana") -> MarketSnapshot:
        return MarketSnapshot(
            token_address=token, captured_at=iso(), source="test", symbol="TST",
            market_cap_usd=self.market_cap, price_usd=0.1,
        )


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_milestone_fires_once_across_restart(self) -> None:
        with temp_db_path() as path:
            db = store(path)
            signal_id = create_signal(db)
            tracker = SignalTracker(db, StaticMarket(66_000), settings(path))
            first = await tracker.monitor_once()
            second = await tracker.monitor_once()
            self.assertEqual(first["milestones"], 2)  # 1.5X and 2X crossed together
            self.assertEqual(second["milestones"], 0)
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM milestones WHERE signal_id=? AND multiple=2", (signal_id,)
            ).fetchone()[0], 1)
            db.close()

            restarted = store(path)
            tracker = SignalTracker(restarted, StaticMarket(66_000), settings(path))
            third = await tracker.monitor_once()
            self.assertEqual(third["milestones"], 0)
            self.assertEqual(restarted.signal(signal_id)["signal_market_cap_usd"], 30_000)
            restarted.close()

    async def test_failed_signal_remains_in_performance(self) -> None:
        with temp_db_path() as path:
            db = store(path)
            create_signal(db)
            tracker = SignalTracker(db, StaticMarket(8_000), settings(path))
            result = await tracker.monitor_once()
            self.assertEqual(result["failed"], 1)
            report = db.performance("v1")
            self.assertEqual(report["total_signals"], 1)
            self.assertEqual(report["failed"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
