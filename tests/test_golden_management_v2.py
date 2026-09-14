import math
import unittest

from tools import golden_management_v2 as mgmt


class TestGoldenManagementV2(unittest.TestCase):
    def test_exact_sizing_ladder(self):
        self.assertEqual(mgmt.TIERS['BASE'].position_fraction, 0.05)
        self.assertEqual(mgmt.TIERS['MEDIUM'].position_fraction, 0.08)
        self.assertEqual(mgmt.TIERS['HIGH'].position_fraction, 0.15)
        self.assertEqual(mgmt.TIERS['EXCEPTIONAL'].position_fraction, 0.25)

    def test_initial_ladder(self):
        self.assertEqual(mgmt.TIERS['BASE'].initial_fraction, 0.30)
        self.assertEqual(mgmt.TIERS['MEDIUM'].initial_fraction, 0.30)
        self.assertEqual(mgmt.TIERS['HIGH'].initial_fraction, 0.20)
        self.assertEqual(mgmt.TIERS['EXCEPTIONAL'].initial_fraction, 0.20)

    def test_conviction_uses_only_entry_fields(self):
        a=mgmt.conviction({'prior_win_rate':.90,'prior_appearances':100,'buyer_count':5,'future_pnl':-999,'e4_bought':True})
        b=mgmt.conviction({'prior_win_rate':.90,'prior_appearances':100,'buyer_count':5,'future_pnl':999,'e4_bought':False})
        self.assertEqual(a,b)
        self.assertEqual(a['conviction_tier'],'EXCEPTIONAL')
        self.assertEqual(a['position_fraction'],.25)

    def test_temporary_drawdown_not_dumb_stopped(self):
        g=mgmt.RunnerGuardian(mgmt.TIERS['HIGH'])
        self.assertEqual(g.on_mark(mgmt.Mark(325,-.03)).kind,'INITIAL')
        self.assertIsNone(g.on_mark(mgmt.Mark(500,-.17)))
        self.assertFalse(g.closed)

    def test_sustained_failure_exits(self):
        g=mgmt.RunnerGuardian(mgmt.TIERS['HIGH'])
        g.on_mark(mgmt.Mark(325,-.02))
        g.on_mark(mgmt.Mark(900,-.19))
        g.on_mark(mgmt.Mark(1000,-.21))
        g.on_mark(mgmt.Mark(1100,-.23))
        a=g.on_mark(mgmt.Mark(1200,-.26))
        self.assertIsNotNone(a)
        self.assertEqual(a.kind,'EXIT')
        self.assertTrue(g.closed)

    def test_catastrophic_guard(self):
        g=mgmt.RunnerGuardian(mgmt.TIERS['EXCEPTIONAL'])
        g.on_mark(mgmt.Mark(300,-.05))
        a=g.on_mark(mgmt.Mark(500,-.36))
        self.assertEqual(a.kind,'EXIT')

    def test_strong_runner_not_flattened_at_two_seconds(self):
        g=mgmt.RunnerGuardian(mgmt.TIERS['EXCEPTIONAL'])
        self.assertEqual(g.on_mark(mgmt.Mark(300,.02)).kind,'INITIAL')
        self.assertEqual(g.on_mark(mgmt.Mark(1000,.16)).kind,'SCALE_OUT')
        g.on_mark(mgmt.Mark(1500,.31))
        g.on_mark(mgmt.Mark(2000,.35))
        self.assertFalse(g.closed)

    def test_actions_never_sell_more_than_entry(self):
        g=mgmt.RunnerGuardian(mgmt.TIERS['EXCEPTIONAL'])
        marks=[(300,.01),(500,.16),(700,.31),(900,.51),(1100,.81),(1300,1.21),(16000,1.0)]
        total=0.0
        for t,r in marks:
            a=g.on_mark(mgmt.Mark(t,r))
            if a: total += a.fraction_of_entry
        self.assertLessEqual(total,1.000000001)
        self.assertTrue(g.closed)
        self.assertTrue(math.isclose(g.remaining_fraction,0.0,abs_tol=1e-9))

    def test_no_fixed_185_anywhere_in_tiers(self):
        self.assertNotIn(.0185,[x.position_fraction for x in mgmt.TIERS.values()])


if __name__=='__main__': unittest.main()
