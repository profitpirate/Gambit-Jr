from __future__ import annotations

import time
import unittest
from pathlib import Path

from memecoin_bot import e4_copy_exit_v10 as copy

core = copy.core
v6 = copy.v6


class CopyExitTests(unittest.TestCase):
    def setUp(self) -> None:
        v6._CONTEXT_BY_MINT.clear()
        v6._PROFILE_BY_MINT.clear()

    def position(self, mint: str, family: str):
        v6._PROFILE_BY_MINT[mint] = v6.EntryProfile(
            family=family,
            tier="standard",
            fraction=0.0125,
            score=0.85,
            first_partial_fraction=0.30,
            features={},
        )
        now = time.time_ns()
        position = core.Position(
            position_id="p",
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=now,
            entry_sol=1.0,
            tokens=1000.0,
            remaining=1000.0,
            entry_price=1e-6,
            max_price=1e-6,
            last_price=1e-6,
            entry_signature="entry",
        )
        state = core.TokenState(mint)
        return position, state

    def test_first_e4_sell_triggers_copy_partial(self) -> None:
        position, state = self.position("mint", "e4_teacher_confirmed_copy_safe")
        v6._CONTEXT_BY_MINT["mint"] = {
            "e4_copy_sell_events": 1,
            "e4_copy_cumulative_sell_fraction": 0.30,
            "e4_copy_latest_sell_fraction": 0.30,
        }
        action, fraction, reason = core.E4Policy(core.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_PARTIAL", reason)
        self.assertAlmostEqual(fraction, 0.30)

    def test_e4_full_exit_triggers_copy_full_exit(self) -> None:
        position, state = self.position("mint", "e4_teacher_confirmed_copy_safe")
        position.first_partial_done = True
        v6._CONTEXT_BY_MINT["mint"] = {
            "e4_copy_sell_events": 2,
            "e4_copy_cumulative_sell_fraction": 0.95,
            "e4_copy_latest_sell_fraction": 0.65,
        }
        action, fraction, reason = core.E4Policy(core.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertEqual(action, "SELL_ALL", reason)
        self.assertEqual(fraction, 1.0)

    def test_independent_entry_does_not_blindly_mirror_e4(self) -> None:
        position, state = self.position("mint", "elite_recurring_creator")
        v6._CONTEXT_BY_MINT["mint"] = {
            "e4_copy_sell_events": 1,
            "e4_copy_cumulative_sell_fraction": 1.0,
            "e4_copy_latest_sell_fraction": 1.0,
        }
        action, _, reason = core.E4Policy(core.Settings(model_path=Path("missing.json"))).exit(position, state)
        self.assertNotIn("E4 copy", reason)
        self.assertIn(action, {"HOLD", "SELL_PARTIAL", "SELL_ALL"})


if __name__ == "__main__":
    unittest.main()
