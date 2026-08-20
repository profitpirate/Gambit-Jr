from __future__ import annotations

import sqlite3
import unittest

from tests.helpers import create_signal, store, temp_db_path


class PersistenceTests(unittest.TestCase):
    def test_initial_signal_snapshot_is_immutable(self) -> None:
        with temp_db_path() as path:
            db = store(path)
            signal_id = create_signal(db)
            with self.assertRaises(sqlite3.IntegrityError):
                with db.conn:
                    db.conn.execute(
                        "UPDATE signals SET signal_market_cap_usd=999 WHERE id=?", (signal_id,)
                    )
            self.assertEqual(db.signal(signal_id)["signal_market_cap_usd"], 30_000)
            db.close()

    def test_duplicate_discovery_does_not_duplicate_token(self) -> None:
        from memecoin_bot.models import DiscoveryEvent
        with temp_db_path() as path:
            db = store(path)
            first = db.upsert_discovery(DiscoveryEvent(token_address="same"))
            second = db.upsert_discovery(DiscoveryEvent(token_address="same", source="second"))
            self.assertTrue(first[1])
            self.assertFalse(second[1])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
