from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4

from memecoin_bot.config import Settings
from memecoin_bot.database import Store
from memecoin_bot.models import (
    DiscoveryEvent, MarketSnapshot, ScoreResult, SignalClass, iso,
)


def settings(path: Path) -> Settings:
    value = Settings(database_path=path)
    value.max_pair_age_minutes = 100_000
    return value


def store(path: Path) -> Store:
    value = Store(path)
    value.migrate()
    return value


@contextmanager
def temp_db_path():
    directory = Path.cwd() / "work" / "test-data"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"test-{uuid4().hex}.db"
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()


def create_signal(db: Store, scoring_version: str = "v1") -> int:
    token_id, _ = db.upsert_discovery(DiscoveryEvent(
        token_address="Token111111111111111111111111111111111111", symbol="TST", name="Test",
    ))
    market = MarketSnapshot(
        token_address="Token111111111111111111111111111111111111",
        captured_at=iso(), source="test", symbol="TST", name="Test",
        market_cap_usd=30_000, price_usd=0.00003, liquidity_usd=20_000,
        volume_5m_usd=25_000,
    )
    score = ScoreResult(
        total=88, component_scores={"narrative": 23, "social": 17, "onchain": 18,
        "developer": 13, "momentum": 12, "safety": 5},
        component_maxima={"narrative": 25, "social": 20, "onchain": 20,
        "developer": 15, "momentum": 15, "safety": 5},
        classification=SignalClass.HIGH_CONVICTION, confidence=1,
        scoring_version=scoring_version,
    )
    signal_id = db.create_signal(
        token_id, market, score,
        {"developer": {}, "narrative": {}, "social": {}, "onchain": {}},
        [], {
            "classification": "HIGH_CONVICTION", "score": 88, "confidence": 1,
            "symbol": "TST", "name": "Test", "token_address": market.token_address,
            "signal_market_cap_usd": 30_000, "liquidity_usd": 20_000,
            "volume_5m_usd": 25_000, "holders": None, "top10_percent": None,
            "bundled_percent": None, "component_scores": score.component_scores,
            "component_maxima": score.component_maxima,
            "developer": {"classification": "UNKNOWN"}, "narrative": {},
            "social": {}, "onchain": {}, "momentum": {}, "risks": [], "thesis": [],
            "signal_timestamp": market.captured_at, "shadow": True,
            "scoring_version": scoring_version,
        }, holder_count=None,
    )
    assert signal_id is not None
    return signal_id
