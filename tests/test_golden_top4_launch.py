from __future__ import annotations
import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from golden_top4_operations import CampaignEngine,OriginalEngine,ARMS,Config
from golden_top4_report import analyse,validate_state
from golden_top4_e4 import cycle_audit,parse_transaction,WALLET
from test_golden_full3s_v2 import T0,state,decision,outcome


def completed(e=None,now=T0,mint='fixture-coin',x=33,y=9e8,proof=False):
    e=e or CampaignEngine(Config(),fixture_mode=True)
    d=decision(mint=mint,now=now);d.update(name='Fixture',symbol='TEST',session_start_ns=now-1_000_000_000)
    assert e.submit(d,state(now),now)
    if proof:
        e.proofs[mint]={'verified':True,'mint':mint,'signature':d['create_signature'],'block_time':now//1_000_000_000}
    for dt in range(0,32_001,250):
        n=now+dt*1_000_000;e.tick(n,{mint:state(n,x if dt>=500 else 30,y if dt>=500 else 1e9)},n)
    return e

class LaunchTests(unittest.TestCase):
    def setUp(self):
        for cls in (CampaignEngine,OriginalEngine):
            cls.output=None;cls.smoke=False;cls.require_reporter=False

    def test_four_accounts_start_at_three(self):
        e=CampaignEngine(Config(),fixture_mode=True)
        self.assertEqual(set(e.accounts),set(ARMS));self.assertTrue(all(a['cash']==3 for a in e.accounts.values()))

    def test_full_horizons_preserved_without_partials(self):
        e=completed()
        for arm,ms in zip(ARMS,(3000,4000,7000,3000)):
            p=e.accounts[arm]['ledger'][0];self.assertEqual(p['horizon_ms'],ms)
            self.assertEqual([a['side'] for a in p['actions']],['BUY','SELL'])
            self.assertEqual(p['hold_ms'],ms+250)
        e.validate()

    def test_every_attempt_cost_reported(self):
        e=completed()
        events=[r for r in e.events if r['kind']=='TRANSACTION_ATTEMPT']
        self.assertEqual(len(events),8)
        for r in events:
            self.assertIn('win_rate',r);self.assertIn('account_equity_sol',r)
            self.assertIsNone(r['actual_profit_sol']);self.assertIsNone(r['actual_fees_sol'])
            self.assertAlmostEqual(r['action_fees_sol'],sum(r['action']['fees'].values()))
        self.assertEqual(len([r for r in e.events if r['kind']=='TRANSACTION_INTENT']),8)

    def test_winners_and_losers_cash_reconcile(self):
        for x,y in ((33,9e8),(25,12e8)):
            e=completed(x=x,y=y)
            for a in e.accounts.values():
                p=a['ledger'][0];self.assertAlmostEqual(a['cash'],3+p['net_pnl_sol'])
                self.assertAlmostEqual(p['net_pnl_sol'],p['gross_pnl_sol']-sum(p['fees'].values()))
            e.validate()

    def test_no_fixture_count_is_live(self):
        e=completed(proof=True)
        self.assertTrue(all(n==0 for n in e.forward_counts().values()))

    def test_missing_launch_proof_not_counted(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False))
        self.assertTrue(all(n==0 for n in e.forward_counts().values()))

    def test_direct_live_schema_counts_without_axiom_flag(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False),proof=True)
        # Local artificial paths validate gating logic only, never production evidence.
        self.assertTrue(all(n==1 for n in e.forward_counts().values()))
        self.assertTrue(all(r['axiom_listing_verified'] is None for r in e.events))

    def test_no_post_ticks_unknown_not_certified(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False),proof=True)
        e.accounts['FULL_3S']['ledger'][0]['post']['coverage']='NO_FURTHER_TICKS_OBSERVED'
        self.assertEqual(e.forward_counts()['FULL_3S'],0)

    def test_incident_uncertain_excluded_not_erased(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False),proof=True)
        cash=e.accounts['FULL_3S']['cash'];e.control_uncertain.add('fixture-coin')
        self.assertEqual(e.forward_counts()['FULL_3S'],0);self.assertEqual(e.accounts['FULL_3S']['cash'],cash)

    def test_clean_restart_retains_all_costs_and_no_duplicate_events(self):
        e=completed();raw=json.loads(json.dumps(e.persistence()));r=CampaignEngine(Config(),raw,fixture_mode=True)
        n=len(r.events);r.emit_intents();r.emit_actions();self.assertEqual(len(r.events),n)
        self.assertEqual(r.accounts,e.accounts);r.validate()

    def test_open_or_failed_restart_refused(self):
        e=CampaignEngine(Config(),fixture_mode=True);e.submit(decision(),state(),T0)
        with self.assertRaises(ValueError):CampaignEngine(Config(),e.persistence(),fixture_mode=True)

    def test_v2_can_reject_without_suppressing_controls(self):
        e=CampaignEngine(Config(),fixture_mode=True)
        for i in range(30):e.runtime.evidence.add(outcome(i,net=-.03))
        completed(e)
        self.assertTrue(all(e.accounts[a]['ledger'] for a in ARMS[:3]))
        self.assertFalse(e.accounts['FULL_3S_V2']['ledger'])
        result=analyse(e);self.assertGreater(result['v2_rejected_signals']['count'],0)

    def test_reporting_failure_blocks_entries_not_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cls in (CampaignEngine,OriginalEngine):cls.output=Path(tmp);cls.require_reporter=True
            e=CampaignEngine(Config(),fixture_mode=True)
            self.assertFalse(e.submit(decision(),state(),T0))
            for cls in (CampaignEngine,OriginalEngine):cls.require_reporter=False
            e=completed();self.assertTrue(all(a['ledger'] for a in e.accounts.values()))

    def test_duplicate_mint_no_second_entry(self):
        e=completed();self.assertFalse(e.submit(decision(),state(),T0));self.assertTrue(all(len(a['ledger'])==1 for a in e.accounts.values()))

    def test_failed_attempts_are_paid_and_reported(self):
        e=CampaignEngine(Config(),fixture_mode=True);e.submit(decision(),state(),T0)
        n=T0+250_000_000;e.tick(n,{'fixture-coin':state(n,60,5e8)},n)
        events=[r for r in e.events if r.get('action',{}).get('outcome')=='MODELLED_LANDED_FAILURE']
        self.assertEqual(len(events),4)
        self.assertTrue(all(a['cash']<3 for a in e.accounts.values()))
        for r in events:self.assertAlmostEqual(r['action_fees_sol'],.001005)

    def test_full_gate_rejects_less_than_100(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False),proof=True)
        s={'market_source':'DIRECT_SOLANA_NEW_CREATIONS','engine':e.persistence(),'sessions':[{'errors':[],'historical_replay':False}]}
        validate_state(s,require_target=False)
        with self.assertRaises(ValueError):validate_state(s)

    def test_event_loss_is_detected(self):
        e=completed(CampaignEngine(Config(),fixture_mode=False),proof=True)
        e.events=[r for r in e.events if r.get('action_number')!=2]
        s={'market_source':'DIRECT_SOLANA_NEW_CREATIONS','engine':e.persistence(),'sessions':[{'errors':[],'historical_replay':False}]}
        with self.assertRaises(ValueError):validate_state(s,require_target=False)

    def test_e4_fees_deducted_once(self):
        rows=[]
        for kind,pre,post,cash in [('TRADE_BUY',0,100,-1.01),('TRADE_SELL',100,0,1.21)]:
            rows.append({'kind':kind,'mint':'m','token_deltas':{'m':{'pre_raw':pre,'post_raw':post}},'wallet_liquid_cashflow_sol':cash,'actual_chain_fee_paid_by_e4_sol':.01,'signature':kind})
        rows.append({'kind':'FAILED_TRANSACTION','actual_chain_fee_paid_by_e4_sol':.02})
        s=cycle_audit(rows);self.assertAlmostEqual(s['closed_cycle_net_cashflow_sol'],.20)
        self.assertAlmostEqual(s['closed_cycles_minus_all_failed_fees_sol'],.18)

    def test_100_synthetic_stress_paths_do_not_claim_live(self):
        for i in range(100):
            e=completed(now=T0+i*40_000_000_000,mint=str(i),x=25+i%15,y=8e8+i%5*1e8)
            e.validate();self.assertEqual(e.matched_closed(),0)

if __name__=='__main__':unittest.main()
