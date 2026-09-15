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

    def test_full_2s_has_no_initial(self):
        g = v21.FullTwoSecondGuardian()
        self.assertIsNone(g.on_mark(v2.Mark(350, -0.20)))
        self.assertIsNone(g.on_mark(v2.Mark(1999, 0.40)))
        a = g.on_mark(v2.Mark(2000, 0.30))
        self.assertEqual(a.kind, 'EXIT')
        self.assertAlmostEqual(a.fraction_of_entry, 1.0)

    def test_positive_holder_and_buy_flow_earns_continuation(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1100, 'BUY', 'a', 1.0, 100, 0.01, vsol=30.5))
        ft.ingest(v21.FlowEvent(1450, 'BUY', 'b', 1.2, 100, 0.05, vsol=31.0))
        ft.ingest(v21.FlowEvent(1800, 'BUY', 'c', 1.5, 100, 0.11, vsol=32.0))
        score, reasons = v21.continuation_score(ft.snapshot(2000))
        self.assertGreaterEqual(score, 3)
        self.assertIn('NET_BUY_FLOW_POSITIVE', reasons)
        self.assertIn('BUYER_BREADTH', reasons)

    def test_creator_sell_is_heavily_penalised(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1400, 'BUY', 'a', 1.0, 100, 0.04))
        ft.ingest(v21.FlowEvent(1800, 'SELL', 'creator', 1.2, 100, -0.08, is_creator=True))
        score, reasons = v21.continuation_score(ft.snapshot(2000))
        self.assertIn('CREATOR_SELLING', reasons)
        self.assertLess(score, 0)

    def test_whale_concentration_penalty(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1200, 'BUY', 'whale', 9.0, 100, 0.05))
        ft.ingest(v21.FlowEvent(1500, 'BUY', 'small', 1.0, 100, 0.07))
        score, reasons = v21.continuation_score(ft.snapshot(2000))
        self.assertIn('WHALE_CONCENTRATION', reasons)

    def test_sell_pressure_fails_anchor(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1100, 'BUY', 'a', 0.2, 100, 0.02))
        ft.ingest(v21.FlowEvent(1500, 'SELL', 'a', 1.0, 100, -0.04))
        ft.ingest(v21.FlowEvent(1900, 'SELL', 'b', 1.0, 100, -0.10))
        g = v21.FlowAwareGuardian(v2.TIERS['HIGH'], ft)
        action = g.on_mark(v2.Mark(2000, -0.10))
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, 'EXIT')

    def test_deep_red_strong_flow_defers_initial(self):
        ft = v21.FlowTracker()
        for t, w in ((100, 'a'), (200, 'b'), (300, 'c'), (320, 'd')):
            ft.ingest(v21.FlowEvent(t, 'BUY', w, 1.0, 100, -0.06, vsol=31+t/1000))
        g = v21.FlowAwareGuardian(v2.TIERS['HIGH'], ft)
        self.assertIsNone(g.on_mark(v2.Mark(325, -0.06)))
        self.assertTrue(g.initial_deferred)
        a = g.on_mark(v2.Mark(800, 0.00))
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, 'INITIAL')

    def test_early_bad_flow_cuts_loss(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(300, 'SELL', 'a', 2.0, 100, -0.13))
        ft.ingest(v21.FlowEvent(450, 'SELL', 'b', 1.0, 100, -0.14))
        g = v21.FlowAwareGuardian(v2.TIERS['EXCEPTIONAL'], ft)
        a = g.on_mark(v2.Mark(550, -0.14))
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, 'EXIT')
        self.assertIn('EARLY_FLOW_INVALIDATION', a.reason)

    def test_profit_floor_prevents_full_giveback(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1500, 'BUY', 'a', 1.0, 100, 0.30))
        ft.ingest(v21.FlowEvent(1800, 'BUY', 'b', 1.0, 100, 0.30))
        g = v21.FlowAwareGuardian(v2.TIERS['HIGH'], ft)
        # First 2s decision earns continuation.
        self.assertIsNone(g.on_mark(v2.Mark(2000, 0.30)))
        # Tape deteriorates and price falls through the 30% peak floor.
        ft.ingest(v21.FlowEvent(2100, 'SELL', 'a', 1.0, 100, 0.12))
        ft.ingest(v21.FlowEvent(2200, 'SELL', 'b', 1.0, 100, 0.12))
        a = g.on_mark(v2.Mark(2300, 0.12))
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, 'EXIT')

    def test_cluster_adjustment_reduces_fake_diversity(self):
        book = v21.PriorEvidenceBook()
        for w in ('a','b','c','d'):
            book.wallet_seen[w] = 20
        for pair in (('a','b'),('a','c'),('a','d'),('b','c'),('b','d'),('c','d')):
            book.pair_seen[tuple(sorted(pair))] = 18
        p = book.cluster_profile(['a','b','c','d'])
        self.assertEqual(p['independent_groups'], 1)
        self.assertEqual(p['redundant_wallets'], 3)
        c = v21.enhanced_conviction({
            'prior_win_rate': .95, 'prior_appearances': 100,
            'buyers':['a','b','c','d'], 'creator':''
        }, book)
        self.assertNotEqual(c['conviction_tier'], 'EXCEPTIONAL')

    def test_good_creator_can_help_but_not_create_signal(self):
        book = v21.PriorEvidenceBook()
        book.creator_seen['c'] = 10
        book.creator_wins['c'] = 8
        c = v21.enhanced_conviction({
            'prior_win_rate': .70, 'prior_appearances': 10,
            'buyers':['a'], 'creator':'c'
        }, book)
        self.assertGreaterEqual(c['conviction_score'], 54)

    def test_views_are_optional_not_fabricated(self):
        ft = v21.FlowTracker()
        ft.ingest(v21.FlowEvent(1500, 'BUY', 'a', 1.0, 100, 0.05, views=None))
        self.assertIsNone(ft.snapshot(2000)['view_growth_1000ms'])


if __name__ == '__main__':
    unittest.main()
