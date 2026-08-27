from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from memecoin_bot.config import Settings
from memecoin_bot.models import (
    CandidateState,
    DiscoveryEvent,
    MarketSnapshot,
    SafetyAssessment,
    SignalClass,
    iso,
)
from memecoin_bot.momentum import MomentumEngine
from memecoin_bot.scoring import ScoringEngine
from memecoin_bot.service import IntelligenceService
from tests.helpers import settings, store, temp_db_path


class SequenceMarket:
    name = "sequence_market"

    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.index = 0

    async def market_snapshot(self, address, chain="solana"):
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value


class SafeRpc:
    name = "safe_rpc"

    async def safety(self, chain, address):
        return SafetyAssessment(checked_at=iso(), source=self.name, top10_percent=20, holder_count=50)


class EmptyDiscovery:
    async def poll(self):
        return []


class NullNotifier:
    async def send(self, content):
        return None


def snapshot(address: str, mc: float, liquidity: float, volume: float, buys: int, sells: int) -> MarketSnapshot:
    return MarketSnapshot(
        token_address=address, captured_at=iso(), source="fixture", symbol="LIFE", name="Lifecycle",
        pair_created_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
        market_cap_usd=mc, price_usd=mc / 1_000_000_000, liquidity_usd=liquidity,
        volume_5m_usd=volume, buys_5m=buys, sells_5m=sells, price_change_5m=10,
    )


class CandidateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_candidate_path_persists_consumed_v15_evidence(self):
        with temp_db_path() as path:
            config = settings(path)
            address = "V15Production111111111111111111111111111111"
            db = store(path)
            service = IntelligenceService(
                config,
                db,
                EmptyDiscovery(),
                SequenceMarket([snapshot(address, 20_000, 25_000, 4_000, 30, 10)]),
                SafeRpc(),
                NullNotifier(),
            )
            await service.evaluate(DiscoveryEvent(token_address=address, source="test"))

            decision = db.conn.execute("SELECT * FROM v15_decisions").fetchone()
            assert decision is not None
            features = json.loads(decision["feature_vector_json"])
            assert features["survival_quality"] is not None
            assert features["payoff_quality"] is not None
            assert "provider_conflicts" in features and "stale_evidence" in features
            assert db.conn.execute("SELECT COUNT(*) FROM tradeability_v15").fetchone()[0] == 5
            evidence = {
                row[0]
                for row in db.conn.execute(
                    "SELECT field_name FROM provider_evidence_v15 ORDER BY field_name"
                )
            }
            assert evidence == {"market", "safety"}
            candidate = db.candidate_for_token(db.token_id(address))
            assert candidate["runner_score"] == decision["runner_score"]
            assert candidate["failure_score"] == decision["failure_score"]
            db.close()

    async def test_low_liquidity_remains_candidate_and_duplicate_does_not_disable_monitor(self):
        with temp_db_path() as path:
            config = settings(path)
            address = "Lifecycle111111111111111111111111111111111"
            market = SequenceMarket([
                snapshot(address, 14_000, 5_000, 1_000, 10, 10),
                snapshot(address, 18_000, 12_000, 2_000, 18, 10),
                snapshot(address, 24_000, 15_000, 5_000, 35, 12),
            ])
            db = store(path)
            service = IntelligenceService(config, db, EmptyDiscovery(), market, SafeRpc(), NullNotifier())
            event = DiscoveryEvent(token_address=address, source="test")
            self.assertEqual(await service.evaluate(event), CandidateState.PENDING_EVIDENCE)
            self.assertEqual(await service.evaluate(event), "KNOWN_CANDIDATE")
            await service.monitor_candidates_once()
            await service.monitor_candidates_once()
            candidate = db.candidate_for_token(db.token_id(address))
            self.assertGreaterEqual(candidate["snapshot_count"], 3)
            self.assertNotEqual(candidate["state"], CandidateState.REJECTED_UNSAFE)
            db.close()

    async def test_candidate_and_snapshots_survive_store_restart(self):
        with temp_db_path() as path:
            db = store(path)
            token_id, _ = db.upsert_discovery(DiscoveryEvent(token_address="restart"))
            candidate_id, _ = db.ensure_candidate(token_id, iso(), "v1.1")
            snap = snapshot("restart", 20_000, 12_000, 2_000, 20, 10)
            db.save_snapshot(token_id, snap)
            db.update_candidate(candidate_id, CandidateState.PENDING_EVIDENCE, "HISTORY", snap)
            db.close()
            recovered = store(path)
            self.assertEqual(len(recovered.active_candidates()), 1)
            self.assertEqual(len(recovered.recent_snapshots(token_id)), 1)
            recovered.close()


class NormalizedScoringTests(unittest.TestCase):
    def test_unknown_sources_do_not_count_as_zero_or_raise_confidence(self):
        result = ScoringEngine(Settings()).score({
            "narrative": 20, "social": None, "onchain": 16,
            "developer": None, "momentum": 12, "safety": 5,
        }, [])
        self.assertEqual(result.available_weight, 65)
        self.assertEqual(result.confidence, .65)
        self.assertAlmostEqual(result.normalized_score, 81.54, places=2)
        self.assertEqual(result.classification, SignalClass.STRONG)

    def test_high_normalized_score_with_low_confidence_cannot_signal(self):
        result = ScoringEngine(Settings()).score({
            "narrative": None, "social": None, "onchain": 20,
            "developer": None, "momentum": None, "safety": 5,
        }, [])
        self.assertEqual(result.normalized_score, 100)
        self.assertLess(result.confidence, .60)
        self.assertEqual(result.classification, SignalClass.IGNORE)


class MomentumHistoryTests(unittest.TestCase):
    def test_momentum_requires_real_configured_history(self):
        address = "momentum"
        first = snapshot(address, 10_000, 10_000, 1_000, 10, 10)
        second = snapshot(address, 12_000, 11_000, 1_500, 15, 10)
        third = snapshot(address, 16_000, 13_000, 3_000, 30, 10)
        engine = MomentumEngine()
        self.assertIsNone(engine.assess_history(first, [], 3)["score"])
        self.assertIsNone(engine.assess_history(second, [first.to_dict()], 3)["score"])
        self.assertIsNotNone(engine.assess_history(third, [first.to_dict(), second.to_dict()], 3)["score"])


if __name__ == "__main__":
    unittest.main()
