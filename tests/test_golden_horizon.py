"""Synthetic UNIT fixtures only. Never counted as live trades or thesis evidence."""
import copy
import json
import math
import random
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from golden_horizon_engine import ARMS, Config, Engine, buy_quote, fee_breakdown, sell_quote
from golden_horizon_live import state_from_item

T = 1_000_000_000_000

def state(ns=T, x=30.0):
    return {"ns": ns, "vsol": x, "vtok": 30_000_000_000 / x, "rtok": 800_000_000.0,
            "complete": False, "signature": "UNIT_FIXTURE_NOT_ON_CHAIN", "slot": 1}


def decision(mint="UNIT_FIXTURE", tier="HIGH"):
    return {"mint": mint, "tier": tier, "create_signature": "UNIT_FIXTURE_NOT_ON_CHAIN",
            "create_ns": T - 10_000_000, "decision_ns": T, "name": "Unit fixture", "symbol": "TEST"}


def drive(e, mint="UNIT_FIXTURE", x=30.0):
    for ms in range(0, 35001, 50):
        ns = T + ms * 1_000_000
        e.tick(ns, {mint: state(ns, x)}, ns)
    return e


class HorizonTests(unittest.TestCase):
    def test_twelve_variants(self):
        self.assertEqual(len(ARMS), 12)
        self.assertEqual(set(ARMS), {f"{f}_{s}S" for f in ("FULL", "PARTIALS") for s in (2,3,4,5,7,10)})

    def test_input_validation(self):
        for kw in ({"tip_sol": -1}, {"priority_sol": float("nan")}, {"starting_sol": 5}, {"max_slippage_bps": 10000}):
            with self.assertRaises(ValueError): Config(**kw)

    def test_own_price_impact_round_trip(self):
        s = state(); n = .75; t = buy_quote(n, s)
        proceeds = sell_quote(t, s, n, -t)
        self.assertAlmostEqual(proceeds, n, places=7)
        first = t * .3
        p1 = sell_quote(first, s, n, -t)
        p2 = sell_quote(t-first, s, n-p1, -t+first)
        self.assertAlmostEqual(p1+p2, n, places=7)

    def test_accounting_and_partial_fee(self):
        e = Engine(Config()); self.assertTrue(e.submit(decision(), state(), T)); drive(e)
        self.assertFalse(e.bundles); e.validate()
        self.assertEqual(e.matched_closed(), 1)
        for a in e.accounts.values():
            self.assertEqual(len(a["ledger"]), 1); self.assertAlmostEqual(a["rent_locked"], 0)
            p = a["ledger"][0]
            self.assertEqual(p["remaining"], 0); self.assertTrue(p["post"]["complete"])
            self.assertLess(p["net_pnl_sol"], 0) # A flat market must not mint free profit.
            self.assertAlmostEqual(sum(x["tokens"] for x in p["actions"] if x["side"] == "SELL"), p["original"], places=5)
        f = e.accounts["FULL_2S"]["ledger"][0]; p = e.accounts["PARTIALS_2S"]["ledger"][0]
        self.assertEqual(len(p["actions"]), len(f["actions"]) + 1)
        self.assertAlmostEqual(p["fees"]["priority_sol"] - f["fees"]["priority_sol"], e.c.priority_sol)
        self.assertLess(e.accounts["PARTIALS_2S"]["cash"], e.accounts["FULL_2S"]["cash"])

    def test_each_exit_horizon_and_delay(self):
        e = Engine(Config()); e.submit(decision(), state(), T); drive(e)
        for arm, a in e.accounts.items():
            p = a["ledger"][0]
            self.assertGreaterEqual(p["hold_ms"], p["horizon_ms"] + e.c.inclusion_delay_ms)
            self.assertLess(p["hold_ms"], p["horizon_ms"] + e.c.inclusion_delay_ms + 101)

    def test_duplicate_and_concurrency(self):
        e = Engine(Config()); self.assertTrue(e.submit(decision(), state(), T))
        self.assertFalse(e.submit(decision(), state(), T))
        self.assertTrue(e.submit(decision("FIXTURE2"), state(), T))
        self.assertFalse(e.submit(decision("FIXTURE3"), state(), T))
        self.assertEqual(e.accepting_slots(), 2)

    def test_future_state_rejected(self):
        e = Engine(Config()); self.assertFalse(e.submit(decision(), state(T+1), T))
        with self.assertRaises(ValueError):
            d = decision(); d["decision_ns"] = T+1; e.submit(d, state(), T)

    def test_resume_never_erases_open_inventory(self):
        e = Engine(Config()); e.submit(decision(), state(), T)
        with self.assertRaises(ValueError): Engine(e.c, e.persistence())
        drive(e); restored = Engine(e.c, json.loads(json.dumps(e.persistence())))
        restored.validate(); self.assertEqual(restored.matched_closed(), 1)
        self.assertAlmostEqual(restored.accounts["FULL_2S"]["cash"], e.accounts["FULL_2S"]["cash"])

    def test_unknown_post_ticks_not_false(self):
        e = Engine(Config()); e.submit(decision(), state(), T)
        for ms in range(0, 35001, 50):
            ns = T+ms*1_000_000; e.tick(ns, {"UNIT_FIXTURE": state(T)}, ns)
        e.validate()
        for a in e.accounts.values():
            post = a["ledger"][0]["post"]
            self.assertEqual(post["coverage"], "NO_FURTHER_TICKS_OBSERVED")
            self.assertIsNone(post["went_higher"]); self.assertIsNone(post["went_lower"])
        early = e.accounts["FULL_2S"]["ledger"][0]["post"]
        late = e.accounts["FULL_10S"]["ledger"][0]["post"]
        self.assertEqual(late["start_ns"] - early["start_ns"], 8_000_000_000)

    def test_slippage_failure_costs_preserved(self):
        c = Config(); e = Engine(c); e.submit(decision(), state(), T)
        e.tick(T + 250_000_000, {"UNIT_FIXTURE": state(T+250_000_000, 90)}, T+250_000_000)
        for a in ARMS:
            p = e.bundles["UNIT_FIXTURE"]["positions"][a]
            self.assertEqual(p["actions"][0]["outcome"], "MODELLED_LANDED_FAILURE")
            self.assertEqual(p["actions"][0]["fees"]["tip_sol"], 0)
        for ms in range(500, 35001, 50):
            ns = T+ms*1_000_000; e.tick(ns, {"UNIT_FIXTURE": state(ns,90)}, ns)
        e.validate()
        self.assertEqual(e.summary()["FULL_2S"]["modelled_landed_failures"], 1)
        self.assertIsNone(e.summary()["FULL_2S"]["actual_failed_transactions"])

    def test_exhausted_buy_does_not_count_as_trade(self):
        e = Engine(replace(Config(), max_attempts=1)); e.submit(decision(),state(),T)
        for ms in range(250, 1001, 250):
            ns=T+ms*1_000_000; e.tick(ns, {"UNIT_FIXTURE":state(ns,90)},ns)
        e.validate(); self.assertEqual(e.matched_closed(),0)
        for a in e.accounts.values():
            self.assertEqual(len(a["ledger"]),0); self.assertEqual(len(a["aborted_entries"]),1)
            self.assertLess(a["cash"],3)

    def test_stale_feed_does_not_create_fill(self):
        e = Engine(Config()); e.submit(decision(),state(),T)
        e.tick(T+4_000_000_000, {"UNIT_FIXTURE":state(T)}, T)
        self.assertTrue(all(p["status"] == "PENDING" for p in e.bundles["UNIT_FIXTURE"]["positions"].values()))
        self.assertEqual(e.matched_closed(),0)

    def test_migration_open_is_not_free_exit(self):
        e=Engine(Config()); e.submit(decision(),state(),T)
        e.tick(T+250_000_000,{"UNIT_FIXTURE":state(T+250_000_000)},T+250_000_000)
        s=state(T+300_000_000);s["complete"]=True
        e.tick(T+300_000_000,{"UNIT_FIXTURE":s},T+300_000_000)
        self.assertTrue(e.errors);self.assertEqual(e.matched_closed(),0)

    def test_explicit_reserve_units(self):
        item={"anchor_event":"TradeEvent","virtual_sol_reserves":300_000,"virtual_token_reserves":10_000_000,"real_token_reserves":1_000_000}
        s=state_from_item(item,T,"test",1)
        self.assertEqual(s["vsol"],.0003);self.assertEqual(s["vtok"],10)
        item["quote_mint"]="USDC_NOT_SOL"
        self.assertIsNone(state_from_item(item,T,"test",1))

    def test_fee_model_not_forged_actual(self):
        e=Engine(Config()); summary=e.summary()
        for a in summary.values():
            self.assertIsNone(a["actual_profit_sol"]);self.assertIsNone(a["actual_fees_sol"])
        failed=fee_breakdown(e.c,1,True)
        self.assertEqual(failed["protocol_sol"],0);self.assertEqual(failed["platform_sol"],0)
        self.assertEqual(sum(failed.values()), e.c.base_fee_sol + e.c.priority_sol)


