from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
            with mock.patch.dict(
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


class _BalanceRpc:
    def __init__(self, balances: dict[str, float]) -> None:
        self.balances = balances

    async def token_balance(self, wallet: str, mint: str) -> float:
        if wallet != "wallet":
            raise AssertionError(wallet)
        return float(self.balances.get(mint, 0.0))


class E4ProductionRestartReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_closes_stale_position_when_onchain_balance_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = e4_live.Store(Path(directory) / "e4.db")
            try:
                position = e4_live.Position(
                    position_id="stale",
                    mint="mint-stale",
                    status=e4_live.PositionStatus.OPEN,
                    opened_ns=time.time_ns(),
                    entry_sol=0.2,
                    tokens=1_000.0,
                    remaining=1_000.0,
                    entry_price=0.0002,
                    max_price=0.0002,
                    last_price=0.0002,
                    entry_signature="entry-stale",
                )
                store.save_position(position)
                engine = SimpleNamespace(
                    positions={"mint-stale": position},
                    store=store,
                    signer=SimpleNamespace(wallet="wallet"),
                    rpc=_BalanceRpc({"mint-stale": 0.0}),
                )
                await e4_production._reconcile(engine)
                self.assertNotIn("mint-stale", engine.positions)
                self.assertNotIn("mint-stale", store.load_open_positions())
                row = store.conn.execute(
                    "SELECT status,remaining FROM e4_positions WHERE mint=?",
                    ("mint-stale",),
                ).fetchone()
                self.assertEqual(row["status"], e4_live.PositionStatus.CLOSED)
                self.assertEqual(float(row["remaining"]), 0.0)
            finally:
                store.close()

    async def test_restart_recovers_confirmed_buy_missing_position_row_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = e4_live.Store(Path(directory) / "e4.db")
            try:
                store.order("buy-request", "mint-orphan", "BUY", 0.25, None, "accepted")
                store.receipt(
                    "buy-request",
                    "entry-signature",
                    "direct",
                    True,
                    123,
                    None,
                    {"direct": "entry-signature"},
                )
                engine = SimpleNamespace(
                    positions={},
                    store=store,
                    signer=SimpleNamespace(wallet="wallet"),
                    rpc=_BalanceRpc({"mint-orphan": 500.0}),
                )
                await e4_production._reconcile(engine)
                recovered = engine.positions["mint-orphan"]
                self.assertEqual(recovered.entry_sol, 0.25)
                self.assertEqual(recovered.tokens, 500.0)
                self.assertEqual(recovered.remaining, 500.0)
                self.assertEqual(recovered.status, e4_live.PositionStatus.OPEN)

                first_id = recovered.position_id
                await e4_production._reconcile(engine)
                self.assertEqual(engine.positions["mint-orphan"].position_id, first_id)
                count = store.conn.execute(
                    "SELECT COUNT(*) FROM e4_positions WHERE mint=?",
                    ("mint-orphan",),
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
