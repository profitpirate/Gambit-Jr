from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from memecoin_bot.config import Settings
from memecoin_bot.discovery import DiscoveryPoller
from memecoin_bot.models import (
    CandidateState,
    DiscoveryEvent,
    MarketSnapshot,
    RadarResult,
    SafetyAssessment,
    ScoreResult,
    SignalClass,
    iso,
)
from memecoin_bot.narratives import NarrativeEngine
from memecoin_bot.providers.base import ProviderError
from memecoin_bot.providers.bsc_rpc import BscRpcProvider
from memecoin_bot.radar import RadarEngine
from memecoin_bot.service import IntelligenceService, fair_chain_sample
from memecoin_bot.signals import format_discord_event, radar_payload
from tests.helpers import settings, store, temp_db_path


def market(address: str, chain: str, mc: float, liq: float, vol: float,
           buys: int, sells: int, change: float = 20) -> MarketSnapshot:
    return MarketSnapshot(
        token_address=address, chain=chain, captured_at=iso(), source="test", symbol="猫AI",
        name="🚀猫AI", pair_address="pair123", pair_created_at=(
            datetime.now(UTC) - timedelta(minutes=2)
        ).isoformat(), price_usd=mc / 1_000_000_000, market_cap_usd=mc,
        liquidity_usd=liq, volume_5m_usd=vol, buys_5m=buys, sells_5m=sells,
        price_change_5m=change,
    )


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.config = Settings()
        self.engine = RadarEngine(self.config)
        self.address = "0x1111111111111111111111111111111111111111"
        self.first_seen = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()

    def test_young_accelerating_token_triggers(self):
        first = market(self.address, "bsc", 10_000, 9_000, 2_000, 10, 10)
        current = market(self.address, "bsc", 16_000, 13_000, 6_000, 35, 10)
        result = self.engine.evaluate(current, [first.to_dict()], self.first_seen, True)
        self.assertTrue(result.triggered)
        self.assertIn("VOLUME_ACCELERATING", result.reasons)

    def test_flat_late_and_unsafe_tokens_do_not_trigger(self):
        first = market(self.address, "bsc", 10_000, 10_000, 2_000, 10, 10)
        flat = market(self.address, "bsc", 10_100, 10_100, 2_050, 10, 10)
        late = market(self.address, "bsc", 50_000, 15_000, 20_000, 50, 5, 900)
        self.assertFalse(self.engine.evaluate(flat, [first.to_dict()], self.first_seen, True).triggered)
        late_result = self.engine.evaluate(late, [first.to_dict()], self.first_seen, True)
        self.assertFalse(late_result.triggered)
        self.assertIn("LATE_VERTICAL_PRICE_MOVE", late_result.penalties)
        self.assertFalse(self.engine.evaluate(flat, [first.to_dict()], self.first_seen, False).triggered)


class SequenceMarket:
    name = "sequence_market"

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0

    async def market_snapshot(self, address, chain="solana"):
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value


class BscSafeRouter:
    name = "bsc_safe"

    async def safety(self, chain, address):
        return SafetyAssessment(checked_at=iso(), source=self.name, chain=chain,
                                warnings=["BSC_OWNER_RENOUNCED", "BSC_HOLDER_CONCENTRATION_UNKNOWN"])


class EmptyDiscovery:
    async def poll(self):
        return []


class NullNotifier:
    async def send(self, content):
        return None


class BscLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bsc_rpc_validity_and_owner_evidence(self):
        class RpcClient:
            async def request(self, url, method="GET", payload=None):
                rpc_method = payload["method"]
                if rpc_method == "eth_getCode":
                    return {"result": "0x6001600055"}
                if rpc_method == "eth_call":
                    return {"result": "0x" + "0" * 64}
                raise AssertionError(rpc_method)

        provider = BscRpcProvider("https://rpc.invalid", RpcClient())
        result = await provider.safety("0x" + "1" * 40)
        self.assertIn("BSC_OWNER_RENOUNCED", result.warnings)
        self.assertEqual(result.chain, "bsc")
        invalid = await provider.safety("not-a-contract")
        self.assertIn("INVALID_BSC_CONTRACT_ADDRESS", invalid.rejection_reasons)

    async def test_bsc_unknown_concentration_reaches_radar_but_not_qualified_signal(self):
        with temp_db_path() as path:
            config = settings(path)
            config.radar_min_conditions = 3
            address = "0x2222222222222222222222222222222222222222"
            snapshots = [
                market(address, "bsc", 11_000, 9_000, 2_000, 10, 10),
                market(address, "bsc", 17_000, 13_000, 6_000, 35, 10),
                market(address, "bsc", 25_000, 18_000, 15_000, 80, 15),
            ]
            db = store(path)
            service = IntelligenceService(
                config, db, EmptyDiscovery(), SequenceMarket(snapshots), BscSafeRouter(), NullNotifier()
            )
            event = DiscoveryEvent(token_address=address, chain="bsc", symbol="猫AI", name="🚀猫AI",
                                   source="geckoterminal_bsc_new_pools",
                                   metadata={"description": "未来AI 猫猫"})
            await service.evaluate(event)
            await service.monitor_candidates_once()
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM radar_events").fetchone()[0], 1)
            await service.monitor_candidates_once()
            token_id = db.token_id(address, "bsc")
            candidate = db.candidate_for_token(token_id)
            self.assertEqual(candidate["state"], CandidateState.EARLY_RADAR)
            self.assertIsNotNone(candidate["radar_triggered_at"])
            signal = db.conn.execute("SELECT * FROM signals WHERE token_id=?", (token_id,)).fetchone()
            self.assertIsNone(signal)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM radar_events").fetchone()[0], 1)
            db.close()


class SourceAndIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_discovery_cycle_limit_is_fair_across_chains(self):
        events = [
            *[DiscoveryEvent(token_address=f"sol-{i}", chain="solana") for i in range(5)],
            *[DiscoveryEvent(token_address=f"bsc-{i}", chain="bsc") for i in range(5)],
        ]
        selected = fair_chain_sample(events, 4)
        self.assertEqual([event.chain for event in selected], ["solana", "bsc", "solana", "bsc"])

    async def test_cross_source_dedupe_and_failure_isolation(self):
        event_a = DiscoveryEvent(token_address="same", chain="solana", source="launch")
        event_b = DiscoveryEvent(token_address="same", chain="solana", source="new_pair")

        class Feed:
            def __init__(self, value=None, fail=False): self.value, self.fail = value, fail
            async def discover(self):
                if self.fail: raise ProviderError("offline")
                return [self.value]

        poller = DiscoveryPoller([Feed(fail=True), Feed(event_a), Feed(event_b)])
        values = await poller.poll()
        self.assertEqual(len(values), 1)
        self.assertIn("new_pair", values[0].metadata["additional_sources"])

    def test_same_address_on_two_chains_is_distinct_and_sources_persist(self):
        with temp_db_path() as path:
            db = store(path)
            sol, _ = db.upsert_discovery(DiscoveryEvent(token_address="same", chain="solana", source="launch"))
            bsc, _ = db.upsert_discovery(DiscoveryEvent(token_address="same", chain="bsc", source="bnb_pair"))
            self.assertNotEqual(sol, bsc)
            db.upsert_discovery(DiscoveryEvent(token_address="same", chain="solana", source="boost"))
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM discovery_sources WHERE token_id=?", (sol,)
            ).fetchone()[0], 2)
            db.close()


class UnicodeAndDiscordTests(unittest.TestCase):
    def test_unicode_metadata_persists_and_narrative_is_unicode_safe(self):
        with temp_db_path() as path:
            db = store(path)
            names = ["猫猫", "柴犬王", "未来AI", "고양이", "ねこ", "🚀猫AI"]
            for index, name in enumerate(names):
                db.upsert_discovery(DiscoveryEvent(
                    token_address=f"unicode-{index}", name=name, symbol=name, source="test",
                    metadata={"description": name},
                ))
            persisted = [r[0] for r in db.conn.execute("SELECT name FROM tokens ORDER BY id")]
            self.assertEqual(persisted, names)
            result = NarrativeEngine().assess(
                DiscoveryEvent(token_address="u", name="未来AI", metadata={"description": "猫猫"}),
                market("u", "solana", 10_000, 10_000, 1_000, 10, 5),
            )
            self.assertIsNotNone(result["score"])
            db.close()

    def test_discord_cards_have_full_ca_chain_and_valid_buttons(self):
        address = "0x3333333333333333333333333333333333333333"
        snap = market(address, "bsc", 20_000, 12_000, 5_000, 30, 10)
        payload = radar_payload(
            DiscoveryEvent(token_address=address, chain="bsc"), snap,
            RadarResult(True, 72, ["VOLUME_ACCELERATING"], []), 3,
        )
        card = format_discord_event("EARLY_RADAR", payload)
        self.assertIn(address, card["content"])
        self.assertEqual(card["embeds"][0]["fields"][0]["value"], "BNB CHAIN")
        urls = [
            button["url"]
            for row in card["components"]
            for button in row["components"]
            if "url" in button
        ]
        self.assertTrue(all(url.startswith("https://") for url in urls))
        self.assertTrue(any("bscscan.com/token/" in url for url in urls))

    def test_all_automatic_event_cards_keep_full_contract_and_chain(self):
        address = "0x4444444444444444444444444444444444444444"
        base = {"token_address": address, "chain": "bsc", "pair_address": "pair", "symbol": "猫AI"}
        payloads = {
            "SIGNAL": dict(base, classification="WATCH", score=44, normalized_score=68,
                           confidence=.65, name="猫AI", signal_market_cap_usd=20_000,
                           liquidity_usd=12_000, volume_5m_usd=5_000, holders=None,
                           top10_percent=None, bundled_percent=None,
                           component_scores={"narrative": 6, "social": 0, "onchain": 18,
                                             "developer": 0, "momentum": 15, "safety": 5},
                           component_maxima=Settings().weights, developer={}, narrative={}, social={},
                           onchain={}, momentum={}, risks=[], thesis=[], signal_timestamp=iso(),
                           shadow=True, scoring_version="v1.2"),
            "MILESTONE": dict(base, milestone=5, seconds_to_hit=120, signal_market_cap_usd=20_000,
                              market_cap_usd=100_000, max_multiple=5),
            "FAILED": dict(base, signal_market_cap_usd=20_000, max_multiple=1.2,
                           current_multiple=.3, max_drawdown=-.7),
            "UPGRADE": dict(base, previous_class="WATCH", new_class="STRONG",
                            normalized_score=78, confidence=.7),
            "DETERIORATION": dict(base, normalized_score=54, reasons=["VOLUME_DYING"]),
        }
        for event_type, payload in payloads.items():
            with self.subTest(event_type=event_type):
                card = format_discord_event(event_type, payload)
                self.assertIn(address, card["content"])
                chain_field = next(
                    field
                    for field in card["embeds"][0]["fields"]
                    if field["name"] == "Chain"
                )
                self.assertEqual(chain_field["value"], "BNB CHAIN")
                self.assertLessEqual(len(card["embeds"][0]["description"]), 4096)


