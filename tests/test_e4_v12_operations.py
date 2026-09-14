from __future__ import annotations
import asyncio, importlib, json, math, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import e4_v12_live_ops as ops
from e4_v12_ops_watchdog import inspect_status
legacy=ops.legacy

class Build:
    FEE_BPS=125
    PRIORITY_AND_TIP_SOL=.00015
    @staticmethod
    def quote_buy(stake,s):
        cost=max(0,stake-.00015)/1.0125
        return min(cost*s["vtok"]/(s["vsol"]+cost),s["rtok"]),cost
    @staticmethod
    def quote_sell(tokens,s):
        return max(0,tokens*s["vsol"]/(s["vtok"]+tokens)*.9875-.00015)

def state(vsol=30):
    return dict(vsol=vsol,vtok=1073000000.,rtok=793100000.,ns=time.time_ns(),complete=False)

def healthy(now=1000):
    return dict(status="RUNNING",phase="online",checked_at=now,last_event_at=now,
        last_score_at=now,last_launch_at=now,elapsed_seconds=100,
        feed={"fresh":True},counts={"launches_seen":100,"scored":100},
        accounts={n:{"signals":0,"task_errors":[],"unpriced_exposure":False} for n in ops.CANDIDATES},
        score_queue_depth=0,failures=[])

