from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from memecoin_bot import e4_live
from memecoin_bot import e4_final  # applies final production patches


class FinalSourceTests(unittest.TestCase):
    def test_live_source_starts_at_current_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE canonical_events(id INTEGER PRIMARY KEY,event_type TEXT,mint TEXT,source_timestamp_ns INTEGER)"
            )
            conn.executemany(
                "INSERT INTO canonical_events(event_type,mint,source_timestamp_ns) VALUES('BUY',?,?)",
                [("old-a", 1), ("old-b", 2)],
            )
            conn.commit()
            conn.close()
            source = e4_live.SQLiteEventSource(path, 0.001)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("E4_CONSUME_EXISTING_EVENTS", None)
                table, column = source._discover()
            self.assertEqual(table, "canonical_events")
            self.assertEqual(column, "id")
            self.assertEqual(source.last_id, 2)

    def test_explicit_replay_opt_in_does_not_advance_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE canonical_events(id INTEGER PRIMARY KEY,event_type TEXT,mint TEXT)")
            conn.execute("INSERT INTO canonical_events(event_type,mint) VALUES('BUY','old')")
            conn.commit()
            conn.close()
            source = e4_live.SQLiteEventSource(path, 0.001)
            with patch.dict(os.environ, {"E4_CONSUME_EXISTING_EVENTS": "true"}, clear=False):
                source._discover()
            self.assertEqual(source.last_id, 0)


class FinalInterlockTests(unittest.TestCase):
    def test_cli_requires_environment_and_flag(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env.pop("E4_LIVE", None)
        process = subprocess.run(
            [sys.executable, "-m", "memecoin_bot.e4_exec", "run", "--live"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("requires both E4_LIVE=true and --live", process.stderr + process.stdout)


class FinalRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_signature_is_rebroadcast_until_confirmed(self) -> None:
        settings = e4_live.Settings(
            route_urls={"route-a": "https://example.invalid"},
            direct_rpc_route=False,
            confirmation_timeout_seconds=0.2,
        )
        rpc = AsyncMock()
        sender = e4_final.RebroadcastRouteSender(settings, rpc)
        sender._send = AsyncMock(
            return_value=e4_live.RouteResult(
                name="route-a#1",
                submitted_ns=1,
                completed_ns=2,
                accepted=True,
                result="signature",
            )
        )
        statuses = iter([(False, None, None), (True, 123, None)])
        sender._status = AsyncMock(side_effect=lambda signature: next(statuses))
        with patch.dict(
            os.environ,
            {"E4_REBROADCAST_ROUNDS": "2", "E4_REBROADCAST_INTERVAL_SECONDS": "0.025"},
            clear=False,
        ):
            route, confirmed, slot, error, results = await sender.submit("transaction", "signature")
        self.assertTrue(confirmed)
        self.assertEqual(route, "route-a")
        self.assertEqual(slot, 123)
        self.assertIsNone(error)
        self.assertEqual(len(results), 2)
        self.assertEqual(sender._send.await_count, 2)


class BuilderPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_never_exceeds_two_workers(self) -> None:
        with patch.dict(os.environ, {"E4_BUILDER_WORKERS": "9"}, clear=False):
            pool = e4_final.BuilderPool(("node", "fake.mjs"))
        self.assertEqual(len(pool.workers), 2)


if __name__ == "__main__":
    unittest.main()
