import unittest

from tools import golden_management_v2 as v2
from tools import golden_management_v2_1 as v21


class TestGoldenManagementV21(unittest.TestCase):
    def test_partials_2s_keeps_new_initial_then_flattens(self):
        g = v21.TwoSecondPartialsGuardian(v2.TIERS['EXCEPTIONAL'])
        first = g.on_mark(v2.Mark(300, -0.04))
        self.assertEqual(first.kind, 'INITIAL')
        self.assertAlmostEqual(first.fraction_of_entry, 0.20)
        self.assertIsNone(g.on_mark(v2.Mark(1500, 0.30)))
        exit_action = g.on_mark(v2.Mark(2001, 0.18))
        self.assertEqual(exit_action.kind, 'EXIT')
        self.assertAlmostEqual(exit_action.fraction_of_entry, 0.80)
        self.assertTrue(g.closed)

    def test_positive_holder_and_buy_flow_earns_continuation(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1100, 'BUY', 'a', 1.0, 100, 0.01))
        ft.ingest(v21.FlowEvent(1450, 'BUY', 'b', 1.2, 100, 0.05))
        ft.ingest(v21.FlowEvent(1800, 'BUY', 'c', 1.5, 100, 0.11))
        score, reasons = v21.continuation_score(ft.snapshot(2000))
        self.assertGreaterEqual(score, 3)
        self.assertIn('NET_BUY_FLOW_POSITIVE', reasons)

    def test_sell_pressure_fails_anchor(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1100, 'BUY', 'a', 0.2, 100, 0.02))
        ft.ingest(v21.FlowEvent(1500, 'SELL', 'a', 1.0, 100, -0.04))
        ft.ingest(v21.FlowEvent(1900, 'SELL', 'b', 1.0, 100, -0.10))
        g = v21.FlowAwareGuardian(v2.TIERS['HIGH'], ft)
        self.assertEqual(g.on_mark(v2.Mark(325, -0.02)).kind, 'INITIAL')
        action = g.on_mark(v2.Mark(2000, -0.10))
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, 'EXIT')

    def test_views_are_optional_not_fabricated(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1500, 'BUY', 'a', 1.0, 100, 0.05, views=None))
        self.assertIsNone(ft.snapshot(2000)['view_growth_1000ms'])


if __name__ == '__main__':
    unittest.main()
