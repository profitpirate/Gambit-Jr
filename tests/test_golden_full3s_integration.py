from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from test_golden_full3s_v2 import T0, state, decision, outcome, settled
from golden_full3s_v2 import Runtime, SingleEngine, Config, base, ARM
from golden_full3s_policy import EvidenceIndex, Selector, PolicyConfig
from golden_full3s_service import PaperService

class IntegrationTests(unittest.TestCase):
    def test_durable_end_to_end_post_exit_alias_and_reputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=PaperService(Path(tmp)/'state.db',Runtime(fixture_mode=True))
            service.process('submit',{'kind':'SUBMIT','decision':decision(),'state':state(),'now':T0})
            for dt in range(0,25001,250):
                ns=T0+dt*1_000_000
                event={'kind':'TICK','states':{'fixture-coin':state(ns,33 if dt>=500 else 30,9e8 if dt>=500 else 1e9)},'now':ns,'last_feed_ns':ns}
                service.process(str(dt),event)
            r=service.runtime()
            self.assertEqual(r.candidate.matched_closed(),1)
            self.assertTrue(r.candidate.accounts[ARM]['ledger'][0]['post']['complete'])
            self.assertEqual(r.candidate.forward_count(),1)
            self.assertEqual(len(r.evidence.rows),1)
            self.assertEqual(r.label_cursor,1)
            self.assertEqual(r.summary()['candidate_fully_observed_closed'],0)
            self.assertFalse(r.candidate.bundles)
            service.store.verify();service.close()

    def test_true_service_restart_marks_interruption_and_prevents_new_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db';s=PaperService(path,Runtime(fixture_mode=True))
            s.process('submit',{'kind':'SUBMIT','decision':decision(),'state':state(),'now':T0})
            ns=T0+250_000_000;s.process('entry',{'kind':'TICK','states':{'fixture-coin':state(ns)},'now':ns,'last_feed_ns':ns})
            before=s.runtime().candidate.bundles['fixture-coin']['positions'][ARM]['remaining']
            s.close();s=PaperService(path);r=s.runtime()
            self.assertTrue(r.interrupted);self.assertTrue(r.halt_new_entries)
            self.assertEqual(r.candidate.bundles['fixture-coin']['positions'][ARM]['remaining'],before)
            s.close()

    def test_same_economics_as_original_full3s_on_clean_paths(self):
        import golden_horizon_engine as original
        for i in range(60):
            old=original.Engine(original.Config());new=SingleEngine()
            d=decision(tier=list(base.TIERS)[i%4]);old.submit(d,state(),T0);new.submit(d,state(),T0)
            x=30*(.60+i*.015);y=1e9/(.60+i*.015)
            for dt in range(0,35001,250):
                ns=T0+dt*1_000_000;states={'fixture-coin':state(ns,x if dt>=500 else 30,y if dt>=500 else 1e9)}
                old.tick(ns,states,ns);new.tick(ns,states,ns)
            self.assertAlmostEqual(old.accounts[ARM]['cash'],new.accounts[ARM]['cash'],places=8)
            self.assertEqual(old.accounts[ARM]['ledger'][0]['actions'],new.accounts[ARM]['ledger'][0]['actions'])

    def test_label_cursor_does_not_reingest_settled_coins(self):
        r=settled();before=r.evidence.generation
        for dt in range(26000,27000,250):
            ns=T0+dt*1_000_000;r.tick(ns,{},ns)
        self.assertEqual(r.evidence.generation,before)
        self.assertEqual(len(r.evidence.rows),1)

    def test_fixture_mode_snapshot_tampering_rejected(self):
        r=settled();snap=r.snapshot();snap['fixture_mode']=False
        with self.assertRaises(ValueError):Runtime.restore(snap)

    def test_policy_snapshot_mismatch_rejected(self):
        r=settled();snap=r.snapshot();snap['policy']['max_impact_bps']=600
        with self.assertRaises(ValueError):Runtime.restore(snap)

    def test_candidate_veto_still_observes_and_learns_rejected_signal(self):
        r=Runtime(fixture_mode=True)
        for i in range(25):r.evidence.add(outcome(i,net=-.02,budget=.25))
        a=r.submit(decision(),state(),T0)
        self.assertFalse(a['candidate_submitted']);self.assertTrue(a['observer_submitted'])
        for dt in range(0,25001,250):
            ns=T0+dt*1_000_000;r.tick(ns,{'fixture-coin':state(ns,33,9e8)},ns)
        self.assertEqual(r.candidate.matched_closed(),0)
        self.assertEqual(r.observer.matched_closed(),1)
        self.assertIn('fixture-coin',r.evidence.rows)

    def test_insufficient_retry_reserve_rejects_without_fee(self):
        e=SingleEngine();e.accounts[ARM]['cash']=.035
        self.assertFalse(e.submit(decision(),state(),T0))
        self.assertEqual(e.accounts[ARM]['cash'],.035)

    def test_quoted_inventory_change_blocks_fictional_fill(self):
        e=SingleEngine();e.submit(decision(),state(),T0)
        ns=T0+250_000_000;quote=state(ns);quote['rtok']=1.0
        e.tick(ns,{'fixture-coin':quote},ns)
        self.assertTrue(e.errors)
        self.assertEqual(e.accounts[ARM]['cash'],3.0)
        self.assertEqual(e.matched_closed(),0)

if __name__=='__main__':unittest.main()
