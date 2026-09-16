from __future__ import annotations
import copy
import unittest
from golden_top4_axiom_guard import CampaignEngine, AxiomAccessBlocked, validate_observation, load_live_axiom_source
from golden_top4_engine import Config, ARMS
from test_golden_full3s_v2 import decision, state, T0


def observation(**changes):
    row = {'evidence_mode':'SYNTHETIC_FIXTURE','mint':'fixture-coin','origin':'wss://fixture.axiom.trade',
           'session_started_ns':T0-1_000_000_000,'source_event_ns':T0-1_000_000,
           'received_ns':T0-500_000,'market_state':'ACTIVE','tls_verified':True,
           'raw_sha256':'a'*64,'transport_session_id':'fixture-session','market_id':'fixture-market'}
    row.update(changes);return row


class AxiomGuardTests(unittest.TestCase):
    def setUp(self):
        CampaignEngine.output = None; CampaignEngine.smoke = False; CampaignEngine.require_reporter = False

    def test_live_source_unavailable_is_not_a_pass(self):
        with self.assertRaises(AxiomAccessBlocked): load_live_axiom_source()

    def test_live_entry_blocked_for_all_models_and_observer(self):
        e=CampaignEngine(Config())
        self.assertFalse(e.submit(decision(),state(),T0))
        self.assertEqual(set(e.accounts),set(ARMS))
        self.assertTrue(all(a['cash']==3 and not a['ledger'] for a in e.accounts.values()))
        self.assertTrue(all(not x.bundles for x in e.all_engines()))
        self.assertEqual(e.events[-1]['kind'],'ENTRY_VETO')
        self.assertEqual(e.matched_closed(),0)

    def test_user_supplied_verified_flag_does_not_unlock(self):
        e=CampaignEngine(Config());d=decision();d['axiom_verified']=True;d['axiom_observation']=observation()
        self.assertFalse(e.submit(d,state(),T0))
        self.assertFalse(e.bundles)

    def test_synthetic_observation_cannot_verify_real_market(self):
        with self.assertRaises(AxiomAccessBlocked): validate_observation(observation(),'fixture-coin',T0)

    def test_missing_and_html_success_not_market_evidence(self):
        for row in (None,{}, {'status':200,'axiom_verified':True}):
            with self.subTest(row=row),self.assertRaises(AxiomAccessBlocked):
                validate_observation(row,'fixture-coin',T0)

    def test_valid_fixture_schema_only(self):
        self.assertEqual(validate_observation(observation(),'fixture-coin',T0,fixture_mode=True)['mint'],'fixture-coin')

    def test_wrong_coin_refused(self):
        with self.assertRaises(AxiomAccessBlocked):validate_observation(observation(),'other',T0,fixture_mode=True)

    def test_nonfirstparty_or_insecure_origins(self):
        for origin in ('https://axiom.trade.evil.test','https://evilaxiom.trade','http://axiom.trade','wss://axiom.trade@evil.test','https://axiom.trade:444','https://axiom.trade/?token=secret'):
            with self.subTest(origin=origin),self.assertRaises(AxiomAccessBlocked):
                validate_observation(observation(origin=origin),'fixture-coin',T0,fixture_mode=True)

    def test_invalid_receipt_hash(self):
        for value in ('', 'a'*63, 'Z'*64, None):
            with self.subTest(value=value),self.assertRaises(AxiomAccessBlocked):
                validate_observation(observation(raw_sha256=value),'fixture-coin',T0,fixture_mode=True)

    def test_future_and_regressed_times(self):
        for change in ({'source_event_ns':T0+1},{'received_ns':T0+1},{'source_event_ns':T0-2_000_000_000},{'received_ns':True}):
            with self.subTest(change=change),self.assertRaises(AxiomAccessBlocked):
                validate_observation(observation(**change),'fixture-coin',T0,fixture_mode=True)

    def test_old_source_event_is_not_refreshed_by_new_receive_time(self):
        with self.assertRaises(AxiomAccessBlocked):
            validate_observation(observation(source_event_ns=T0-501_000_000,received_ns=T0),'fixture-coin',T0,fixture_mode=True)

    def test_no_tls_or_inactive_market(self):
        for change in ({'tls_verified':False},{'market_state':'UNKNOWN'},{'market_id':''},{'transport_session_id':''}):
            with self.subTest(change=change),self.assertRaises(AxiomAccessBlocked):
                validate_observation(observation(**change),'fixture-coin',T0,fixture_mode=True)

    def test_fixture_trade_does_not_enter_live_evidence_count(self):
        e=CampaignEngine(Config(),fixture_mode=True)
        self.assertTrue(e.submit(decision(),state(),T0))
        for dt in range(0,32_001,250):
            ns=T0+dt*1_000_000;e.tick(ns,{'fixture-coin':state(ns,33 if dt>=500 else 30,9e8 if dt>=500 else 1e9)},ns)
        self.assertTrue(all(a['ledger'] for a in e.accounts.values()))
        self.assertTrue(all(n==0 for n in e.forward_counts().values()))
        e.validate()

    def test_entry_gate_does_not_disable_inherited_exit_management(self):
        e=CampaignEngine(Config(),fixture_mode=True);e.submit(decision(),state(),T0)
        ns=T0+250_000_000;e.tick(ns,{'fixture-coin':state(ns)},ns)
        # Source is unavailable throughout this fixture; existing inventory must
        # still close, not be abandoned because new admissions are blocked.
        for dt in range(500,32_001,250):
            ns=T0+dt*1_000_000;e.tick(ns,{'fixture-coin':state(ns,33,9e8)},ns)
        self.assertFalse(e.bundles)
        self.assertTrue(all(a['rent_locked']==0 for a in e.accounts.values()))

    def test_fixture_restart_keeps_receipts_and_costs(self):
        e=CampaignEngine(Config(),fixture_mode=True)
        saved=e.persistence();r=CampaignEngine(Config(),saved,fixture_mode=True)
        self.assertTrue(r.persistence()['axiom_requirement_enforced'])
        self.assertTrue(all(a['cash']==3 for a in r.accounts.values()))

    def test_thousand_unverified_candidates_never_become_trades(self):
        e=CampaignEngine(Config())
        for i in range(1000):
            self.assertFalse(e.submit(decision(mint=f'unverified-{i}'),state(),T0))
        self.assertEqual(len(e.events),1000)
        self.assertTrue(all(a['cash']==3 and not a['ledger'] for a in e.accounts.values()))
        self.assertEqual(e.matched_closed(),0)

if __name__ == '__main__': unittest.main()
