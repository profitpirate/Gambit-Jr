from __future__ import annotations

import unittest

import golden_full3s_v2 as v2
import golden_top4_engine as top4
from golden_loss2s_engine import (
    ARMS,
    CHECKPOINT_MS,
    FULL2,
    V2_LOSS2,
    CampaignEngine,
    Config,
    LossControlEngine,
    LossV2Engine,
)
from test_golden_full3s_v2 import T0, decision, state


MINT = "fixture-coin"


def open_engine(engine):
    d = decision(mint=MINT, now=T0)
    assert engine.submit(d, state(T0), T0)
    fill = T0 + 250_000_000
    engine.tick(fill, {MINT: state(fill)}, fill)
    return fill, engine.bundles[MINT]["positions"][v2.ARM]


class Loss2sTests(unittest.TestCase):
    def setUp(self):
        CampaignEngine.output = None
        CampaignEngine.smoke = False
        CampaignEngine.require_reporter = False
        top4.CampaignEngine.output = None
        top4.CampaignEngine.smoke = False
        top4.CampaignEngine.require_reporter = False

    def test_six_accounts_start_at_three_sol(self):
        e = CampaignEngine(Config(), fixture_mode=True)
        self.assertEqual(set(e.accounts), set(ARMS))
        self.assertTrue(all(a["cash"] == 3 for a in e.accounts.values()))
        self.assertEqual(
            set(top4.ARMS),
            {"FULL_3S", "FULL_4S", "FULL_7S", "FULL_3S_V2"},
        )

    def test_red_at_two_seconds_schedules_full_exit(self):
        e = LossControlEngine(v2.Config())
        fill, p = open_engine(e)
        checkpoint = fill + CHECKPOINT_MS * 1_000_000
        adverse = state(checkpoint, 20, 1.5e9)
        e.tick(checkpoint, {MINT: adverse}, checkpoint)
        self.assertTrue(p["loss2s_checked"])
        self.assertTrue(p["loss2s_triggered"])
        self.assertLess(p["loss2s_checkpoint_net_return"], 0)
        self.assertEqual(p["intent"]["reason"], "LOSS_2S_CHECKPOINT")
        self.assertEqual(p["intent"]["scheduled_ns"], checkpoint)

        due = checkpoint + e.c.inclusion_delay_ms * 1_000_000
        e.tick(due, {MINT: state(due, 20, 1.5e9)}, due)
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(p["hold_ms"], CHECKPOINT_MS + e.c.inclusion_delay_ms)

    def test_green_at_two_seconds_is_not_stopped_if_later_red(self):
        e = LossControlEngine(v2.Config())
        fill, p = open_engine(e)
        checkpoint = fill + CHECKPOINT_MS * 1_000_000
        e.tick(checkpoint, {MINT: state(checkpoint, 50, 7e8)}, checkpoint)
        self.assertTrue(p["loss2s_checked"])
        self.assertFalse(p["loss2s_triggered"])
        self.assertIsNone(p["intent"])
        self.assertEqual(p["horizon_ms"], 3000)

        later_red = fill + 2_500_000_000
        e.tick(later_red, {MINT: state(later_red, 20, 1.5e9)}, later_red)
        self.assertIsNone(p["intent"])
        self.assertEqual(p["horizon_ms"], 3000)

        horizon = fill + 3_000_000_000
        e.tick(horizon, {MINT: state(horizon, 20, 1.5e9)}, horizon)
        self.assertEqual(p["intent"]["reason"], "HORIZON_FLATTEN")
        due = horizon + e.c.inclusion_delay_ms * 1_000_000
        e.tick(due, {MINT: state(due, 20, 1.5e9)}, due)
        self.assertEqual(p["hold_ms"], 3000 + e.c.inclusion_delay_ms)

    def test_v2_shadow_uses_same_loss_checkpoint(self):
        e = LossV2Engine(v2.Config())
        fill, p = open_engine(e)
        checkpoint = fill + CHECKPOINT_MS * 1_000_000
        e.tick(checkpoint, {MINT: state(checkpoint, 20, 1.5e9)}, checkpoint)
        self.assertTrue(p["loss2s_triggered"])
        self.assertEqual(p["intent"]["reason"], "LOSS_2S_CHECKPOINT")

    def test_campaign_parent_full3_remains_three_seconds(self):
        e = CampaignEngine(Config(), fixture_mode=True)
        d = decision(mint=MINT, now=T0)
        self.assertTrue(e.submit(d, state(T0), T0))
        for dt in range(0, 25_001, 250):
            now = T0 + dt * 1_000_000
            x, y = (30, 1e9) if dt < 2250 else (20, 1.5e9)
            e.tick(now, {MINT: state(now, x, y)}, now)

        parent = {p["mint"]: p for p in e.accounts["FULL_3S"]["ledger"]}
        shadow = {p["mint"]: p for p in e.accounts[FULL2]["ledger"]}
        self.assertIn(MINT, parent)
        self.assertIn(MINT, shadow)
        self.assertEqual(parent[MINT]["horizon_ms"], 3000)
        self.assertEqual(shadow[MINT]["horizon_ms"], 2000)
        self.assertTrue(shadow[MINT]["loss2s_triggered"])

    def test_restore_clean_campaign_keeps_shadow_accounts(self):
        e = CampaignEngine(Config(), fixture_mode=True)
        raw = e.persistence()
        r = CampaignEngine(Config(), raw, fixture_mode=True)
        self.assertEqual(set(r.accounts), set(ARMS))
        self.assertEqual(r.accounts[FULL2]["cash"], 3)
        self.assertEqual(r.accounts[V2_LOSS2]["cash"], 3)


if __name__ == "__main__":
    unittest.main()
