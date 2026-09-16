from __future__ import annotations
import copy
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from golden_full3s_v2 import Runtime, SingleEngine, Config, base, ARM, quote_metrics, valid_quote
from golden_full3s_policy import PolicyConfig, Outcome, EvidenceIndex, Selector, clean_buyers, cohort_key
from golden_full3s_store import Store
from golden_full3s_diagnostics import classify_loss, filter_tradeoff, evidence_gate

T0 = 100_000_000_000


def state(ns=T0, x=30.0, y=1_000_000_000.0, **extra):
    return dict(ns=ns, vsol=x, vtok=y, rtok=800_000_000.0, slot=int(ns//400_000_000), signature=f"fixture-{ns}", complete=False, **extra)


def decision(mint="fixture-coin", now=T0, tier="MEDIUM"):
    return {"mint": mint, "create_ns": now-10_000_000, "decision_ns": now,
            "create_signature": "synthetic-test-only", "creator": "creator",
            "buyers": ["buyer-a", "buyer-b"], "prior_appearances": 20, "prior_win_rate": .80,
            "tier": tier}


def settled(runtime=None, *, x=33.0, y=900_000_000.0, now=T0):
    r=runtime or Runtime(fixture_mode=True)
    r.submit(decision(now=now), state(now), now)
    for dt in range(0, 25_001, 250):
        ns=now+dt*1_000_000
        r.tick(ns, {"fixture-coin": state(ns, x if dt>=500 else 30, y if dt>=500 else 1e9)}, ns)
    return r


def outcome(i, net=.03, budget=.25, *, available=None, buyers=("buyer-a", "buyer-b"), creator="creator", cfg=None):
    c=cfg or Config()
    a=available if available is not None else (i+1)*1_000_000
    return Outcome(f"history-{i}", tuple(sorted(buyers)), creator, a-4000, a-2000, a,
                   budget, net, net+.01, c.fingerprint(), "SYNTHETIC_FIXTURE")


class LifecycleTests(unittest.TestCase):
    def test_private_arm_does_not_mutate_original_tournament(self):
        import golden_horizon_engine as original
        self.assertEqual(len(original.ARMS),12)
        self.assertEqual(base.ARMS,("FULL_3S",))

    def test_no_partials_and_three_seconds_preserved(self):
        r=settled(); p=r.candidate.accounts[ARM]["ledger"][0]
        acts=[a for a in p["actions"] if a["outcome"]=="MODELLED_FILL"]
        self.assertEqual([a["side"] for a in acts],["BUY","SELL"])
        self.assertEqual(p["horizon_ms"],3000)
        self.assertEqual(p["hold_ms"],3250)
        self.assertEqual(p["remaining"],0)
        self.assertEqual(p["initial_fraction"],0)
        self.assertTrue(p["post"]["complete"])

    def test_costs_and_account_reconcile(self):
        r=settled(); a=r.candidate.accounts[ARM]; p=a["ledger"][0]
        self.assertAlmostEqual(a["cash"],3+p["net_pnl_sol"],places=8)
        self.assertAlmostEqual(p["net_pnl_sol"],p["gross_pnl_sol"]-sum(p["fees"].values()),places=8)
        self.assertEqual(a["rent_locked"],0)
        self.assertEqual(p["fees"]["priority_sol"],.002)
        self.assertEqual(p["fees"]["tip_sol"],.002)
        r.candidate.validate()

    def test_fixture_has_no_actual_profit_or_live_count(self):
        r=settled(); s=r.summary()
        self.assertIsNone(s["actual_profit_sol"])
        self.assertIsNone(s["actual_failed_transactions"])
        self.assertEqual(s["candidate_fully_observed_closed"],0)
        self.assertFalse(evidence_gate(r)["complete"])
        with self.assertRaises(ValueError): evidence_gate(r,1)

    def test_duplicate_decision(self):
        r=Runtime(fixture_mode=True)
        r.submit(decision(),state(),T0)
        ans=r.submit(decision(),state(),T0)
        self.assertIn("DUPLICATE_DECISION",ans["vetoes"])
        self.assertEqual(len(r.candidate.bundles),1)

    def test_concurrency_and_reserved_budgets(self):
        e=SingleEngine()
        for i in range(2): self.assertTrue(e.submit(decision(str(i),tier="EXCEPTIONAL"),state(),T0))
        self.assertFalse(e.submit(decision("third"),state(),T0))
        self.assertEqual(e.rejections[-1]["reason"],"CONCURRENCY")
        self.assertEqual(e.accounts[ARM]["cash"],3)

    def test_future_or_stale_quote(self):
        for ns in (T0+1,T0-501_000_000):
            e=SingleEngine(); self.assertFalse(e.submit(decision(),state(ns),T0))
        self.assertFalse(valid_quote(state(x=float("nan")),T0,500))
        self.assertFalse(valid_quote(state(y=float("inf")),T0,500))

    def test_stale_open_quote_cannot_manufacture_exit(self):
        e=SingleEngine(); e.submit(decision(),state(),T0)
        e.tick(T0+250_000_000,{"fixture-coin":state(T0+250_000_000)},T0+250_000_000)
        e.tick(T0+4_000_000_000,{"fixture-coin":state(T0+250_000_000)},T0+4_000_000_000)
        self.assertEqual(e.bundles["fixture-coin"]["positions"][ARM]["status"],"OPEN")
        self.assertIn("fixture-coin",e.uncertain_mints)
        self.assertEqual(e.matched_closed(),0)

    def test_feed_loss_prevents_fills(self):
        e=SingleEngine(); e.submit(decision(),state(),T0)
        e.tick(T0+250_000_000,{"fixture-coin":state(T0+250_000_000)},T0-4_000_000_000)
        self.assertEqual(e.bundles["fixture-coin"]["positions"][ARM]["status"],"PENDING")
        self.assertEqual(e.accounts[ARM]["cash"],3)

    def test_pending_entry_expires_without_erasing_costs(self):
        e=SingleEngine(); e.submit(decision(),state(),T0)
        e.tick(T0+4_000_000_000,{"fixture-coin":state()},T0+4_000_000_000)
        self.assertFalse(e.bundles)
        self.assertEqual(e.matched_closed(),0)
        self.assertEqual(len(e.accounts[ARM]["aborted_entries"]),1)
        e.validate()

    def test_migration_retains_inventory(self):
        e=SingleEngine(); e.submit(decision(),state(),T0)
        ns=T0+250_000_000;e.tick(ns,{"fixture-coin":state(ns)},ns)
        ns+=250_000_000;s=state(ns);s["complete"]=True
        e.tick(ns,{"fixture-coin":s},ns)
        self.assertTrue(e.bundles)
        self.assertGreater(e.bundles["fixture-coin"]["positions"][ARM]["remaining"],0)
        self.assertTrue(e.errors);self.assertFalse(e.accepting)

    def test_slippage_failure_fee_and_retry(self):
        e=SingleEngine();e.submit(decision(),state(),T0)
        ns=T0+250_000_000;e.tick(ns,{"fixture-coin":state(ns,60,5e8)},ns)
        p=e.bundles["fixture-coin"]["positions"][ARM]
        self.assertEqual(p["status"],"PENDING")
        self.assertEqual(p["actions"][0]["outcome"],"MODELLED_LANDED_FAILURE")
        self.assertEqual(p["actions"][0]["fees"]["tip_sol"],0)
        self.assertAlmostEqual(e.accounts[ARM]["cash"],3-.001005)
        ns+=250_000_000;e.tick(ns,{"fixture-coin":state(ns,60,5e8)},ns)
        self.assertEqual(p["status"],"OPEN")
        self.assertEqual(p["entry_attempts"],2)

    def test_out_of_order_cannot_reverse_quote(self):
        e=SingleEngine();e.submit(decision(),state(),T0)
        ns=T0+250_000_000;e.tick(ns,{"fixture-coin":state(ns)},ns)
        e.tick(ns+1,{"fixture-coin":state(T0,300,1e7)},ns+1)
        self.assertEqual(e.input_rejections,1)
        self.assertEqual(e._view["fixture-coin"]["vsol"],30)

    def test_clock_regression_closes_entry_gate(self):
        r=Runtime(fixture_mode=True);r.tick(T0,{},T0)
        with self.assertRaises(ValueError): r.tick(T0-1,{},T0-1)
        self.assertTrue(r.halt_new_entries)

    def test_backlog_blocks_new_entries(self):
        r=Runtime(fixture_mode=True)
        ans=r.submit(decision(),state(),T0,queue_depth=1025)
        self.assertFalse(ans["accept"]);self.assertFalse(r.candidate.bundles)

    def test_open_restart_preserves_inventory_and_invalidates_forward_count(self):
        r=Runtime(fixture_mode=True);r.submit(decision(),state(),T0)
        ns=T0+250_000_000;r.tick(ns,{"fixture-coin":state(ns)},ns)
        old=r.candidate.bundles["fixture-coin"]["positions"][ARM]["remaining"]
        restored=Runtime.restore(json.loads(json.dumps(r.snapshot())))
        self.assertEqual(restored.candidate.bundles["fixture-coin"]["positions"][ARM]["remaining"],old)
        self.assertTrue(restored.halt_new_entries);self.assertTrue(restored.interrupted)
        self.assertIn("fixture-coin",restored.candidate.uncertain_mints)

    def test_clean_roundtrip_snapshot(self):
        r=settled();p=Runtime.restore(json.loads(json.dumps(r.snapshot())))
        self.assertAlmostEqual(p.candidate.accounts[ARM]["cash"],r.candidate.accounts[ARM]["cash"])
        self.assertFalse(p.interrupted)
        self.assertEqual(len(p.evidence.rows),1)

    def test_cost_change_on_restart_rejected(self):
        r=settled();snap=r.snapshot();snap["config"]["priority_sol"]=.01
        with self.assertRaises(ValueError): Runtime.restore(snap)

    def test_all_fraction_tiers_retained(self):
        for tier, fraction in (("BASE",.05),("MEDIUM",.08),("HIGH",.15),("EXCEPTIONAL",.25)):
            e=SingleEngine();e.submit(decision(tier=tier),state(),T0)
            p=e.bundles["fixture-coin"]["positions"][ARM]
            self.assertAlmostEqual(p["budget"],3*fraction)

    def test_quote_metrics_include_fixed_and_percent_cost(self):
        q=quote_metrics(Config(),state(),.24)
        self.assertTrue(q["supported"])
        self.assertGreater(q["roundtrip_drag_bps"],400)
        self.assertGreater(q["impact_bps"],0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.c=Config();self.e=EvidenceIndex(self.c.fingerprint(),fixture_mode=True)
        self.sel=Selector(self.e)
        self.q={"supported":True,"ns":T0,"impact_bps":40,"roundtrip_drag_bps":550}

    def test_cold_start_is_explicit_not_fake_calibration(self):
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertTrue(a["accept"])
        self.assertIn("NET_REPUTATION_WARMUP_FROZEN_GOLDEN_FALLBACK",a["notes"])
        self.assertEqual(a["pooled_unique_coin_stats"]["n"],0)

    def test_wallet_pool_deduplicates_same_coins(self):
        for i in range(25):self.e.add(outcome(i))
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertEqual(a["pooled_unique_coin_stats"]["n"],25)
        self.assertTrue(a["accept"])

    def test_fee_erased_reputation_rejects(self):
        for i in range(25):self.e.add(outcome(i,net=-.008))
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertFalse(a["accept"])
        self.assertIn("INSUFFICIENT_COST_ALIGNED_NET_EDGE",a["vetoes"])

    def test_creator_negative_expectancy_veto(self):
        for i in range(12):self.e.add(outcome(i,net=-.06,buyers=(f"other-{i}",)))
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertIn("CREATOR_NEGATIVE_EXPECTANCY",a["vetoes"])
        self.assertIn("CREATOR_EXCESSIVE_TAIL_LOSSES",a["vetoes"])

    def test_future_label_not_visible(self):
        for i in range(25):self.e.add(outcome(i,net=-.02,available=T0+i+1))
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertTrue(a["accept"]);self.assertEqual(a["pooled_unique_coin_stats"]["n"],0)

    def test_same_timestamp_label_not_visible(self):
        self.e.add(outcome(1,available=T0))
        self.assertEqual(self.sel.evaluate(decision(),self.q,.25,T0)["pooled_unique_coin_stats"]["n"],0)

    def test_wrong_cost_horizon_and_recovered_labels_rejected(self):
        for change in ({"horizon_ms":2000},{"cost_hash":"old-costs"},{"recovered":True},{"source":"OLD_CORPUS"},{"family":"PARTIALS"}):
            with self.assertRaises(ValueError):self.e.add(replace(outcome(0),**change))

    def test_fixture_cannot_seed_forward_index(self):
        e=EvidenceIndex(self.c.fingerprint())
        with self.assertRaises(ValueError):e.add(outcome(0))

    def test_nonfinite_label_rejected(self):
        for val in (float("nan"),float("inf"),float("-inf")):
            with self.assertRaises(ValueError): self.e.add(replace(outcome(0),net_pnl_sol=val))

    def test_same_coin_in_conflicting_groups_rejected(self):
        self.e.add(outcome(0))
        with self.assertRaises(ValueError): self.e.add(replace(outcome(0),buyers=("other",)))

    def test_different_size_not_falsely_calibrated(self):
        for i in range(25):self.e.add(outcome(i,net=-.02,budget=.05))
        a=self.sel.evaluate(decision(),self.q,.25,T0)
        self.assertEqual(a["pooled_unique_coin_stats"]["n"],0)

    def test_input_quote_vetoes(self):
        for key,val,expected in (("impact_bps",400,"EXCESSIVE_IMPACT_BPS"),("roundtrip_drag_bps",1600,"EXCESSIVE_ROUNDTRIP_DRAG_BPS"),("supported",False,"UNSUPPORTED_EXECUTABLE_ROUTE"),("ns",T0-501_000_000,"STALE_COIN_QUOTE")):
            q=dict(self.q);q[key]=val
            self.assertIn(expected,self.sel.evaluate(decision(),q,.25,T0)["vetoes"])

    def test_no_e4_or_creator_votes(self):
        from golden_full3s_policy import E4
        d=decision();d["buyers"]=["buyer-a","buyer-a",E4,"creator"]
        self.assertEqual(clean_buyers(d),("buyer-a",))

    def test_history_prunes_without_broken_references(self):
        e=EvidenceIndex(self.c.fingerprint(),PolicyConfig(max_history_per_key=10),fixture_mode=True)
        for i in range(40):e.add(outcome(i))
        self.assertEqual(len(e.rows),10)
        e.prune(10*86_400_000_000_000)
        self.assertEqual(len(e.rows),0)
        self.assertEqual(len(e.index),0)

    def test_snapshot_reproduces_stats(self):
        for i in range(25):self.e.add(outcome(i))
        e=EvidenceIndex.restore(json.loads(json.dumps(self.e.snapshot())))
        self.assertEqual(self.e.stats("buyer",("buyer-a",),.25,T0),e.stats("buyer",("buyer-a",),.25,T0))

    def test_bad_golden_rule_rejects(self):
        for wr in (.69, float("nan"),1.1):
            d=decision();d["prior_win_rate"]=wr
            self.assertFalse(self.sel.evaluate(d,self.q,.25,T0)["accept"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/"paper.db"
        self.s=Store(self.path,{"cash":3.0,"tokens":0})
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def test_idempotent_write(self):
        fn=lambda s:{"cash":2.5,"tokens":100}
        self.assertTrue(self.s.apply("buy",{"side":"BUY"},fn))
        self.assertFalse(self.s.apply("buy",{"side":"BUY"},fn))
        self.assertEqual(self.s.verify()["events"],1)
    def test_conflicting_key_rejected(self):
        self.s.apply("buy",{"side":"BUY"},lambda s:s)
        with self.assertRaises(ValueError):self.s.apply("buy",{"side":"SELL"},lambda s:s)
    def test_precommit_fault_rolls_back(self):
        def fail(where):
            if where=="BEFORE_COMMIT":raise RuntimeError("fault")
        with self.assertRaises(RuntimeError):self.s.apply("buy",{},lambda s:{"cash":2,"tokens":1},fault=fail)
        self.assertEqual(self.s.read()["cash"],3)
        self.assertEqual(self.s.verify()["events"],0)
    def test_postcommit_fault_remains_committed(self):
        def fail(where):
            if where=="AFTER_COMMIT":raise RuntimeError("lost acknowledgement")
        with self.assertRaises(RuntimeError):self.s.apply("buy",{},lambda s:{"cash":2,"tokens":1},fault=fail)
        self.assertEqual(self.s.read()["tokens"],1)
        self.assertFalse(self.s.apply("buy",{},lambda s:s))
    def test_corruption_detected(self):
        self.s.db.execute("UPDATE state SET body='{}'")
        with self.assertRaises(ValueError):self.s.read()
    def test_nonfinite_state_does_not_commit(self):
        with self.assertRaises(ValueError):self.s.apply("bad",{},lambda s:{"cash":float("nan")})
        self.assertEqual(self.s.read()["cash"],3)


class DiagnosticsTests(unittest.TestCase):
    def test_higher_spot_is_not_net_profit(self):
        row={"net_pnl_sol":-.02,"gross_pnl_sol":.01,"mfe":-.01,"post":{"went_higher":True,"coverage":"OBSERVED"}}
        d=classify_loss(row,[-.005,-.001])
        self.assertTrue(d["post_exit_spot_higher"]);self.assertFalse(d["post_exit_net_recovery"])
        self.assertIn("FEE_ERASED_GAIN",d["overlapping_categories"])
    def test_missing_coverage_is_unknown(self):
        d=classify_loss({"net_pnl_sol":-.02,"gross_pnl_sol":-.01},None)
        self.assertIsNone(d["post_exit_net_recovery"])
    def test_filter_reports_sacrificed_winners(self):
        decisions={x:{"baseline_accept":True,"accept":False,"vetoes":["FILTER"]} for x in ("a","b","missing")}
        rows=[{"mint":"a","status":"CLOSED","net_pnl_sol":-.1},{"mint":"b","status":"CLOSED","net_pnl_sol":.2}]
        d=filter_tradeoff(decisions,rows)
        self.assertAlmostEqual(d["descriptive_difference_sol"],-.1)
        self.assertEqual(d["missing_counterfactual_outcomes"],1)


if __name__=="__main__":unittest.main()