class MissedRunnerTests(unittest.TestCase):
    def test_outcomes_distinguish_complete_miss_and_radar_catch(self):
        with temp_db_path() as path:
            db = store(path)
            for address, radar in (("missed", False), ("radar-catch", True)):
                token_id, _ = db.upsert_discovery(DiscoveryEvent(token_address=address, source="test"))
                candidate_id, _ = db.ensure_candidate(token_id, iso(), "v1.2")
                first = market(address, "solana", 10_000, 10_000, 1_000, 10, 10)
                peak = market(address, "solana", 100_000, 20_000, 20_000, 100, 20)
                db.save_snapshot(token_id, first)
                if radar:
                    self.assertTrue(db.trigger_radar(candidate_id, 70, ["VOLUME_ACCELERATING"], first,
                                                    {"token_address": address, "chain": "solana"}))
                    self.assertFalse(db.trigger_radar(candidate_id, 70, ["VOLUME_ACCELERATING"], first,
                                                     {"token_address": address, "chain": "solana"}))
                db.save_snapshot(token_id, peak)
                db.update_candidate(candidate_id, CandidateState.EXPIRED, "MOMENTUM_NEVER_MATURED", peak, expired=True)
            missed = db.missed_report(None, 5)
            self.assertEqual(len(missed), 2)
            coverage = db.coverage(None, 10)
            self.assertEqual(coverage["major_runners_discovered"], 2)
            self.assertEqual(coverage["major_runners_radar"], 1)
            self.assertEqual(coverage["major_runners_completely_missed"], 1)
            db.close()
            recovered = store(path)
            self.assertEqual(len(recovered.missed_report(None, 5)), 2)
            self.assertEqual(recovered.conn.execute("SELECT COUNT(*) FROM radar_events").fetchone()[0], 1)
            recovered.close()

    def test_signal_after_threshold_is_still_counted_as_missed(self):
        with temp_db_path() as path:
            db = store(path)
            address = "late-signal"
            token_id, _ = db.upsert_discovery(DiscoveryEvent(token_address=address, source="test"))
            _candidate_id, _ = db.ensure_candidate(token_id, iso(), "v1.2")
            first = market(address, "solana", 10_000, 10_000, 1_000, 10, 10)
            peak = market(address, "solana", 100_000, 20_000, 20_000, 100, 20)
            db.save_snapshot(token_id, first)
            db.save_snapshot(token_id, peak)
            score = ScoreResult(
                total=10, component_scores={"narrative": 10},
                component_maxima={"narrative": 10}, classification=SignalClass.WATCH,
                confidence=1, scoring_version="v1.2", normalized_score=100,
                available_weight=10,
            )
            db.create_signal(token_id, peak, score, {}, [], {})
            self.assertEqual(len(db.missed_report(None, 10)), 1)
            coverage = db.coverage(None, 10)
            self.assertEqual(coverage["major_runners_signalled"], 0)
            self.assertEqual(coverage["major_runners_completely_missed"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
