from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from memecoin_bot.config import Settings
from memecoin_bot.intelligence import (
    constant_product_impact,
    entry_quality,
    priority,
    probable_rug,
    reconcile_field,
    social_presence,
    stale_alert,
    wallet_intelligence,
)
from memecoin_bot.models import DiscoveryEvent, MarketSnapshot, RadarResult, iso
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.gmgn import READ_ONLY_ROUTES, GmgnProvider
from memecoin_bot.signals import format_discord_event, radar_payload
from tests.helpers import store, temp_db_path


class FakeClient:
    def __init__(self, fail: str | None = None):
        self.calls = []
        self.fail = fail

    async def request(self, url, method="GET", payload=None, headers=None):
        self.calls.append((url, method, headers))
        if self.fail and self.fail in url:
            raise ProviderError("unavailable")
        if "holders" in url:
            data = {"list": [{"address": "a", "tags": ["smart"]}, {"address": "b", "tags": ["smart"]}]}
        elif "traders" in url:
            data = {"list": [{"address": "c", "tags": ["smart", "whale"]}]}
        else:
            data = {"liquidity": 42000, "twitter": "https://x.com/example"}
        return {"code": 0, "data": data}


class GmgnTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_documented_read_routes_and_api_key_header(self):
        client = FakeClient()
        provider = GmgnProvider("https://openapi.gmgn.ai", "secret-value", client, 60, 2)
        result = await provider.enrich("solana", "token")
        self.assertEqual(len(client.calls), 5)
        self.assertTrue(all(method == "GET" for _, method, _ in client.calls))
        self.assertTrue(all(headers == {"X-APIKEY": "secret-value"} for _, _, headers in client.calls))
        self.assertTrue(all("timestamp=" in url and "client_id=" in url for url, _, _ in client.calls))
        self.assertEqual(set(READ_ONLY_ROUTES), {"info", "security", "pool", "holders", "traders"})
        self.assertNotIn("secret-value", json.dumps(provider.redacted_config()))
        self.assertEqual(result.chain, "sol")

    async def test_cache_and_inflight_dedupe(self):
        client = FakeClient()
        provider = GmgnProvider("https://api.gmgn.ai", "key", client, 60, 2)
        a, b = await asyncio.gather(provider.enrich("bsc", "0x1"), provider.enrich("bsc", "0x1"))
        self.assertIs(a, b)
        await provider.enrich("bsc", "0x1")
        self.assertEqual(len(client.calls), 5)

    async def test_partial_outage_is_unknown_not_false(self):
        result = await GmgnProvider("https://api.gmgn.ai", "key", FakeClient("security"), 60, 2).enrich("bsc", "0x1")
        self.assertIsNone(result.security)
        self.assertIn("SECURITY", result.unavailable)
        self.assertIsNotNone(result.info)

    async def test_malformed_payload_is_degraded(self):
        class BadClient:
            async def request(self, *args, **kwargs): return []
        result = await GmgnProvider("https://api.gmgn.ai", "key", BadClient(), 60, 1).enrich("bsc", "0x1")
        self.assertEqual(set(result.unavailable), {"INFO", "SECURITY", "POOL", "HOLDERS", "TRADERS"})

    def test_config_requires_no_private_or_trading_key(self):
        fields = Settings.__dataclass_fields__
        self.assertIn("gmgn_api_key", fields)
        self.assertFalse(any("private" in name or "trade" in name or "seed" in name for name in fields))
        with patch.dict(os.environ, {"GMGN_ENABLED": "true", "GMGN_API_KEY": "read-key"}, clear=False):
            cfg = Settings.from_env(); cfg.validate()
            self.assertEqual(cfg.gmgn_api_key, "read-key")


