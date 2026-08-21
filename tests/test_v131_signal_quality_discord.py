from __future__ import annotations

import logging
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from memecoin_bot.database import Store
from memecoin_bot.discord import bot_runtime
from memecoin_bot.discord.cards import (
    settings_card,
    smartmoney_card,
    status_card,
    token_card,
)
from memecoin_bot.discord.cards import (
    test_alert_card as build_test_alert_card,
)
from memecoin_bot.intelligence import (
    catalyst_timing,
    entry_quality,
    intelligence_pillar,
    narrative_context,
    setup_quality,
    signal_convergence,
)
from memecoin_bot.models import CandidateState, DiscoveryEvent, MarketSnapshot, iso
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.service import IntelligenceService
from tests.helpers import create_signal, settings, store, temp_db_path
from tests.test_candidate_lifecycle import EmptyDiscovery, NullNotifier, SafeRpc


class MissingMarket:
    name = "missing"
    calls = 0

    async def market_snapshot(self, address, chain="solana"):
        self.calls += 1


class FailedMarket(MissingMarket):
    async def market_snapshot(self, address, chain="solana"):
        self.calls += 1
        raise ProviderError("market down")


class CandidateAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_age_is_enforced_before_missing_pair_provider_call(self):
        with temp_db_path() as path:
            config = settings(path)
            market = MissingMarket()
            db = store(path)
            service = IntelligenceService(
                config, db, EmptyDiscovery(), market, SafeRpc(), NullNotifier()
            )
            old = (datetime.now(UTC) - timedelta(minutes=181)).isoformat()
            result = await service.evaluate(
                DiscoveryEvent(token_address="old-missing", discovered_at=old)
            )
            row = db.candidate_for_token(db.token_id("old-missing"))
            self.assertEqual(result, CandidateState.EXPIRED)
            self.assertEqual(row["reason"], "CANDIDATE_MAX_AGE_EXCEEDED")
            self.assertIsNotNone(row["last_attempted_at"])
            self.assertEqual(market.calls, 0)
            db.close()

    async def test_provider_failure_cannot_bypass_ttl(self):
        with temp_db_path() as path:
            config = settings(path)
            market = FailedMarket()
            db = store(path)
            service = IntelligenceService(
                config, db, EmptyDiscovery(), market, SafeRpc(), NullNotifier()
            )
            old = (datetime.now(UTC) - timedelta(minutes=181)).isoformat()
            await service.evaluate(DiscoveryEvent(token_address="old-provider", discovered_at=old))
            row = db.candidate_for_token(db.token_id("old-provider"))
            self.assertEqual(row["state"], CandidateState.EXPIRED)
            self.assertEqual(market.calls, 0)
            db.close()

    async def test_pair_unavailable_records_attempt_and_exponential_retry(self):
        with temp_db_path() as path:
            config = settings(path)
            db = store(path)
            service = IntelligenceService(
                config, db, EmptyDiscovery(), MissingMarket(), SafeRpc(), NullNotifier()
            )
            await service.evaluate(DiscoveryEvent(token_address="retry"))
            first = db.candidate_for_token(db.token_id("retry"))
            first_due = datetime.fromisoformat(first["next_retry_at"])
            first_attempt = first["last_attempted_at"]
            await service._monitor_candidate(first, DiscoveryEvent(token_address="retry"))
            second = db.candidate_for_token(db.token_id("retry"))
            self.assertEqual(second["attempt_count"], 2)
            self.assertGreater(datetime.fromisoformat(second["next_retry_at"]), first_due)
            self.assertGreaterEqual(
                (
                    datetime.fromisoformat(second["next_retry_at"])
                    - datetime.fromisoformat(second["last_attempted_at"])
                ).total_seconds(),
                60,
            )
            self.assertNotEqual(second["last_attempted_at"], first_attempt)
            db.close()