def stress(paths=1000):
    """Adversarial pure-accounting paths, NOT market evidence."""
    rng=random.Random(20260915);t0=time.perf_counter();closed=0
    for n in range(paths):
        e=Engine(replace(Config(),max_slippage_bps=9500)); e.submit(decision(),state(),T)
        x=30.0
        for ms in range(0,35001,250):
            x=max(5.0,min(200.0,x*math.exp(rng.uniform(-.15,.15))))
            ns=T+ms*1_000_000;e.tick(ns,{"UNIT_FIXTURE":state(ns,x)},ns)
        if e.errors: raise AssertionError(e.errors)
        e.validate()
        if e.matched_closed()!=1 or e.bundles:raise AssertionError("unclosed stress path")
        closed+=12
    return {"status":"PASS","synthetic_unit_paths":paths,"closed_fixture_positions":closed,
            "fixture_positions_counted_as_live":0,"elapsed_seconds":time.perf_counter()-t0}

if __name__ == "__main__": unittest.main()

class EvidenceGateTests(unittest.TestCase):
    def test_empty_campaign_cannot_complete(self):
        from golden_horizon_report import gate
        with self.assertRaises(ValueError): gate({'engine':Engine(Config()).persistence()},100)
    def test_lowering_target_cannot_complete(self):
        from golden_horizon_report import gate
        with self.assertRaises(ValueError): gate({},1)
    def test_fake_actual_profit_rejected(self):
        from golden_horizon_report import gate
        d=Engine(Config()).persistence();d['actual_profit_sol']=123
        with self.assertRaises(ValueError): gate({'engine':d})
