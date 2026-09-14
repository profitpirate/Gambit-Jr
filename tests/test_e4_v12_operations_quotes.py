import tempfile,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_e4_v12_operations import ops,Build,state

class QuoteFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_quote_fetches_current_rpc_without_backdating(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=ops.PriceFeed(SimpleNamespace(hardening=SimpleNamespace()));p.set_quote(100,"fixture")
            a=ops.Account("B_75",Build,{"m":dict(state(),ns=time.time_ns()-int(3e9))},p,ops.Evidence(tmp))
            async def refresh(m,previous):return dict(state(),quote_source="fresh_rpc")
            with patch.object(ops,"RESERVE_REFRESHER",refresh):
                s=await a.resolve_state("m")
            self.assertEqual(s["quote_source"],"fresh_rpc")
            self.assertLess(time.time_ns()-s["ns"],int(1e9))
    async def test_rpc_failure_does_not_invent_a_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=ops.PriceFeed(SimpleNamespace(hardening=SimpleNamespace()));p.set_quote(100,"fixture")
            a=ops.Account("B_75",Build,{"m":dict(state(),ns=time.time_ns()-int(3e9))},p,ops.Evidence(tmp))
            async def refresh(m,previous):raise ValueError("RPC_DOWN")
            with patch.object(ops,"RESERVE_REFRESHER",refresh):
                with self.assertRaises(ValueError):await a.resolve_state("m")
    async def test_migration_without_reserves_invalidates_old_curve(self):
        e=SimpleNamespace(mint="m",raw={},complete=True,received_ns=time.time_ns(),kind="MIGRATION")
        s=ops.checked_state(e,Build)
        self.assertTrue(s["complete"])
        self.assertEqual(s["rtok"],0)
