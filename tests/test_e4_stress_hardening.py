from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_hardening

core = e4_hardening.core


class CanonicalJournalCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def create_schema(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE canonical_events(
            event_id TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            canonical_token TEXT NOT NULL,
            source_timestamp TEXT NOT NULL,
            received_timestamp TEXT NOT NULL,
            available_timestamp TEXT NOT NULL,
            transaction_signature TEXT,
            payload_json TEXT NOT NULL
            )"""
        )
        connection.commit()
        connection.close()

    @staticmethod
    def insert_trade(path: Path, suffix: str, side: str = "buy") -> None:
        connection = sqlite3.connect(path)
        now = "2026-08-30T12:00:00+00:00"
        payload = {
            "side": side,
            "actor": f"wallet-{suffix}",
            "sol_amount": 0.2,
            "token_amount": 1_000_000_000,
            "virtual_sol_reserves": 30_000_000_000,
            "virtual_token_reserves": 1_000_000_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        }
        connection.execute(
            "INSERT INTO canonical_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                suffix * 64,
                f"key-{suffix}",
                "TOKEN_TRADE",
                f"mint-{suffix}",
                now,
                now,
                now,
                f"signature-{suffix}",
                json.dumps(payload),
            ),
        )
        connection.commit()
        connection.close()

    async def test_real_v15_text_event_id_and_canonical_token_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational.db"
            self.create_schema(path)
            self.insert_trade(path, "a")
            source = core.SQLiteEventSource(path, 0.001)
            with patch.dict(
                os.environ,
                {"E4_CONSUME_EXISTING_EVENTS": "true"},
                clear=False,
            ):
                event = await asyncio.wait_for(anext(source.events()), timeout=1)
            self.assertEqual(event.kind, core.EventKind.BUY)
            self.assertEqual(event.mint, "mint-a")
            self.assertEqual(event.event_id, 1)
            self.assertAlmostEqual(event.price_sol or 0, 3e-8, places=14)
            self.assertAlmostEqual(event.fdv_usd or 0, 4_500, delta=1)

    async def test_live_tail_uses_rowid_not_sha256_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational.db"
            self.create_schema(path)
            self.insert_trade(path, "a")
            source = core.SQLiteEventSource(path, 0.001)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("E4_CONSUME_EXISTING_EVENTS", None)
                table, cursor = source._discover()
            self.assertEqual(table, "canonical_events")
            self.assertEqual(cursor, "rowid")
            self.assertEqual(source.last_id, 1)
            self.insert_trade(path, "b")
            rows = source._read()
            self.assertEqual(len(rows), 1)
            event = core.Event.from_row(dict(rows[0]))
            self.assertEqual(event.event_id, 2)
            self.assertEqual(event.mint, "mint-b")


class EconomicEventDeduplicationTests(unittest.TestCase):
    def test_trade_and_wallet_projection_count_once(self) -> None:
        state = core.TokenState("mint")
        now = time.time_ns()
        event = core.Event(
            event_id=1,
            kind=core.EventKind.BUY,
            mint="mint",
            source_ns=now,
            received_ns=now,
            signature="same-signature",
            trader="buyer",
            sol_amount=0.2,
            token_amount=100,
            price_sol=3e-8,
            fdv_usd=4_500,
        )
        duplicate = core.Event(
            event_id=2,
            kind=core.EventKind.BUY,
            mint="mint",
            source_ns=now + 1,
            received_ns=now + 1,
            signature="same-signature",
            trader="buyer",
            sol_amount=0.2,
            token_amount=100,
            price_sol=3e-8,
            fdv_usd=4_500,
        )
        state.apply(event, None)
        state.apply(duplicate, None)
        self.assertAlmostEqual(state.flow(1000).buy_sol, 0.2)
        self.assertEqual(len(state.flow(1000).buyers), 1)


class EntryStateTests(unittest.TestCase):
    def test_only_confirmed_or_uncertain_submission_blocks_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory) / "e4.db")
            try:
                self.assertTrue(store.mark_entry("mint", 0.8, "test"))
                self.assertFalse(store.has_entered("mint"))
                store.order("rejected", "mint", "BUY", 0.05, None, "test")
                store.receipt(
                    "rejected",
                    "signature-a",
                    "NONE",
                    False,
                    None,
                    "all routes rejected",
                    {"direct": "rejected"},
                )
                self.assertFalse(store.has_entered("mint"))
                self.assertTrue(store.mark_entry("mint", 0.8, "retry"))
                store.order("uncertain", "mint", "BUY", 0.05, None, "retry")
                store.receipt(
                    "uncertain",
                    "signature-b",
                    "direct",
                    False,
                    None,
                    "confirmation timeout",
                    {"direct": "signature-b"},
                )
                self.assertTrue(store.has_entered("mint"))
                self.assertFalse(store.mark_entry("mint", 0.9, "unsafe duplicate"))
            finally:
                store.close()

    def test_confirmed_receipt_commits_single_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(Path(directory) / "e4.db")
            try:
                self.assertTrue(store.mark_entry("mint", 0.8, "test"))
                store.order("confirmed", "mint", "BUY", 0.05, None, "test")
                store.receipt(
                    "confirmed",
                    "signature",
                    "direct",
                    True,
                    123,
                    None,
                    {"direct": "signature"},
                )
                self.assertTrue(store.has_entered("mint"))
                self.assertFalse(store.mark_entry("mint", 0.9, "duplicate"))
            finally:
                store.close()


class FakeRpc:
    def __init__(self, wallet: str, balance: float):
        self.wallet = wallet
        self.balance_sol = balance
        self.tokens: dict[str, float] = {}
        self.transactions: dict[str, dict] = {}
        self.sequence = 0

    async def balance(self, wallet: str) -> float:
        assert wallet == self.wallet
        return self.balance_sol

    async def token_balance(self, wallet: str, mint: str) -> float:
        assert wallet == self.wallet
        return self.tokens.get(mint, 0.0)

    async def call(self, method: str, params: list) -> dict | None:
        if method == "getTransaction":
            return self.transactions.get(str(params[0]))
        raise AssertionError(f"unexpected RPC method {method}")

    def apply_buy(self, request: dict, signature: str) -> None:
        mint = request["mint"]
        pre_sol = self.balance_sol
        pre_tokens = self.tokens.get(mint, 0.0)
        amount = float(request["amount"])
        fees = float(request["priority_fee_sol"]) + float(request["tip_sol"])
        received = amount / 0.00005
        self.balance_sol -= amount + fees
        self.tokens[mint] = pre_tokens + received
        self.transactions[signature] = self.transaction(
            mint,
            pre_sol,
            self.balance_sol,
            pre_tokens,
            self.tokens[mint],
        )

    def apply_sell(self, request: dict, signature: str) -> None:
        mint = request["mint"]
        pre_sol = self.balance_sol
        pre_tokens = self.tokens.get(mint, 0.0)
        sold = min(pre_tokens, float(request["amount"]))
        proceeds = sold * 0.00006
        fees = float(request["priority_fee_sol"]) + float(request["tip_sol"])
        self.tokens[mint] = max(0.0, pre_tokens - sold)
        self.balance_sol += proceeds - fees
        self.transactions[signature] = self.transaction(
            mint,
            pre_sol,
            self.balance_sol,
            pre_tokens,
            self.tokens[mint],
        )

    def transaction(
        self,
        mint: str,
        pre_sol: float,
        post_sol: float,
        pre_tokens: float,
        post_tokens: float,
    ) -> dict:
        def balance_row(value: float) -> dict:
            return {
                "mint": mint,
                "owner": self.wallet,
                "uiTokenAmount": {"uiAmountString": str(value)},
            }

        return {
            "transaction": {
                "message": {"accountKeys": [{"pubkey": self.wallet}]}
            },
            "meta": {
                "preBalances": [round(pre_sol * core.LAMPORTS_PER_SOL)],
                "postBalances": [round(post_sol * core.LAMPORTS_PER_SOL)],
                "preTokenBalances": [balance_row(pre_tokens)] if pre_tokens else [],
                "postTokenBalances": [balance_row(post_tokens)] if post_tokens else [],
            },
        }


async def make_engine(
    path: Path,
    balance: float = 0.3,
) -> tuple[core.Engine, FakeRpc]:
    engine = object.__new__(core.Engine)
    engine.settings = core.Settings(
        execution_db=path,
        wallet="wallet",
        vault=None,
        min_position_sol=0.001,
        max_position_fraction=0.20,
        max_position_sol=5.0,
        reserve_sol=0.03,
        model_path=Path("missing-model.json"),
    )
    engine.store = core.Store(path)
    engine.rpc = FakeRpc("wallet", balance)
    engine.signer = SimpleNamespace(wallet="wallet")
    engine.tokens = {}
    engine.positions = {}
    engine.pending_entries = set()
    engine.pending_exits = set()
    engine.tasks = set()
    engine.stop_event = asyncio.Event()
    engine.allocation_lock = asyncio.Lock()
    engine.reserved_sol = 0.0

    async def execute(
        self: core.Engine,
        request_id: str,
        request: dict,
    ):
        rpc: FakeRpc = self.rpc
        rpc.sequence += 1
        signature = f"signature-{rpc.sequence}"
        if request["side"] == "BUY":
            rpc.apply_buy(request, signature)
        elif request["side"] == "SELL":
            rpc.apply_sell(request, signature)
        else:
            raise AssertionError(request["side"])
        self.store.receipt(
            request_id,
            signature,
            "fake",
            True,
            rpc.sequence,
            None,
            {"fake": signature},
        )
        return signature, True, rpc.sequence, None

    engine.execute = types.MethodType(execute, engine)
    return engine, engine.rpc


class ExecutionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_entries_never_oversize_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 0.3)
            try:
                states = []
                for index in range(2):
                    state = core.TokenState(f"mint-{index}")
                    state.price_sol = 0.00005
                    state.fdv_usd = 4_878
                    engine.tokens[state.mint] = state
                    engine.pending_entries.add(state.mint)
                    states.append(state)
                await asyncio.gather(
                    engine.execute_buy(states[0], 0.95, 0.20, "stress"),
                    engine.execute_buy(states[1], 0.95, 0.20, "stress"),
                )
                if engine.tasks:
                    await asyncio.gather(
                        *tuple(engine.tasks),
                        return_exceptions=True,
                    )
                self.assertEqual(len(engine.positions), 2)
                self.assertGreaterEqual(
                    rpc.balance_sol,
                    engine.settings.reserve_sol - 1e-9,
                )
                self.assertLessEqual(
                    sum(
                        position.entry_sol
                        for position in engine.positions.values()
                    ),
                    0.3 - engine.settings.reserve_sol + 1e-9,
                )
                self.assertEqual(engine.reserved_sol, 0.0)
            finally:
                engine.store.close()

    async def test_partial_then_full_exit_reconciles_exact_token_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, rpc = await make_engine(Path(directory) / "e4.db", 1.0)
            try:
                mint = "mint"
                rpc.tokens[mint] = 1_000.0
                state = core.TokenState(mint)
                state.price_sol = 0.00006
                state.fdv_usd = 6_000
                engine.tokens[mint] = state
                position = core.Position(
                    position_id="position",
                    mint=mint,
                    status=core.PositionStatus.OPEN,
                    opened_ns=time.time_ns() - 1_000_000_000,
                    entry_sol=0.05,
                    tokens=1_000,
                    remaining=1_000,
                    entry_price=0.00005,
                    max_price=0.00006,
                    last_price=0.00006,
                    entry_signature="entry",
                )
                engine.positions[mint] = position
                engine.store.save_position(position)
                engine.pending_exits.add(mint)
                await engine.execute_sell(
                    position,
                    0.30,
                    "E4 normal first partial",
                )
                if engine.tasks:
                    await asyncio.gather(
                        *tuple(engine.tasks),
                        return_exceptions=True,
                    )
                self.assertTrue(position.first_partial_done)
                self.assertAlmostEqual(
                    position.first_partial_fraction or 0,
                    0.30,
                    places=9,
                )
                self.assertAlmostEqual(position.remaining, 700.0, places=6)
                self.assertEqual(position.status, core.PositionStatus.PARTIAL)

                engine.pending_exits.add(mint)
                await engine.execute_sell(
                    position,
                    1.0,
                    "E4 runner flow broke",
                )
                for _ in range(5):
                    if engine.tasks:
                        await asyncio.gather(
                            *tuple(engine.tasks),
                            return_exceptions=True,
                        )
                    await asyncio.sleep(0)
                self.assertNotIn(mint, engine.positions)
                self.assertEqual(position.status, core.PositionStatus.CLOSED)
                self.assertAlmostEqual(position.remaining, 0.0, places=9)
                self.assertAlmostEqual(rpc.tokens[mint], 0.0, places=9)
            finally:
                engine.store.close()


class RepeatedPolicyStressTests(unittest.TestCase):
    def test_failure_profit_and_horizon_paths_repeat_without_invariant_break(self) -> None:
        settings = core.Settings(model_path=Path("missing-model.json"))
        policy = core.E4Policy(settings)
        now = time.time_ns()
        actions = {"failure": 0, "partial": 0, "horizon": 0}
        for index in range(2_000):
            state = core.TokenState(f"mint-{index}")
            entry = 0.001
            mode = index % 3
            if mode == 0:
                price = 0.00090
                kind = core.EventKind.SELL
                age_ms = 1_000
            elif mode == 1:
                price = 0.00116
                kind = core.EventKind.BUY
                age_ms = 1_000
            else:
                price = 0.00101
                kind = core.EventKind.BUY
                age_ms = 60_001
            state.apply(
                core.Event(
                    event_id=index + 1,
                    kind=kind,
                    mint=state.mint,
                    source_ns=now,
                    received_ns=now,
                    signature=f"sig-{index}",
                    trader=f"wallet-{index}",
                    sol_amount=1.0,
                    token_amount=1_000,
                    price_sol=price,
                    fdv_usd=4_878,
                ),
                None,
            )
            position = core.Position(
                position_id=f"p-{index}",
                mint=state.mint,
                status=core.PositionStatus.OPEN,
                opened_ns=time.time_ns() - age_ms * 1_000_000,
                entry_sol=1.0,
                tokens=1_000,
                remaining=1_000,
                entry_price=entry,
                max_price=max(entry, price),
                last_price=entry,
                entry_signature="entry",
                first_partial_done=(mode == 2),
            )
            action, fraction, reason = policy.exit(position, state)
            if mode == 0:
                self.assertEqual((action, fraction), ("SELL_ALL", 1.0))
                actions["failure"] += 1
            elif mode == 1:
                self.assertEqual(action, "SELL_PARTIAL")
                self.assertIn(fraction, (0.20, 0.30))
                actions["partial"] += 1
            else:
                self.assertEqual((action, fraction), ("SELL_ALL", 1.0))
                self.assertIn("horizon", reason)
                actions["horizon"] += 1
        self.assertEqual(sum(actions.values()), 2_000)


if __name__ == "__main__":
    unittest.main()
