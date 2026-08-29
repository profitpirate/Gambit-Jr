from __future__ import annotations

import unittest
from pathlib import Path

from memecoin_bot.replay import ReplayRunner
from tests.helpers import settings, store, temp_db_path


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_replay_scores_rejects_signals_and_tracks(self) -> None:
        with temp_db_path() as path:
            config = settings(path)
            config.public_alerts_enabled = True
            db = store(path)
            fixture = Path(__file__).resolve().parents[1] / "fixtures" / "replay_lifecycle.json"
            report = await ReplayRunner(config, db).run(fixture)
            self.assertEqual(report["evidence_type"], "SIMULATION_ONLY_NOT_LIVE_E2E")
            self.assertEqual(len(report["signals_created"]), 1)
            self.assertEqual(report["decisions"][1]["classification"], "REJECT")
            self.assertEqual(len(report["milestones"]), 3)  # 1.5, 2, 3
            db.close()

    async def test_v12_multichain_replay_uses_production_radar_and_lifecycle(self) -> None:
        with temp_db_path() as path:
            config = settings(path)
            config.public_alerts_enabled = True
            db = store(path)
            fixture = Path(__file__).resolve().parents[1] / "fixtures" / "replay_v12_multichain.json"
            report = await ReplayRunner(config, db).run(fixture)
            self.assertEqual(len(report["outcomes"]), 9)
            self.assertGreaterEqual(len(report["radar_events"]), 4)
            # Unknown BNB concentration/transfer controls no longer receive
            # full safety credit merely to satisfy the replay fixture.
            self.assertGreaterEqual(len(report["signals_created"]), 2)
            chains = {row["chain"] for row in report["decisions"]}
            self.assertEqual(chains, {"solana", "bsc"})
            self.assertTrue(any("FAILED_PROVIDER" in cycle["candidates"] for cycle in report["cycles"]))
            coverage = report["performance"]["coverage"]
            self.assertGreaterEqual(coverage["major_runners_discovered"], 3)
            self.assertGreaterEqual(coverage["major_runners_radar"], 3)
            db.close()


if __name__ == "__main__":
    unittest.main()