class IntelligenceHeuristicTests(unittest.TestCase):
    def test_source_reconciliation_conflict_and_unknown(self):
        self.assertEqual(reconcile_field([])["status"], "UNKNOWN")
        values = [{"value": 42000, "provider": "dex"}, {"value": 41980, "provider": "gmgn"}]
        self.assertEqual(reconcile_field(values)["status"], "HIGH_CONFIDENCE")
        values[1]["value"] = 25000
        self.assertEqual(reconcile_field(values)["status"], "DATA_CONFLICT")

    def test_entry_quality_all_bands(self):
        self.assertEqual(entry_quality(None, 1), "UNKNOWN")
        self.assertEqual(entry_quality(100, 120), "EARLY")
        self.assertEqual(entry_quality(100, 150), "ACCEPTABLE")
        self.assertEqual(entry_quality(100, 220), "EXTENDED")
        self.assertEqual(entry_quality(100, 400), "CHASING")

    def test_slippage_is_estimate_and_handles_thin_pool(self):
        self.assertEqual(constant_product_impact(None, 100)["quality"], "UNKNOWN")
        self.assertEqual(constant_product_impact(100000, 50)["quality"], "GOOD")
        thin = constant_product_impact(2000, 500)
        self.assertEqual(thin["quality"], "POOR")
        self.assertTrue(thin["estimate_only"])

    def test_wallet_labels_separate_and_clusters_probabilistic(self):
        rows = [
            {"address": f"w{i}", "tags": [tag], "funder": "same", "amount": 1}
            for i, tag in enumerate(("smart", "smart", "smart", "sniper", "insider", "bundler"))
        ]
        result = wallet_intelligence(rows, [])
        self.assertEqual(result["smart_money"], "SMART_MONEY_CONVERGENCE")
        self.assertEqual(result["counts"]["sniper"], 1)
        self.assertEqual(result["counts"]["insider"], 1)
        self.assertEqual(result["counts"]["bundler"], 1)
        self.assertTrue(result["possible_wallet_cluster"])
        self.assertEqual(result["activity_quality"], "SUSPICIOUS")
        self.assertEqual(wallet_intelligence(None, None)["activity_quality"], "UNKNOWN")
        counts_only = wallet_intelligence(None, None, {"wallet_tags_stat": {"smart_wallets": 4, "bundler_wallets": 2}})
        self.assertEqual(counts_only["smart_money"], "SMART_MONEY_CONVERGENCE")
        self.assertEqual(counts_only["counts"]["bundler"], 2)

    def test_priority_cannot_be_caused_by_social_or_override_safety(self):
        self.assertEqual(priority(99, .99, False, 10, True, True), "STANDARD")
        self.assertEqual(priority(99, .99, True, 10, True), "STANDARD")
        self.assertEqual(priority(90, .8, False, 4, True), "PRIORITY")
        self.assertEqual(priority(80, .6, False, 3, False), "HOT")

    def test_probable_rug_requires_material_evidence(self):
        self.assertTrue(probable_rug(10000, 900, 500000, 50000)["probable_rug"])
        ordinary = probable_rug(10000, 9500, 500000, 350000)
        self.assertFalse(ordinary["probable_rug"])
        self.assertTrue(ordinary["ordinary_pullback"])

    def test_stale_alert_and_social_unknown(self):
        self.assertEqual(stale_alert("2026-01-01T00:00:00+00:00", "2026-01-01T00:03:00+00:00", 120)["status"], "STALE_SNAPSHOT")
        self.assertEqual(social_presence({}, iso())["twitter"]["status"], "UNKNOWN")


