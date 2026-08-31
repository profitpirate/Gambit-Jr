from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot import e4_live
from memecoin_bot import e4_hardening_v6  # noqa: F401 - applies production V6 hardening


class E4InvariantTests(unittest.TestCase):
    def test_reentry_and_concurrency_invariants_are_not_configurable(self) -> None:
        with patch.dict(os.environ, {"E4_MAX_ENTRIES_PER_MINT": "2"}, clear=False):
            with self.assertRaisesRegex(ValueError, "MAX_ENTRIES"):
                e4_live.Settings().validate()
        with patch.dict(os.environ, {"E4_MAX_CONCURRENT_POSITIONS": "3"}, clear=False):
            with self.assertRaisesRegex(ValueError, "MAX_CONCURRENT"):
                e4_live.Settings().validate()

    def test_store_commits_only_one_entry_per_mint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = e4_live.Store(Path(directory) / "e4.db")
            try:
                self.assertTrue(store.mark_entry("mint", 0.8, "accepted"))
                self.assertFalse(store.has_entered("mint"))
                self.assertFalse(store.mark_entry("mint", 0.9, "duplicate pending"))
                store.order("request", "mint", "BUY", 0.05, None, "accepted")
                store.receipt(
                    "request",
                    "signature",
                    "direct",
                    True,
                    123,
                    None,
                    {"direct": "signature"},
                )
                self.assertTrue(store.has_entered("mint"))
                self.assertFalse(store.mark_entry("mint", 0.9, "duplicate confirmed"))
            finally:
                store.close()

    def test_position_persistence_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = e4_live.Store(Path(directory) / "e4.db")
            try:
                position = e4_live.Position(
                    position_id="position",
                    mint="mint",
                    status=e4_live.PositionStatus.OPEN,
                    opened_ns=time.time_ns(),
                    entry_sol=1.0,
                    tokens=1000.0,
                    remaining=1000.0,
                    entry_price=0.001,
                    max_price=0.001,
                    last_price=0.001,
                    entry_signature="signature",
                )
                store.save_position(position)
                loaded = store.load_open_positions()
                self.assertIn("mint", loaded)
                self.assertEqual(loaded["mint"].entry_signature, "signature")
            finally:
                store.close()