class ReconciliationAndSchedulerTests(unittest.TestCase):
    def test_v13_database_migrates_in_place_and_second_migrate_is_a_noop(self):
        root = Path(__file__).resolve().parents[1]
        with temp_db_path() as path:
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES('001_initial.sql','now'),('002_candidate_lifecycle.sql','now'),
                  ('003_radar_multichain.sql','now'),('004_v13_intelligence.sql','now');
                CREATE TABLE tokens(id INTEGER PRIMARY KEY,chain TEXT,token_address TEXT);
                CREATE TABLE candidates(id INTEGER PRIMARY KEY,token_id INTEGER,state TEXT,reason TEXT,
                  first_discovered_at TEXT,radar_triggered_at TEXT,normalized_score REAL,confidence REAL);
                CREATE TABLE provider_health(provider TEXT PRIMARY KEY,healthy INTEGER NOT NULL,
                  consecutive_failures INTEGER NOT NULL,last_success_at TEXT,last_failure_at TEXT,
                  last_error TEXT,updated_at TEXT NOT NULL);
                CREATE TABLE outbox(id INTEGER PRIMARY KEY);
                CREATE TABLE radar_events(id INTEGER PRIMARY KEY);
                INSERT INTO tokens VALUES(1,'solana','preserved-v13');
                INSERT INTO candidates VALUES(1,1,'PENDING_EVIDENCE','PAIR_NOT_AVAILABLE','2026-08-21T00:00:00+00:00',NULL,NULL,NULL);
                INSERT INTO provider_health VALUES('gmgn',0,0,NULL,'now','DISABLED','now');
            """)
            old.commit()
            old.close()
            upgraded = Store(path, root / "migrations")
            upgraded.migrate()
            upgraded.migrate()
            versions = [
                row[0] for row in upgraded.conn.execute("SELECT version FROM schema_migrations")
            ]
            self.assertIn("005_v131_signal_quality_discord.sql", versions)
            self.assertEqual(
                upgraded.conn.execute("SELECT reason FROM candidates WHERE id=1").fetchone()[0],
                "PAIR_NOT_AVAILABLE",
            )
            self.assertEqual(
                upgraded.conn.execute(
                    "SELECT state FROM provider_health WHERE provider='gmgn'"
                ).fetchone()[0],
                "DISABLED",
            )
            self.assertEqual(
                upgraded.conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version='005_v131_signal_quality_discord.sql'"
                ).fetchone()[0],
                1,
            )
            upgraded.close()

    def test_startup_reconciliation_is_idempotent_and_preserves_reason(self):
        with temp_db_path() as path:
            db = store(path)
            old = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
            token, _ = db.upsert_discovery(
                DiscoveryEvent(token_address="legacy", discovered_at=old)
            )
            candidate, _ = db.ensure_candidate(token, old, "v1.3")
            db.update_candidate(candidate, CandidateState.PENDING_EVIDENCE, "PAIR_NOT_AVAILABLE")
            now = datetime.now(UTC).isoformat()
            self.assertEqual(db.reconcile_stale_candidates(180, now), 1)
            self.assertEqual(db.reconcile_stale_candidates(180, now), 1)
            row = db.candidate_for_token(token)
            transitions = db.conn.execute(
                "SELECT COUNT(*) FROM candidate_transitions WHERE candidate_id=? AND reason='STALE_PENDING_RECONCILIATION'",
                (candidate,),
            ).fetchone()[0]
            self.assertEqual(transitions, 1)
            self.assertEqual(row["previous_reason"], "PAIR_NOT_AVAILABLE")
            db.close()

    def test_thousand_candidate_backlog_guarantees_fresh_capacity_and_chain_fairness(self):
        with temp_db_path() as path:
            db = store(path)
            due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            for index in range(900):
                chain = "solana" if index % 2 == 0 else "bsc"
                token, _ = db.upsert_discovery(
                    DiscoveryEvent(token_address=f"retry-{index}", chain=chain)
                )
                candidate, _ = db.ensure_candidate(token, due, "v1.3")
                db.begin_candidate_attempt(candidate, due)
                db.conn.execute(
                    "UPDATE candidates SET next_retry_at=?,consecutive_missing_pair_count=8 WHERE id=?",
                    (due, candidate),
                )
            fresh_ids = set()
            for index in range(100):
                chain = "solana" if index % 2 == 0 else "bsc"
                token, _ = db.upsert_discovery(
                    DiscoveryEvent(token_address=f"fresh-{index}", chain=chain)
                )
                candidate, _ = db.ensure_candidate(token, due, "v1.3")
                fresh_ids.add(candidate)
            selected = db.active_candidates(
                100, 50, fresh_reserved=25, radar_reserved=10, near_signal_reserved=10
            )
            selected_fresh = [row for row in selected if int(row["id"]) in fresh_ids]
            chains = {
                chain: sum(row["chain"] == chain for row in selected) for chain in ("solana", "bsc")
            }
            self.assertGreaterEqual(len(selected_fresh), 25)
            self.assertEqual(chains, {"solana": 50, "bsc": 50})
            self.assertEqual(len(selected), 100)
            db.close()


class ProviderAndGuildTests(unittest.TestCase):
    def test_provider_states_distinguish_disabled_rate_limit_and_health(self):
        with temp_db_path() as path:
            db = store(path)
            db.set_provider_health("gmgn", False, 0, "GMGN_DISABLED", "DISABLED")
            db.set_provider_health("dex", False, 1, "HTTP 429 rate limited")
            db.set_provider_health("rpc", True, 0, None)
            states = {r["provider"]: r["state"] for r in db.status_stats("now")["provider_status"]}
            self.assertEqual(states, {"dex": "RATE_LIMITED", "gmgn": "DISABLED", "rpc": "HEALTHY"})
            self.assertEqual(db.status_stats("now")["providers_total"], 2)
            db.close()

    def test_radar_outcomes_track_2x_5x_and_liquidity_collapse_idempotently(self):
        with temp_db_path() as path:
            db = store(path)
            token, _ = db.upsert_discovery(
                DiscoveryEvent(token_address="radar-outcome", symbol="RAD")
            )
            candidate, _ = db.ensure_candidate(token, iso(), "v1.3.1")

            def snap(mc: float, liquidity: float) -> MarketSnapshot:
                return MarketSnapshot(
                    token_address="radar-outcome",
                    captured_at=iso(),
                    source="test",
                    market_cap_usd=mc,
                    liquidity_usd=liquidity,
                    price_usd=mc / 1e9,
                )

            initial = snap(10_000, 10_000)
            db.save_snapshot(token, initial)
            self.assertTrue(
                db.trigger_radar(
                    candidate,
                    80,
                    ["acceleration"],
                    initial,
                    {"token_address": "radar-outcome", "priority": "HOT"},
                )
            )
            db.save_snapshot(token, snap(20_000, 12_000))
            db.save_snapshot(token, snap(50_000, 15_000))
            db.save_snapshot(token, snap(1_000, 500))
            db.save_snapshot(token, snap(1_000, 500))
            outcome = db.conn.execute("SELECT * FROM radar_outcomes").fetchone()
            keys = [
                row[0]
                for row in db.conn.execute(
                    "SELECT event_key FROM outbox WHERE event_type IN ('RADAR_MILESTONE','RADAR_RISK') ORDER BY event_key"
                )
            ]
            self.assertEqual(outcome["peak_multiple"], 5)
            self.assertEqual(outcome["status"], "PROBABLE_RUG")
            self.assertEqual(
                keys, ["radar-milestone:1:2", "radar-milestone:1:5", "radar-risk:1:probable-rug"]
            )
            db.close()

    def test_multi_guild_settings_and_delivery_identity(self):
        with temp_db_path() as path:
            db = store(path)
            create_signal(db)
            db.set_guild_settings(1, 11, True, "ALL", 99)
            db.set_guild_settings(2, 22, True, "PRIORITY", 99)
            self.assertEqual(db.guild_settings(1)["alert_channel_id"], "11")
            db.ensure_guild_alert_delivery(1, 1, 11)
            db.ensure_guild_alert_delivery(1, 1, 11)
            db.ensure_guild_alert_delivery(1, 2, 22)
            self.assertEqual(len(db.pending_guild_alert_deliveries(1)), 2)
            self.assertTrue(db.alert_allowed("QUALIFIED", "SIGNAL", {}))
            self.assertFalse(db.alert_allowed("PRIORITY", "EARLY_RADAR", {"priority": "HOT"}))
            db.close()

    def test_test_alert_is_separate_from_signal_and_radar_state(self):
        with temp_db_path() as path:
            db = store(path)
            before = tuple(
                db.conn.execute(
                    "SELECT (SELECT COUNT(*) FROM signals),(SELECT COUNT(*) FROM radar_events)"
                ).fetchone()
            )
            db.record_test_alert(1, 11, 99, "message")
            after = tuple(
                db.conn.execute(
                    "SELECT (SELECT COUNT(*) FROM signals),(SELECT COUNT(*) FROM radar_events)"
                ).fetchone()
            )
            self.assertEqual(before, after)
            self.assertEqual(
                db.conn.execute("SELECT COUNT(*) FROM test_alert_events").fetchone()[0], 1
            )
            db.close()


class IntelligenceAndCardsTests(unittest.TestCase):
    def test_convergence_requires_diversity_and_setup_explains_grade(self):
        one = {"market": intelligence_pillar(100, 1)}
        self.assertEqual(signal_convergence(one)["class"], "WEAK")
        diverse = {
            name: intelligence_pillar(90, 0.9)
            for name in ("market", "wallet", "narrative", "social", "safety", "entry")
        }
        self.assertEqual(signal_convergence(diverse)["class"], "EXCEPTIONAL")
        setup = setup_quality(diverse, "CHASING")
        self.assertEqual(setup["entry_penalty"], 20)
        self.assertIn("mean", setup["explanation"])

    def test_narrative_entry_and_catalyst_states(self):
        now = datetime.now(UTC)
        context = narrative_context("AI", now.isoformat(), peer_count=25, copycat_count=12)
        self.assertEqual(context["saturation"], "SATURATED")
        self.assertEqual(entry_quality(100, 600), "LATE")
        self.assertEqual(
            catalyst_timing(now.isoformat(), (now - timedelta(minutes=30)).isoformat()),
            "EARLY_AFTER_CATALYST",
        )

    def test_all_major_card_builders_emit_rich_embeds_without_raw_json(self):
        stats = {
            "provider_status": [{"provider": "gmgn", "state": "DISABLED"}],
            "state_reconciliation": {"difference": 0},
        }
        token = {
            "token_address": "abc",
            "symbol": "T",
            "chain": "solana",
            "wallet_intelligence": {"counts": {}},
        }
        cards = [
            status_card(stats),
            token_card(token),
            smartmoney_card(token),
            settings_card(None),
            build_test_alert_card(),
        ]
        for payload in cards:
            self.assertIn("embed", payload)
            self.assertNotIn("```json", str(payload).lower())
        self.assertIn("DISABLED", str(cards[0]))
        self.assertIn("TEST / NON-LIVE", str(cards[-1]))


class MultiGuildDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_guild_failure_does_not_duplicate_successful_guild(self):
        class Notifier:
            def __init__(self):
                self.calls = []

            async def send_to(self, channel, content):
                self.calls.append(channel)
                if channel == 22 and self.calls.count(22) == 1:
                    raise RuntimeError("guild failure")
                return f"m-{channel}"

        with temp_db_path() as path:
            db = store(path)
            create_signal(db)
            db.set_guild_settings(1, 11, True, "ALL")
            db.set_guild_settings(2, 22, True, "ALL")
            service = object.__new__(IntelligenceService)
            service.store, service.notifier, service.settings = db, Notifier(), settings(path)
            service.log = logging.getLogger("test-v131")
            self.assertEqual(await service.flush_outbox(), 0)
            self.assertEqual(await service.flush_outbox(), 1)
            self.assertEqual(service.notifier.calls.count(11), 1)
            self.assertEqual(service.notifier.calls.count(22), 2)
            self.assertEqual(len(db.pending_outbox()), 0)
            db.close()


class DiscordCommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_discord_commands_register_without_network_access(self):
        class Service:
            started_at = iso()

            async def run(self):
                pass

            def stop(self):
                pass

        with temp_db_path() as path:
            db = store(path)
            config = settings(path)
            config.discord_token = "test-token"
            trees = []
            original_tree = bot_runtime.app_commands.CommandTree

            def tree_factory(client):
                tree = original_tree(client)
                trees.append(tree)
                return tree

            # Client.start returning immediately exercises command construction and type resolution.
            with (
                patch("discord.Client.start", new=AsyncMock(return_value=None)),
                patch.object(bot_runtime.app_commands, "CommandTree", side_effect=tree_factory),
            ):
                await bot_runtime.run_discord_bot(Service(), db, config)
            self.assertEqual(
                {command.name for command in trees[0].get_commands()},
                {
                    "status",
                    "menu",
                    "help",
                    "performance",
                    "scan",
                    "compare",
                    "watch",
                    "watchlist",
                    "unwatch",
                    "candidates",
                    "rejections",
                    "missed",
                    "radar",
                    "runners",
                    "failed",
                    "token",
                    "smartmoney",
                    "wallet",
                    "clusters",
                    "creator",
                    "narrative",
                    "setup",
                    "server-settings",
                    "test-alert",
                },
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
