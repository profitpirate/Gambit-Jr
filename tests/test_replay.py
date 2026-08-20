from __future__ import annotations

import unittest
from pathlib import Path

from memecoin_bot.replay import ReplayRunner
from tests.helpers import settings, store, temp_db_path


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_replay_scores_rejects_signals_and_tracks(self) -> None:
        with temp_db_path() as path:
            config = settings(path)
            db = store(path)
            fixture = Path(__file__).resolve().parents[1] / "fixtures" / "replay_lifecycle.json"
            report = await ReplayRunner(config, db).run(fixture)
            self.assertEqual(report["evidence_type"], "SIMULATION_ONLY_NOT_LIVE_E2E")
            self.assertEqual(len(report["signals_created"]), 1)
            self.assertEqual(report["decisions"][1]["classification"], "REJECT")
            self.assertEqual(len(report["milestones"]), 3)  # 1.5, 2, 3
            db.close()


if __name__ == "__main__":
    unittest.main()
