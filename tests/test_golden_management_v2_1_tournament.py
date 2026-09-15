import asyncio
import unittest
from unittest.mock import patch

from tools import golden_management_v2_1 as v21
from tools import golden_management_v2_1_tournament_live as base
from tools import golden_management_v2_1_tournament_hardened as hard


STATE = {'ns': 123, 'complete': False, 'vsol': 40.0, 'vtok': 800_000_000.0, 'rtok': 700_000_000.0}
DECISION = {
    'mint': 'TESTpump', 'create_ns': 1_000_000_000, 'decision_ns': 1_010_000_000,
    'buyers': ['a','b'], 'buyer_count': 2, 'prior_appearances': 20,
    'prior_wins': 16, 'prior_win_rate': .8, 'creator': 'creator', 'early_vsol_delta': .4,
}


class TournamentAtomicityTests(unittest.TestCase):
    def make(self):
        t = base.Tournament(v21.PriorEvidenceBook())
        async def no_manage(mint, states):
            return None
        t._manage = no_manage
        return t

    def test_all_four_arms_enter_atomically(self):
        t = self.make()
        asyncio.run(hard._atomic_enter(t, 'TESTpump', dict(DECISION), {'TESTpump': dict(STATE)}, 0))
        self.assertIn('TESTpump', t.active)
        self.assertEqual(set(t.active['TESTpump']['positions']), set(v21.TOURNAMENT_ARMS))
        for name in v21.TOURNAMENT_ARMS:
            self.assertLess(t.accounts[name].cash, 3.0)
            self.assertGreater(t.active['TESTpump']['positions'][name]['stake_sol'], 0)

    def test_partial_prepare_failure_mutates_no_account(self):
        t = self.make()
        original = hard.core.quote_buy
        calls = {'n': 0}
        def flaky(stake, state):
            calls['n'] += 1
            if calls['n'] == 3:
                return 0.0, 0.0
            return original(stake, state)
        with patch.object(hard.core, 'quote_buy', side_effect=flaky):
            asyncio.run(hard._atomic_enter(t, 'TESTpump', dict(DECISION), {'TESTpump': dict(STATE)}, 0))
        self.assertNotIn('TESTpump', t.active)
        for name in v21.TOURNAMENT_ARMS:
            self.assertAlmostEqual(t.accounts[name].cash, 3.0)

    def test_finish_records_same_mint_for_all_arms(self):
        t = self.make()
        asyncio.run(hard._atomic_enter(t, 'TESTpump', dict(DECISION), {'TESTpump': dict(STATE)}, 0))
        bundle = t.active['TESTpump']
        for p in bundle['positions'].values():
            p['status'] = 'CLOSED'; p['pnl_sol'] = 0.01; p['return_fraction'] = .02
            p['proceeds_sol'] = p['stake_sol'] + .01; p['realized_proceeds_sol'] = p['proceeds_sol']
            p['remaining_tokens'] = 0.0; p['mark_remaining_value_sol'] = 0.0
            p['post_exit'] = {'complete': False, 'exit_full_position_return': .02, 'max_return_fraction': None,
                              'min_return_fraction': None, 'went_higher_than_exit': False, 'went_lower_than_exit': False,
                              'window_seconds': 20.0}
        hard._equity_correct_finish(t, bundle, STATE)
        self.assertNotIn('TESTpump', t.active)
        for name in v21.TOURNAMENT_ARMS:
            self.assertEqual(t.accounts[name].ledger[-1]['mint'], 'TESTpump')
            self.assertIn('balance_after_sol', t.accounts[name].ledger[-1])


if __name__ == '__main__':
    unittest.main()