class PersistenceV13Tests(unittest.TestCase):
    def test_v12_database_migrates_additively_and_reconciles(self):
        with temp_db_path() as path:
            db = store(path)
            versions = {r[0] for r in db.conn.execute("SELECT version FROM schema_migrations")}
            self.assertIn("004_v13_intelligence.sql", versions)
            token, _ = db.upsert_discovery(DiscoveryEvent(token_address="x", source="test"))
            db.ensure_candidate(token, iso(), "v1.3")
            self.assertEqual(db.state_reconciliation()["difference"], 0)
            db.close()

    def test_per_channel_delivery_is_restart_safe(self):
        with temp_db_path() as path:
            db = store(path)
            db.conn.execute("INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                            ("x", "EARLY_RADAR", "{}", iso()))
            oid = db.conn.execute("SELECT id FROM outbox").fetchone()[0]
            db.ensure_alert_deliveries(oid, (11, 22))
            rows = db.pending_alert_deliveries(oid)
            self.assertEqual(len(rows), 2)
            db.mark_alert_delivery(rows[0]["id"], True, "message-a")
            self.assertEqual([r["channel_id"] for r in db.pending_alert_deliveries(oid)], ["22"])
            db.close()
            reopened = store(path)
            self.assertEqual([r["channel_id"] for r in reopened.pending_alert_deliveries(oid)], ["22"])
            reopened.close()

    def test_actual_v12_schema_upgrades_without_history_loss(self):
        with temp_db_path() as path:
            conn = sqlite3.connect(path)
            for name in ("001_initial.sql", "002_candidate_lifecycle.sql", "003_radar_multichain.sql"):
                conn.executescript((Path("migrations") / name).read_text(encoding="utf-8"))
                conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(?,?)", (name, iso()))
            conn.execute("INSERT INTO tokens(chain,token_address,source,first_discovered_at) VALUES(?,?,?,?)",
                         ("solana", "historic", "v1.2", iso()))
            conn.commit(); conn.close()
            upgraded = store(path)
            self.assertEqual(upgraded.conn.execute("SELECT source FROM tokens WHERE token_address='historic'").fetchone()[0], "v1.2")
            self.assertIn("004_v13_intelligence.sql", {r[0] for r in upgraded.conn.execute("SELECT version FROM schema_migrations")})
            upgraded.close()
            reopened = store(path)
            self.assertEqual(reopened.state_reconciliation()["difference"], 0)
            reopened.close()

    def test_gmgn_raw_and_wallet_evidence_persist(self):
        with temp_db_path() as path:
            db = store(path)
            token, _ = db.upsert_discovery(DiscoveryEvent(token_address="x", source="test"))
            snap = {"retrieved_at": iso(), "info": {"name": "X"}, "security": None,
                    "pool": {}, "holders": [], "traders": [], "unavailable": ["SECURITY"]}
            wallet = wallet_intelligence([], [])
            db.save_gmgn_intelligence(token, snap, wallet)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM gmgn_snapshots").fetchone()[0], 1)
            self.assertEqual(db.token_intelligence("x")["gmgn"]["unavailable"], ["SECURITY"])
            db.close()

    def test_original_radar_payload_is_immutable_and_restart_safe(self):
        with temp_db_path() as path:
            db = store(path)
            token, _ = db.upsert_discovery(DiscoveryEvent(token_address="x", source="test"))
            candidate, _ = db.ensure_candidate(token, iso(), "v1.3")
            market = MarketSnapshot("x", iso(), "test", market_cap_usd=10000, liquidity_usd=9000)
            db.save_snapshot(token, market)
            payload = radar_payload(DiscoveryEvent(token_address="x"), market, RadarResult(True, 70, ["A"], []), 1)
            self.assertTrue(db.trigger_radar(candidate, 70, ["A"], market, payload))
            self.assertFalse(db.trigger_radar(candidate, 70, ["A"], market, payload))
            with self.assertRaises(sqlite3.IntegrityError):
                db.conn.execute("UPDATE radar_events SET radar_score=99")
            db.close()

    def test_intelligence_event_dedupes(self):
        with temp_db_path() as path:
            db = store(path)
            token, _ = db.upsert_discovery(DiscoveryEvent(token_address="x", source="test"))
            self.assertTrue(db.record_intelligence_event(token, "dev:x:1", "DEV_SELLING", {"observed": True}))
            self.assertFalse(db.record_intelligence_event(token, "dev:x:1", "DEV_SELLING", {"observed": True}))
            db.close()

    def test_discord_has_gmgn_link_full_contract_and_prominent_name(self):
        address = "0x" + "a" * 40
        card = format_discord_event("EARLY_RADAR", {"name": "QUEENDAO", "symbol": "QQQB",
            "chain": "bsc", "token_address": address, "pair_address": "pair", "market_cap_usd": 80000,
            "liquidity_usd": 24000, "radar_score": 89, "reasons": ["VOLUME_ACCELERATING"]})
        self.assertIn(address, card["content"])
        self.assertIn("QUEENDAO", card["embeds"][0]["description"])
        urls = [button["url"] for row in card["components"] for button in row["components"]]
        self.assertTrue(any("gmgn.ai/bsc/token/" in url for url in urls))


if __name__ == "__main__":
    unittest.main()
