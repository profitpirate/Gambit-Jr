from __future__ import annotations
import unittest
import golden_top4_v2_eventstream as fix
import golden_full3s_v2 as v2
from golden_top4_engine import Config
from test_golden_full3s_v2 import decision,state,T0

class EventStreamFixTests(unittest.TestCase):
    def test_quiet_coin_does_not_become_uncertain_and_exits_near_three_seconds(self):
        e=fix.EventStreamSingleEngine(Config())
        d=decision();s=state(T0)
        self.assertTrue(e.submit(d,s,T0))
        for ms in range(0,4501,50):
            now=T0+ms*1_000_000
            e.tick(now,{},now)
        self.assertEqual(len(e.accounts[v2.ARM]['ledger']),1)
        p=e.accounts[v2.ARM]['ledger'][0]
        self.assertNotIn(d['mint'],e.uncertain_mints)
        self.assertLess((p['exit_ns']-p['entry_ns'])/1e6,3500)

    def test_global_feed_loss_still_invalidates_forward_evidence(self):
        e=fix.EventStreamSingleEngine(Config())
        d=decision();s=state(T0)
        self.assertTrue(e.submit(d,s,T0))
        now=T0+(e.c.feed_stale_ms+1000)*1_000_000
        e.tick(now,{},T0)
        self.assertIn(d['mint'],e.uncertain_mints)

    def test_frozen_source_selection_policy_unchanged(self):
        self.assertEqual(v2.MODEL,'FULL_3S_V2')
        self.assertEqual(v2.ARM,'FULL_3S')

if __name__=='__main__': unittest.main()
