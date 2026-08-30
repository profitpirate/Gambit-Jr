from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from memecoin_bot import e4_live
from memecoin_bot import e4_production  # applies production patches


class E4ProductionMappingTests(unittest.TestCase):
    def test_nested_v15_event_and_iso_time_are_normalized(self) -> None:
        event = e4_live.Event.from_row(
            {
                "id": 1,
                "event_type": "WALLET_BUY",
                "payload_json": '{"token_mint":"mint","wallet_address":"wallet","quote_amount_sol":1.25,"market_cap":4878}',
                "source_event_timestamp": "2026-08-30T12:00:00+00:00",
            }
        )
        self.assertEqual(event.kind, e4_live.EventKind.BUY)
        self.assertEqual(event.mint, "mint")
        self.assertEqual(event.trader, "wallet")
        self.assertEqual(event.sol_amount, 1.25)
        self.assertEqual(event.fdv_usd, 4878)
        self.assertGreater(event.source_ns, 0)

    def test_explicit_event_table_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE exact_events(sequence INTEGER PRIMARY KEY,event_type TEXT,mint TEXT)")
            conn.commit()
            conn.close()
            source = e4_live.SQLiteEventSource(path, 0.001)
            with unittest.mock.patch.dict(
                "os.environ",
                {"E4_EVENT_TABLE": "exact_events", "E4_EVENT_ID_COLUMN": "sequence"},
                clear=False,
            ):
                table, column = source._discover()
            self.assertEqual((table, column), ("exact_events", "sequence"))

    def test_production_position_save_uses_exact_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = e4_live.Store(Path(directory) / "e4.db")
            try:
                position = e4_live.Position(
                    position_id="p",
                    mint="mint",
                    status=e4_live.PositionStatus.OPEN,
                    opened_ns=time.time_ns(),
                    entry_sol=1.0,
                    tokens=1000,
                    remaining=1000,
                    entry_price=0.001,
                    max_price=0.001,
                    last_price=0.001,
                    entry_signature="sig",
                )
                store.save_position(position)
                self.assertIn("mint", store.load_open_positions())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
