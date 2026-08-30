from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memecoin_bot.models import DiscoveryEvent

from .helpers import create_signal, store


def test_upgraded_database_enforces_wal_and_hardening_indexes(tmp_path):
    db = store(tmp_path / "upgraded.db")
    try:
        assert db.database_integrity() == {
            "quick_check": "ok",
            "foreign_key_violations": 0,
            "journal_mode": "WAL",
            "healthy": True,
        }
        indexes = {
            str(row[0])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
        }
        assert {
            "idx_v15_decisions_candidate_latest",
            "idx_wallet_edge_destination",
            "idx_signals_v15_tier_time",
            "idx_provider_health_state",
        }.issubset(indexes)
    finally:
        db.close()


def test_v15_signal_tier_is_persisted_as_signal_truth(tmp_path):
    db = store(tmp_path / "tier.db")
    try:
        signal_id = create_signal(db)
        assert db.signal(signal_id)["v15_signal_tier"] == "STRONG"
    finally:
        db.close()


def test_qualified_signal_is_terminal_for_staleness_and_reconciles(tmp_path):
    db = store(tmp_path / "reconciliation.db")
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    try:
        token_id, _ = db.upsert_discovery(
            DiscoveryEvent(
                token_address="Terminal111111111111111111111111111111111",
                symbol="TERM",
                name="Terminal",
                discovered_at=old,
            )
        )
        candidate_id, _ = db.ensure_candidate(token_id, old, "v1")
        # Isolated reporting connections intentionally see committed operational
        # truth only. Commit this direct fixture mutation before querying status.
        with db.conn:
            db.conn.execute(
                "UPDATE candidates SET state='QUALIFIED_SIGNAL',"
                "authoritative_state='QUALIFIED_SIGNAL' WHERE id=?",
                (candidate_id,),
            )
        # The missing signal is intentionally exposed as a ghost, while the
        # terminal state is never incorrectly counted as a stale candidate.
        stats = db.status_stats(datetime.now(UTC).isoformat(), candidate_max_age_minutes=1)
        assert stats["stale_beyond_ttl"] == 0
        assert stats["state_reconciliation"]["ghost_qualified"] == 1
        assert stats["status"] == "DEGRADED"
    finally:
        db.close()