class OperationsTests(unittest.TestCase):
    def test_thresholds_unchanged(self):
        self.assertEqual(ops.CANDIDATES["A_86_96"]["threshold"],.9923740946678363)
        self.assertEqual(ops.CANDIDATES["B_75"]["threshold"],.9838282174643644)
    def test_live_price_updates_actual_decoder(self):
        prod=SimpleNamespace(hardening=SimpleNamespace(_SOL_USD=150))
        feed=ops.PriceFeed(prod);feed.set_quote(99.58,"fixture")
        self.assertEqual(prod.hardening._SOL_USD,99.58)
        self.assertFalse(feed.summary()["fallback_used"])
    def test_no_price_is_not_fresh(self):
        self.assertFalse(ops.PriceFeed(SimpleNamespace()).fresh())
    def test_expired_price_not_fresh(self):
        feed=ops.PriceFeed(SimpleNamespace(hardening=SimpleNamespace()))
        feed.set_quote(100,"fixture",now=time.time()-301)
        self.assertFalse(feed.fresh())
    def test_invalid_price_never_accepted(self):
        feed=ops.PriceFeed(SimpleNamespace(hardening=SimpleNamespace()))
        for x in (0,-1,float("nan"),float("inf")):
            with self.assertRaises(ValueError):feed.set_quote(x,"fixture")
    def test_quote_currency_fail_closed(self):
        for q in (None,"",ops.WSOL):self.assertTrue(ops.native_quote({"quote_mint":q}))
        self.assertFalse(ops.native_quote({"quote_mint":"USDC"}))
    def test_zero_real_tokens_not_infinite(self):
        event=SimpleNamespace(raw={"virtual_sol_reserves":30e9,"virtual_token_reserves":1073e12,
               "real_token_reserves":0},received_ns=time.time_ns(),slot=1,price_sol=1,fdv_usd=1,complete=False)
        b=SimpleNamespace(normal_sol=lambda x:x/1e9,normal_tokens=lambda x:x/1e6)
        self.assertEqual(ops.checked_state(event,b)["rtok"],0)
    def test_missing_real_tokens_unknown(self):
        event=SimpleNamespace(raw={"virtual_sol_reserves":30e9,"virtual_token_reserves":1073e12,
              "real_token_reserves":None},received_ns=time.time_ns(),slot=1,price_sol=1,fdv_usd=1,complete=False)
        b=SimpleNamespace(normal_sol=lambda x:(x or 0)/1e9,normal_tokens=lambda x:(x or 0)/1e6)
        self.assertIsNone(ops.checked_state(event,b)["rtok"])
    def test_feature_schema_mismatch_fails(self):
        with self.assertRaises(ValueError):ops.validate_row({},["test"])
    def test_feature_nan_fails(self):
        with self.assertRaises(ValueError):ops.validate_row({"test":float("nan")},["test"])
    def test_watchdog_healthy(self):
        self.assertEqual(inspect_status(healthy(),1000),([],[]))
    def test_watchdog_stale_heartbeat(self):
        self.assertIn("RUNNER_HEARTBEAT_STALE",inspect_status(healthy(),1031)[1])
    def test_watchdog_feed_stall(self):
        s=healthy();s["last_event_at"]=900
        self.assertIn("LIVE_FEED_STALLED",inspect_status(s,1000)[1])
    def test_watchdog_stale_price(self):
        s=healthy();s["feed"]["fresh"]=False
        self.assertIn("SOL_USD_STALE",inspect_status(s,1000)[1])
    def test_watchdog_scorer_stall(self):
        s=healthy();s["last_score_at"]=950;s["score_queue_depth"]=2
        self.assertIn("SCORING_STALLED",inspect_status(s,1000)[1])
    def test_watchdog_inactivity_is_warning_not_fabricated_trades(self):
        s=healthy();s["counts"]["scored"]=300;s["elapsed_seconds"]=901
        w,f=inspect_status(s,1000)
        self.assertEqual(len(w),2);self.assertEqual(f,[])
        self.assertEqual(s["accounts"]["A_86_96"]["signals"],0)
    def test_watchdog_catches_task_error(self):
        s=healthy();s["accounts"]["A_86_96"]["task_errors"]=["boom"]
        self.assertIn("A_86_96:EXECUTION_FAILURE",inspect_status(s,1000)[1])
    def test_watchdog_invalid_feature_rate(self):
        s=healthy();s["counts"]["invalid_features"]=11
        self.assertIn("FEATURE_CONTRACT_FAILURE_RATE",inspect_status(s,1000)[1])
    def test_rejected_scores_are_preserved(self):
        m=ops.Monitor(ops.CANDIDATES)
        d=m.decision("A_86_96",{"mint":"m","create_ns":1},.4,"BELOW_THRESHOLD",2)
        self.assertEqual(d["reason"],"BELOW_THRESHOLD")
        self.assertEqual(m.summary()["A_86_96"]["score_max"],.4)
    def test_invalid_model_score_fails(self):
        m=ops.Monitor(ops.CANDIDATES)
        with self.assertRaises(ValueError):m.decision("A_86_96",{"mint":"m"},float("nan"),"SIGNAL",0)
    def test_fast_head_same_raw_scores(self):
        rng=np.random.default_rng(7331);x=rng.normal(size=(80,8)).astype(np.float32)
        train=np.arange(80)<50;target=np.arange(80)%3==0
        with patch.object(legacy,"NearestNeighbors",side_effect=lambda **kw:ops.NearestNeighbors(**dict(kw,n_jobs=1))):
            before=legacy.DistanceHead(x,train,target,3)
        after=ops.FastHead(x,train,target,3)
        np.testing.assert_allclose(before.raw(x),after.raw(x),atol=1e-7)
        self.assertAlmostEqual(before.percentile(x[55]),after.percentile(x[55]),12)

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.e=ops.Evidence(Path(self.tmp.name))
        self.price=ops.PriceFeed(SimpleNamespace(hardening=SimpleNamespace()))
        self.price.set_quote(100,"fixture")
        self.states={"m":state()}
        self.a=ops.Account("A_86_96",Build,self.states,self.price,self.e,delay_ms=0)
        self.a.config=dict(self.a.config,policy="hold_20ms")
    def tearDown(self):self.tmp.cleanup()
    async def test_live_entry_exit_cash_locks_then_settles(self):
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.sleep(.005)
        self.assertEqual(len(self.a.active),1)
        self.assertAlmostEqual(self.a.cash,2-.037)
        self.assertAlmostEqual(self.a.equity(),2)
        self.states["m"]=state(vsol=45)
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(len(self.a.ledger),1)
        self.assertGreater(self.a.ledger[0]["pnl_sol"],0)
        self.assertAlmostEqual(self.a.cash,2+self.a.ledger[0]["pnl_sol"])
    async def test_duplicate_signal_no_duplicate_entry(self):
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(len(self.a.ledger),1)
        self.assertEqual(self.a.rejections["DUPLICATE"],1)
    async def test_separate_bankrolls(self):
        b=ops.Account("B_75",Build,self.states,self.price,self.e,delay_ms=0)
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(b.cash,2)
        self.assertEqual(b.ledger,[])
    async def test_zero_trades_wr_null(self):
        self.assertIsNone(self.a.summary()["win_rate"])
        self.assertIsNone(self.a.summary()["profit_factor"])
    async def test_zero_liquidity_no_fill(self):
        self.states["m"]["rtok"]=0
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(self.a.rejections["NO_REAL_TOKEN_LIQUIDITY"],1)
        self.assertEqual(self.a.cash,2)
    async def test_stale_reserves_rejected(self):
        self.states["m"]["ns"]-=int(3e9)
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(self.a.rejections["STALE_RESERVE_STATE"],1)
    async def test_price_expired_blocks_entry(self):
        self.price.updated-=400
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(self.a.rejections["STALE_SOL_USD"],1)
    async def test_exit_failure_retains_unresolved_position(self):
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.sleep(.005)
        self.states["m"]["complete"]=True
        await asyncio.gather(*list(self.a.tasks))
        self.assertEqual(len(self.a.ledger),0)
        self.assertTrue(self.a.summary()["unpriced_exposure"])
        self.assertEqual(len(self.a.active),1)
    async def test_task_exceptions_not_swallowed(self):
        async def broken(*a):raise RuntimeError("fixture failure")
        self.a.trade=broken
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        await asyncio.gather(*list(self.a.tasks),return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(self.a.errors)
    async def test_operations_pause_does_not_force_trade(self):
        self.a.entries_paused=True
        self.a.schedule("m",1,time.time_ns(),self.states["m"])
        self.assertFalse(self.a.tasks)
        self.assertEqual(self.a.cash,2)
        self.assertEqual(self.a.rejections["OPERATIONS_PAUSED"],1)


class ProcessSupervisorTests(unittest.TestCase):
    def test_preparing_uses_separate_startup_deadline(self):
        self.assertEqual(inspect_status({"status":"PREPARING","checked_at":1},100),([],[]))
    def test_independent_process_kills_stalled_runner(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);script=out/"child.py"
            script.write_text("import json,time\nfrom pathlib import Path\n"+
                f"p=Path({str(out/'status.json')!r})\n"+
                "p.write_text(json.dumps({'status':'RUNNING','phase':'online','checked_at':time.time()-100,'feed':{'fresh':True}}))\n"+
                "time.sleep(10)\n")
            result=subprocess.run([sys.executable,str(Path(ops.__file__).with_name("e4_v12_ops_watchdog.py")),
                "--output-dir",tmp,"--repo","","--poll-seconds",".02","--",sys.executable,str(script)],
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,3)
            report=json.loads((out/"supervisor.json").read_text())
            self.assertEqual(report["stop_reason"],"RUNNER_HEARTBEAT_STALE")
            self.assertFalse(report["sample_complete"])
    def test_success_exit_with_zero_sample_cannot_be_green(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);script=out/"child.py"
            script.write_text("import json,time\nfrom pathlib import Path\n"+
                f"p=Path({str(out/'status.json')!r})\n"+
                "p.write_text(json.dumps({'status':'INSUFFICIENT_SAMPLE','checked_at':time.time(),'accounts':{}}))\n")
            result=subprocess.run([sys.executable,str(Path(ops.__file__).with_name("e4_v12_ops_watchdog.py")),
                "--output-dir",tmp,"--repo","","--poll-seconds",".02","--",sys.executable,str(script)],
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,3)
            report=json.loads((out/"supervisor.json").read_text())
            self.assertFalse(report["sample_complete"])
            self.assertFalse(report["performance_pass"])

if __name__=="__main__":unittest.main()