class E4PolicyTests(unittest.TestCase):
    def event(
        self,
        event_id: int,
        kind: e4_live.EventKind,
        price: float,
        trader: str,
        sol: float,
        at_ns: int,
        *,
        signature: str | None = None,
        fdv: float = 6_000,
    ) -> e4_live.Event:
        return e4_live.Event(
            event_id=event_id,
            kind=kind,
            mint="mint",
            source_ns=at_ns,
            received_ns=at_ns,
            signature=signature,
            trader=trader,
            sol_amount=sol,
            token_amount=1000,
            price_sol=price,
            fdv_usd=fdv,
        )

    def test_observed_bundled_microburst_can_enter(self) -> None:
        state = e4_live.TokenState("mint")
        now = time.time_ns()
        state.apply(
            self.event(
                1,
                e4_live.EventKind.CREATE,
                0.001,
                "creator",
                0.0,
                now,
                signature="create",
                fdv=3_000,
            ),
            None,
        )
        sequence = [
            ("creator", 3.0, "create"),
            ("buyer-1", 1.4, "bundle-a"),
            ("buyer-2", 1.4, "bundle-a"),
            ("buyer-3", 1.4, "bundle-a"),
            ("buyer-4", 1.4, "bundle-b"),
            ("buyer-5", 1.4, "bundle-b"),
            ("buyer-6", 2.0, "bundle-b"),
        ]
        for index, (trader, amount, signature) in enumerate(sequence, start=2):
            state.apply(
                self.event(
                    index,
                    e4_live.EventKind.BUY,
                    0.001 * (1.0 + 0.11 * (index - 1)),
                    trader,
                    amount,
                    now + (index - 1) * 150_000,
                    signature=signature,
                    fdv=5_800,
                ),
                None,
            )
        accepted, score, fraction, reason, features = e4_live.E4Policy(
            e4_live.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertTrue(accepted, reason)
        self.assertGreater(score, 0.65)
        self.assertGreater(fraction, 0)
        self.assertEqual(features["microburst_buyers"], 7)
        self.assertEqual(features["microburst_bundled_buys"], 6)

    def test_unbundled_fast_buyers_are_rejected(self) -> None:
        state = e4_live.TokenState("mint")
        now = time.time_ns()
        state.apply(
            self.event(1, e4_live.EventKind.CREATE, 0.001, "creator", 0, now, signature="create"),
            None,
        )
        for index in range(2, 10):
            state.apply(
                self.event(
                    index,
                    e4_live.EventKind.BUY,
                    0.001 * (1 + index * 0.1),
                    f"buyer-{index}",
                    2.0,
                    now + index * 100_000,
                    signature=f"single-{index}",
                ),
                None,
            )
        accepted, _, _, reason, _ = e4_live.E4Policy(
            e4_live.Settings(model_path=Path("missing.json"))
        ).entry(state)
        self.assertFalse(accepted)
        self.assertTrue(
            "multi-buy" in reason or "entry family" in reason,
            reason,
        )

    def test_wallet_touch_blocks_second_entry(self) -> None:
        state = e4_live.TokenState("mint")
        now = time.time_ns()
        state.apply(self.event(1, e4_live.EventKind.BUY, 0.001, "our-wallet", 2.0, now), "our-wallet")
        accepted, *_ = e4_live.E4Policy(e4_live.Settings(model_path=Path("missing.json"))).entry(state)
        self.assertFalse(accepted)

    def test_fast_failure_exits_everything(self) -> None:
        now = time.time_ns()
        state = e4_live.TokenState("mint")
        state.apply(self.event(1, e4_live.EventKind.SELL, 0.00090, "seller", 1.0, now), None)
        position = e4_live.Position(
            position_id="p",
            mint="mint",
            status=e4_live.PositionStatus.OPEN,
            opened_ns=now - 1_000_000_000,
            entry_sol=1,
            tokens=1000,
            remaining=1000,
            entry_price=0.001,
            max_price=0.001,
            last_price=0.001,
            entry_signature="sig",
        )
        action, fraction, _ = e4_live.E4Policy(e4_live.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_ALL")
        self.assertEqual(fraction, 1.0)

    def test_normal_confirmation_takes_thirty_percent(self) -> None:
        now = time.time_ns()
        state = e4_live.TokenState("mint")
        state.apply(self.event(1, e4_live.EventKind.BUY, 0.00110, "buyer", 1.0, now), None)
        position = e4_live.Position(
            position_id="p",
            mint="mint",
            status=e4_live.PositionStatus.OPEN,
            opened_ns=now - 1_000_000_000,
            entry_sol=1,
            tokens=1000,
            remaining=1000,
            entry_price=0.001,
            max_price=0.00110,
            last_price=0.00110,
            entry_signature="sig",
        )
        action, fraction, _ = e4_live.E4Policy(e4_live.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL")
        self.assertAlmostEqual(fraction, 0.30)


class E4SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_v15_schema_signature_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE canonical_events(id INTEGER PRIMARY KEY,event_type TEXT,mint TEXT,source_timestamp_ns INTEGER,price_sol REAL,fdv_usd REAL)"
            )
            conn.execute(
                "INSERT INTO canonical_events(event_type,mint,source_timestamp_ns,price_sol,fdv_usd) VALUES('BUY','mint',1,0.001,4878)"
            )
            conn.commit()
            conn.close()
            source = e4_live.SQLiteEventSource(path, 0.001)
            with patch.dict(os.environ, {"E4_CONSUME_EXISTING_EVENTS": "true"}, clear=False):
                event = await asyncio.wait_for(anext(source.events()), timeout=1)
            self.assertEqual(event.kind, e4_live.EventKind.BUY)
            self.assertEqual(event.mint, "mint")


if __name__ == "__main__":
    unittest.main()
