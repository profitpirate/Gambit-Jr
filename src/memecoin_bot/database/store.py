from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from memecoin_bot.models import CandidateState, DiscoveryEvent, MarketSnapshot, ScoreResult, iso


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


class Store:
    """SQLite persistence with transactional milestone and Discord outbox writes."""

    def __init__(self, path: str | Path, migrations_dir: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        # Migration 001 only establishes WAL for a clean database. Enforce the
        # same production contract when opening an upgraded database.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrations_dir = Path(migrations_dir) if migrations_dir else self._find_migrations()

    def _find_migrations(self) -> Path:
        candidates = [Path.cwd() / "migrations", Path(__file__).resolve().parents[3] / "migrations"]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError("migrations directory not found")

    def migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
            for file in sorted(self.migrations_dir.glob("*.sql")):
                if file.name in applied:
                    continue
                script = file.read_text(encoding="utf-8")

                def skip_existing_column(match: re.Match[str]) -> str:
                    table, column = match.group(1), match.group(2)
                    existing = {
                        str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")
                    }
                    return "" if column in existing else match.group(0)

                script = re.sub(
                    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+[^;]+;",
                    skip_existing_column,
                    script,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                self.conn.executescript(script)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (file.name, iso()),
                )

    def close(self) -> None:
        self.conn.close()

    def register_scoring_version(
        self, version: str, weights: dict[str, float], thresholds: dict[str, float]
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scoring_versions VALUES (?, ?, ?, ?)",
                (version, _json(weights), _json(thresholds), iso()),
            )

    def register_config_fingerprint(
        self,
        fingerprint: str,
        software_version: str,
        scoring_version: str,
        radar_version: str,
        config: dict[str, Any],
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO config_fingerprints VALUES(?,?,?,?,?,?)",
                (
                    fingerprint,
                    software_version,
                    scoring_version,
                    radar_version,
                    _json(config),
                    iso(),
                ),
            )

    def upsert_discovery(self, event: DiscoveryEvent) -> tuple[int, bool]:
        """Return token id and whether it was newly inserted."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO tokens(chain,token_address,symbol,name,source,"
                "first_discovered_at,estimated_created_at,pair_address,deployer,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event.chain,
                    event.token_address,
                    event.symbol,
                    event.name,
                    event.source,
                    event.discovered_at,
                    event.estimated_creation_timestamp,
                    event.pair_address,
                    event.deployer,
                    _json(event.metadata),
                ),
            )
            created = cursor.rowcount == 1
            row = self.conn.execute(
                "SELECT id FROM tokens WHERE chain=? AND token_address=?",
                (event.chain, event.token_address),
            ).fetchone()
            assert row is not None
            if not created:
                self.conn.execute(
                    "UPDATE tokens SET symbol=COALESCE(symbol,?), name=COALESCE(name,?), "
                    "pair_address=COALESCE(pair_address,?) WHERE id=?",
                    (event.symbol, event.name, event.pair_address, row["id"]),
                )
            description = event.metadata.get("description")
            if description is not None:
                self.conn.execute(
                    "UPDATE tokens SET original_description=COALESCE(original_description,?) WHERE id=?",
                    (str(description), row["id"]),
                )
            self.conn.execute(
                "INSERT INTO discovery_sources(token_id,source,first_seen_at,last_seen_at,metadata_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(token_id,source) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,metadata_json=excluded.metadata_json",
                (
                    row["id"],
                    event.source,
                    event.discovered_at,
                    event.discovered_at,
                    _json(event.metadata),
                ),
            )
            for extra_source in event.metadata.get("additional_sources") or []:
                self.conn.execute(
                    "INSERT INTO discovery_sources(token_id,source,first_seen_at,last_seen_at,metadata_json) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(token_id,source) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (row["id"], str(extra_source), event.discovered_at, event.discovered_at, "{}"),
                )
            return int(row["id"]), created

    def token_id(self, address: str, chain: str = "solana") -> int | None:
        row = self.conn.execute(
            "SELECT id FROM tokens WHERE chain=? AND token_address=?", (chain, address)
        ).fetchone()
        return int(row[0]) if row else None

    def has_evaluation(self, token_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM evaluations WHERE token_id=? LIMIT 1", (token_id,)
            ).fetchone()
            is not None
        )

    def save_snapshot(
        self, token_id: int, snapshot: MarketSnapshot, holder_count: int | None = None
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO token_snapshots(token_id,captured_at,source,market_cap_usd,price_usd,"
                "liquidity_usd,volume_5m_usd,holder_count,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    snapshot.captured_at,
                    snapshot.source,
                    snapshot.market_cap_usd,
                    snapshot.price_usd,
                    snapshot.liquidity_usd,
                    snapshot.volume_5m_usd,
                    holder_count,
                    _json(snapshot.to_dict()),
                ),
            )
            if snapshot.market_cap_usd is not None and snapshot.market_cap_usd > 0:
                now = snapshot.captured_at
                self.conn.execute(
                    "INSERT INTO token_outcomes(token_id,discovery_market_cap_usd,peak_market_cap_usd,"
                    "max_multiple_from_discovery,first_market_at,last_observed_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(token_id) DO UPDATE SET "
                    "peak_market_cap_usd=MAX(COALESCE(token_outcomes.peak_market_cap_usd,0),excluded.peak_market_cap_usd),"
                    "max_multiple_from_discovery=MAX(COALESCE(token_outcomes.max_multiple_from_discovery,1),"
                    "excluded.peak_market_cap_usd/token_outcomes.discovery_market_cap_usd),"
                    "last_observed_at=excluded.last_observed_at,updated_at=excluded.updated_at",
                    (
                        token_id,
                        snapshot.market_cap_usd,
                        snapshot.market_cap_usd,
                        1.0,
                        now,
                        now,
                        now,
                    ),
                )
                outcome = self.conn.execute(
                    "SELECT discovery_market_cap_usd,max_multiple_from_discovery,radar_occurred,signal_id "
                    "FROM token_outcomes WHERE token_id=?",
                    (token_id,),
                ).fetchone()
                for multiple in (2, 3, 5, 10, 20, 50, 100):
                    if float(outcome["max_multiple_from_discovery"] or 0) >= multiple:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO outcome_milestones(token_id,multiple,hit_at,market_cap_usd,"
                            "radar_before_hit,signal_before_hit) VALUES(?,?,?,?,?,?)",
                            (
                                token_id,
                                multiple,
                                now,
                                snapshot.market_cap_usd,
                                int(bool(outcome["radar_occurred"])),
                                int(outcome["signal_id"] is not None),
                            ),
                        )
                radar_event = self.conn.execute(
                    "SELECT r.id,r.market_cap_usd,r.liquidity_usd FROM radar_events r "
                    "JOIN candidates c ON c.id=r.candidate_id WHERE c.token_id=? ORDER BY r.triggered_at LIMIT 1",
                    (token_id,),
                ).fetchone()
                if radar_event and radar_event["market_cap_usd"] and snapshot.market_cap_usd:
                    current_multiple = snapshot.market_cap_usd / radar_event["market_cap_usd"]
                    liquidity_drop = (
                        1 - snapshot.liquidity_usd / radar_event["liquidity_usd"]
                        if snapshot.liquidity_usd is not None and radar_event["liquidity_usd"]
                        else 0
                    )
                    status = (
                        "PROBABLE_RUG"
                        if liquidity_drop >= 0.9 and current_multiple <= 0.2
                        else "RUNNER_5X"
                        if current_multiple >= 5
                        else "RUNNER_2X"
                        if current_multiple >= 2
                        else "TRACKING"
                    )
                    first_2x = now if current_multiple >= 2 else None
                    failed_at = now if status == "PROBABLE_RUG" else None
                    self.conn.execute(
                        "INSERT INTO radar_outcomes(radar_event_id,peak_multiple,current_multiple,status,first_2x_at,failed_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(radar_event_id) DO UPDATE SET "
                        "peak_multiple=MAX(COALESCE(radar_outcomes.peak_multiple,0),excluded.peak_multiple),"
                        "current_multiple=excluded.current_multiple,status=excluded.status,"
                        "first_2x_at=COALESCE(radar_outcomes.first_2x_at,excluded.first_2x_at),"
                        "failed_at=COALESCE(radar_outcomes.failed_at,excluded.failed_at),updated_at=excluded.updated_at",
                        (
                            radar_event["id"],
                            current_multiple,
                            current_multiple,
                            status,
                            first_2x,
                            failed_at,
                            now,
                        ),
                    )
                    token = self.conn.execute(
                        "SELECT chain,token_address,symbol,name,pair_address FROM tokens WHERE id=?",
                        (token_id,),
                    ).fetchone()
                    base_payload = {
                        "classification": "EARLY_RADAR",
                        "priority": "HOT",
                        "market_cap_usd": snapshot.market_cap_usd,
                        "liquidity_usd": snapshot.liquidity_usd,
                        "chain": token["chain"],
                        "token_address": token["token_address"],
                        "symbol": token["symbol"],
                        "name": token["name"],
                        "pair_address": token["pair_address"],
                    }
                    for multiple in (2, 5):
                        if current_multiple >= multiple:
                            self.conn.execute(
                                "INSERT OR IGNORE INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                                (
                                    f"radar-milestone:{radar_event['id']}:{multiple:g}",
                                    "RADAR_MILESTONE",
                                    _json(
                                        dict(
                                            base_payload,
                                            milestone=multiple,
                                            current_multiple=current_multiple,
                                        )
                                    ),
                                    now,
                                ),
                            )
                    if status == "PROBABLE_RUG":
                        self.conn.execute(
                            "INSERT OR IGNORE INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                            (
                                f"radar-risk:{radar_event['id']}:probable-rug",
                                "RADAR_RISK",
                                _json(
                                    dict(
                                        base_payload,
                                        risk="PROBABLE_RUG",
                                        current_multiple=current_multiple,
                                        liquidity_drop=liquidity_drop,
                                    )
                                ),
                                now,
                            ),
                        )
            return int(cur.lastrowid)

    def recent_snapshots(self, token_id: int, limit: int = 3) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM token_snapshots WHERE token_id=? ORDER BY id DESC LIMIT ?",
                (token_id, limit),
            )
        )

    def ensure_candidate(
        self, token_id: int, discovered_at: str, scoring_version: str
    ) -> tuple[int, bool]:
        now = iso()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO candidates(token_id,state,reason,first_discovered_at,"
                "scoring_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    token_id,
                    CandidateState.DISCOVERED,
                    "AWAITING_INITIAL_SCREEN",
                    discovered_at,
                    scoring_version,
                    now,
                    now,
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM candidates WHERE token_id=?", (token_id,)
            ).fetchone()
            assert row
            candidate_id = int(row[0])
            if cur.rowcount == 1:
                self.conn.execute(
                    "INSERT INTO candidate_transitions(candidate_id,to_state,reason,created_at) VALUES(?,?,?,?)",
                    (candidate_id, CandidateState.DISCOVERED, "DISCOVERY", now),
                )
            return candidate_id, cur.rowcount == 1

    def candidate_for_token(self, token_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM candidates WHERE token_id=?", (token_id,)
        ).fetchone()

    def active_candidates(
        self,
        limit: int = 250,
        per_chain: int | None = None,
        fresh_reserved: int = 0,
        radar_reserved: int = 0,
        near_signal_reserved: int = 0,
        now: str | None = None,
        genesis_reserved: int = 0,
        priority_reserved: int = 0,
    ) -> list[sqlite3.Row]:
        """Return due candidates using lane quotas and round-robin chain fairness.

        Reservations are floors, not hard ceilings. Unused slots flow to the other
        lanes, while a large retry backlog can never consume reserved fresh capacity.
        """
        if limit <= 0:
            return []
        now = now or iso()
        chain_limit = per_chain or limit
        rows = list(
            self.conn.execute(
                "SELECT c.*,t.token_address,t.symbol,t.name,t.pair_address,t.chain,t.metadata_json,"
                "t.first_discovered_at token_discovered_at FROM candidates c "
                "JOIN tokens t ON t.id=c.token_id WHERE c.state NOT IN (?,?) "
                "AND (c.next_retry_at IS NULL OR c.next_retry_at<=?) "
                "AND (c.next_monitor_at IS NULL OR c.next_monitor_at<=?)",
                (
                    str(CandidateState.REJECTED_UNSAFE),
                    str(CandidateState.EXPIRED),
                    now,
                    now,
                ),
            )
        )
        rows = [row for row in rows if row["state"] != str(CandidateState.SIGNALLED)]

        def lane(row: sqlite3.Row) -> str:
            authoritative = row["authoritative_state"]
            if authoritative == "PRIORITY_RADAR":
                return "PRIORITY"
            if authoritative == "HOT_RADAR":
                return "HOT"
            if authoritative == "GENESIS_RADAR":
                return "GENESIS"
            if row["radar_triggered_at"] is not None:
                return "RADAR"
            if int(row["attempt_count"] or 0) == 0:
                return "FRESH"
            if float(row["confidence"] or 0) >= 0.45 or float(row["normalized_score"] or 0) >= 60:
                return "NEAR_SIGNAL"
            if row["state"] in (str(CandidateState.CANDIDATE), str(CandidateState.SCREENING)):
                return "ACTIVE"
            if int(row["consecutive_missing_pair_count"] or 0) >= 5:
                return "LOW_PRIORITY_RETRY"
            return "PENDING_RETRY"

        lanes: dict[str, list[sqlite3.Row]] = {
            name: []
            for name in (
                "FRESH",
                "GENESIS",
                "HOT",
                "PRIORITY",
                "RADAR",
                "NEAR_SIGNAL",
                "ACTIVE",
                "PENDING_RETRY",
                "LOW_PRIORITY_RETRY",
            )
        }
        for row in rows:
            name = lane(row)
            lanes[name].append(row)
            if row["scheduling_lane"] != name:
                self.conn.execute(
                    "UPDATE candidates SET scheduling_lane=? WHERE id=?", (name, row["id"])
                )
        for queue in lanes.values():
            queue.sort(key=lambda r: (r["last_attempted_at"] or r["first_discovered_at"], r["id"]))

        selected: list[sqlite3.Row] = []
        selected_ids: set[int] = set()
        chain_counts: dict[str, int] = {}

        def take(names: tuple[str, ...], amount: int) -> None:
            while amount > 0 and len(selected) < limit:
                progressed = False
                chains = sorted(
                    {
                        str(r["chain"])
                        for name in names
                        for r in lanes[name]
                        if int(r["id"]) not in selected_ids
                    }
                )
                for chain in chains:
                    if amount <= 0 or len(selected) >= limit:
                        break
                    if chain_counts.get(chain, 0) >= chain_limit:
                        continue
                    candidate = next(
                        (
                            r
                            for name in names
                            for r in lanes[name]
                            if str(r["chain"]) == chain and int(r["id"]) not in selected_ids
                        ),
                        None,
                    )
                    if candidate is None:
                        continue
                    selected.append(candidate)
                    selected_ids.add(int(candidate["id"]))
                    chain_counts[chain] = chain_counts.get(chain, 0) + 1
                    amount -= 1
                    progressed = True
                if not progressed:
                    break

        take(("FRESH",), min(fresh_reserved, limit))
        take(("GENESIS",), min(genesis_reserved, limit - len(selected)))
        take(("PRIORITY",), min(priority_reserved, limit - len(selected)))
        take(("RADAR",), min(radar_reserved, limit - len(selected)))
        take(("NEAR_SIGNAL",), min(near_signal_reserved, limit - len(selected)))
        take(
            (
                "FRESH",
                "GENESIS",
                "HOT",
                "PRIORITY",
                "RADAR",
                "NEAR_SIGNAL",
                "ACTIVE",
                "PENDING_RETRY",
                "LOW_PRIORITY_RETRY",
            ),
            limit - len(selected),
        )
        return selected

    def begin_candidate_attempt(self, candidate_id: int, attempted_at: str | None = None) -> int:
        """Persist an attempt before any provider call or early return."""
        attempted_at = attempted_at or iso()
        with self._lock, self.conn:
            previous = self.conn.execute(
                "SELECT last_attempted_at FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not previous:
                raise KeyError(candidate_id)
            if previous[0] and datetime.fromisoformat(attempted_at) <= datetime.fromisoformat(
                str(previous[0])
            ):
                attempted_at = (
                    datetime.fromisoformat(str(previous[0])) + timedelta(microseconds=1)
                ).isoformat()
            self.conn.execute(
                "UPDATE candidates SET last_attempted_at=?,attempt_count=attempt_count+1,"
                "pending_since=COALESCE(pending_since,?),updated_at=? WHERE id=?",
                (attempted_at, attempted_at, attempted_at, candidate_id),
            )
            row = self.conn.execute(
                "SELECT attempt_count FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            return int(row[0])

    def schedule_candidate_retry(
        self,
        candidate_id: int,
        reason: str,
        retry_class: str,
        initial_seconds: float = 30,
        max_seconds: float = 900,
        multiplier: float = 2,
        now: str | None = None,
    ) -> float:
        now_dt = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT consecutive_missing_pair_count,consecutive_provider_failure_count,reason,"
                "last_attempted_at "
                "FROM candidates WHERE id=?",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise KeyError(candidate_id)
            if row[3]:
                last_attempted = datetime.fromisoformat(str(row[3]))
                if last_attempted.tzinfo is None:
                    last_attempted = last_attempted.replace(tzinfo=UTC)
                now_dt = max(now_dt, last_attempted)
            missing = int(row[0] or 0) + int(retry_class == "MISSING_PAIR")
            provider = int(row[1] or 0) + int(retry_class == "PROVIDER")
            count = missing if retry_class == "MISSING_PAIR" else provider
            delay = min(max_seconds, initial_seconds * (multiplier ** max(0, count - 1)))
            next_retry = (now_dt + timedelta(seconds=delay)).isoformat()
            self.conn.execute(
                "UPDATE candidates SET previous_reason=reason,reason=?,lifecycle_reason=?,retry_class=?,"
                "next_retry_at=?,consecutive_missing_pair_count=?,consecutive_pair_missing=?,"
                "consecutive_provider_failure_count=?,consecutive_provider_failures=?,retry_count=retry_count+1,"
                "scheduling_lane=?,updated_at=? WHERE id=?",
                (
                    reason,
                    reason,
                    retry_class,
                    next_retry,
                    missing,
                    missing,
                    provider,
                    provider,
                    "LOW_PRIORITY_RETRY" if count >= 5 else "PENDING_RETRY",
                    now_dt.isoformat(),
                    candidate_id,
                ),
            )
            return delay

    def record_candidate_snapshot_success(self, candidate_id: int, captured_at: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE candidates SET last_successful_snapshot_at=?,next_retry_at=NULL,retry_class=NULL,"
                "consecutive_missing_pair_count=0,consecutive_pair_missing=0,"
                "consecutive_provider_failure_count=0,consecutive_provider_failures=0,pending_since=NULL "
                "WHERE id=?",
                (captured_at, candidate_id),
            )

    def record_confidence_context(
        self,
        candidate_id: int,
        recorded_at: str,
        score: float,
        confidence: float,
        convergence: dict[str, Any],
        setup: dict[str, Any],
        reason: str,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO confidence_history(candidate_id,recorded_at,normalized_score,confidence,"
                "convergence_score,setup_quality,reason) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id,recorded_at) DO UPDATE SET normalized_score=excluded.normalized_score,"
                "confidence=excluded.confidence,convergence_score=excluded.convergence_score,"
                "setup_quality=excluded.setup_quality,reason=excluded.reason",
                (
                    candidate_id,
                    recorded_at,
                    score,
                    confidence,
                    convergence.get("score"),
                    setup.get("grade"),
                    reason,
                ),
            )

    def record_narrative_event(
        self, token_id: int, event_key: str, context: dict[str, Any]
    ) -> bool:
        now = iso()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO narrative_events(token_id,event_key,detected_at,narrative,freshness,"
                "saturation,quality,evidence_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    event_key,
                    now,
                    context.get("identity", "UNKNOWN"),
                    context.get("freshness", "UNKNOWN"),
                    context.get("saturation", "UNKNOWN"),
                    context.get("quality", "UNKNOWN"),
                    _json(context),
                ),
            )
            return cur.rowcount == 1

    def reconcile_stale_candidates(self, max_age_minutes: float, now: str | None = None) -> int:
        """Expire legacy active rows beyond TTL exactly once, retaining all evidence/history."""
        now_dt = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        cutoff = (now_dt - timedelta(minutes=max_age_minutes)).isoformat()
        key = f"v131-stale:{cutoff}"
        with self._lock, self.conn:
            prior = self.conn.execute(
                "SELECT expired_count FROM reconciliation_runs WHERE reconciliation_key=?", (key,)
            ).fetchone()
            if prior:
                return int(prior[0])
            stale = list(
                self.conn.execute(
                    "SELECT id,state,reason,normalized_score,confidence FROM candidates "
                    "WHERE state NOT IN (?,?,?) AND first_discovered_at<?",
                    (
                        str(CandidateState.REJECTED_UNSAFE),
                        str(CandidateState.EXPIRED),
                        str(CandidateState.SIGNALLED),
                        cutoff,
                    ),
                )
            )
            for row in stale:
                self.conn.execute(
                    "UPDATE candidates SET previous_reason=reason,state=?,authoritative_state='EXPIRED',"
                    "reason=?,lifecycle_reason=?,"
                    "next_retry_at=NULL,expired_at=?,updated_at=? WHERE id=?",
                    (
                        str(CandidateState.EXPIRED),
                        "STALE_PENDING_RECONCILIATION",
                        "STALE_PENDING_RECONCILIATION",
                        now_dt.isoformat(),
                        now_dt.isoformat(),
                        row["id"],
                    ),
                )
                self.conn.execute(
                    "INSERT INTO candidate_transitions(candidate_id,from_state,to_state,reason,score,confidence,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["state"],
                        str(CandidateState.EXPIRED),
                        "STALE_PENDING_RECONCILIATION",
                        row["normalized_score"],
                        row["confidence"],
                        now_dt.isoformat(),
                    ),
                )
            self.conn.execute(
                "INSERT INTO reconciliation_runs(reconciliation_key,started_at,completed_at,expired_count) "
                "VALUES(?,?,?,?)",
                (key, now_dt.isoformat(), now_dt.isoformat(), len(stale)),
            )
            return len(stale)

    def trigger_radar(
        self,
        candidate_id: int,
        score: float,
        reasons: list[str],
        snapshot: MarketSnapshot,
        payload: dict[str, Any],
        level: str = "EARLY_RADAR",
        software_version: str = "1.3.1",
        radar_version: str = "v1.3.1-radar",
        config_fingerprint: str | None = None,
    ) -> bool:
        now = snapshot.captured_at
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO radar_events(candidate_id,event_level,triggered_at,radar_score,"
                "reason_json,market_cap_usd,price_usd,liquidity_usd,snapshot_count,immutable_payload_json,"
                "software_version,radar_version,config_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,(SELECT COUNT(*) FROM token_snapshots WHERE token_id="
                "(SELECT token_id FROM candidates WHERE id=?)),?,?,?,?)",
                (
                    candidate_id,
                    level,
                    now,
                    score,
                    _json(reasons),
                    snapshot.market_cap_usd,
                    snapshot.price_usd,
                    snapshot.liquidity_usd,
                    candidate_id,
                    _json(payload),
                    software_version,
                    radar_version,
                    config_fingerprint,
                ),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "UPDATE candidates SET state='EARLY_RADAR',radar_score=?,radar_reason=?,radar_triggered_at=?,"
                "radar_market_cap_usd=?,radar_price_usd=?,radar_liquidity_usd=?,updated_at=? WHERE id=?",
                (
                    score,
                    ";".join(reasons),
                    now,
                    snapshot.market_cap_usd,
                    snapshot.price_usd,
                    snapshot.liquidity_usd,
                    now,
                    candidate_id,
                ),
            )
            token_id = self.conn.execute(
                "SELECT token_id FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE token_outcomes SET radar_occurred=1,updated_at=? WHERE token_id=?",
                (now, token_id),
            )
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"radar:{candidate_id}:{level}", "EARLY_RADAR", _json(payload), now),
            )
            radar_event_id = self.conn.execute(
                "SELECT id FROM radar_events WHERE candidate_id=? AND event_level=?",
                (candidate_id, level),
            ).fetchone()[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO radar_outcomes(radar_event_id,peak_multiple,current_multiple,status,updated_at) "
                "VALUES(?,1,1,'TRACKING',?)",
                (radar_event_id, now),
            )
            return True

    def update_candidate(
        self,
        candidate_id: int,
        state: CandidateState | str,
        reason: str,
        snapshot: MarketSnapshot | None = None,
        score: ScoreResult | None = None,
        hard_rejections: list[str] | None = None,
        waiting_reasons: list[str] | None = None,
        unknown_fields: list[str] | None = None,
        signal_id: int | None = None,
        expired: bool = False,
    ) -> None:
        now = snapshot.captured_at if snapshot else iso()
        with self._lock, self.conn:
            old = self.conn.execute(
                "SELECT state FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not old:
                raise KeyError(candidate_id)
            values = {
                "state": str(state),
                "reason": reason,
                "updated_at": now,
                "last_monitored_at": now if snapshot else None,
                "last_evaluated_at": now if score else None,
                "raw_points": score.total if score else None,
                "normalized_score": score.normalized_score if score else None,
                "confidence": score.confidence if score else None,
                "classification": str(score.classification) if score else None,
                "hard_rejections_json": _json(hard_rejections or []),
                "waiting_reasons_json": _json(waiting_reasons or []),
                "unknown_fields_json": _json(unknown_fields or []),
                "signal_id": signal_id,
                "expired_at": now if expired else None,
            }
            if snapshot:
                values.update(
                    {
                        "initial_market_cap_usd": snapshot.market_cap_usd,
                        "current_market_cap_usd": snapshot.market_cap_usd,
                        "initial_liquidity_usd": snapshot.liquidity_usd,
                        "current_liquidity_usd": snapshot.liquidity_usd,
                        "initial_price_usd": snapshot.price_usd,
                        "current_price_usd": snapshot.price_usd,
                        "initial_volume_5m_usd": snapshot.volume_5m_usd,
                        "current_volume_5m_usd": snapshot.volume_5m_usd,
                        "current_buys_5m": snapshot.buys_5m,
                        "current_sells_5m": snapshot.sells_5m,
                    }
                )
            assignments = [
                "state=:state",
                "reason=:reason",
                "updated_at=:updated_at",
                "hard_rejections_json=:hard_rejections_json",
                "waiting_reasons_json=:waiting_reasons_json",
                "unknown_fields_json=:unknown_fields_json",
            ]
            for key in (
                "last_monitored_at",
                "last_evaluated_at",
                "raw_points",
                "normalized_score",
                "confidence",
                "classification",
                "signal_id",
                "expired_at",
            ):
                if values[key] is not None:
                    assignments.append(f"{key}=:{key}")
            if snapshot:
                assignments += [
                    "first_evaluated_at=COALESCE(first_evaluated_at,:last_evaluated_at)",
                    "initial_market_cap_usd=COALESCE(initial_market_cap_usd,:initial_market_cap_usd)",
                    "current_market_cap_usd=:current_market_cap_usd",
                    "initial_liquidity_usd=COALESCE(initial_liquidity_usd,:initial_liquidity_usd)",
                    "current_liquidity_usd=:current_liquidity_usd",
                    "initial_price_usd=COALESCE(initial_price_usd,:initial_price_usd)",
                    "current_price_usd=:current_price_usd",
                    "initial_volume_5m_usd=COALESCE(initial_volume_5m_usd,:initial_volume_5m_usd)",
                    "current_volume_5m_usd=:current_volume_5m_usd",
                    "current_buys_5m=:current_buys_5m",
                    "current_sells_5m=:current_sells_5m",
                    "snapshot_count=(SELECT COUNT(*) FROM token_snapshots WHERE token_id=(SELECT token_id FROM candidates WHERE id=:id))",
                ]
            values["id"] = candidate_id
            self.conn.execute(f"UPDATE candidates SET {','.join(assignments)} WHERE id=:id", values)
            authoritative = {
                str(CandidateState.REJECTED_UNSAFE): "REJECTED_UNSAFE",
                str(CandidateState.EXPIRED): "EXPIRED",
                str(CandidateState.SIGNALLED): "QUALIFIED_SIGNAL",
                str(CandidateState.EARLY_RADAR): "STANDARD_RADAR",
                str(CandidateState.GENESIS_RADAR): "GENESIS_RADAR",
                str(CandidateState.STANDARD_RADAR): "STANDARD_RADAR",
                str(CandidateState.HOT_RADAR): "HOT_RADAR",
                str(CandidateState.PRIORITY_RADAR): "PRIORITY_RADAR",
                str(CandidateState.QUALIFIED_SIGNAL): "QUALIFIED_SIGNAL",
                str(CandidateState.FAILED_OUTCOME): "FAILED_OUTCOME",
            }.get(str(state))
            if authoritative:
                if authoritative == "STANDARD_RADAR":
                    self.conn.execute(
                        "UPDATE candidates SET authoritative_state='STANDARD_RADAR' WHERE id=? "
                        "AND authoritative_state IN ('DISCOVERED','SCREENING','GENESIS_RADAR')",
                        (candidate_id,),
                    )
                else:
                    self.conn.execute(
                        "UPDATE candidates SET authoritative_state=?,state=? WHERE id=?",
                        (authoritative, authoritative, candidate_id),
                    )
            if str(state) in {str(CandidateState.REJECTED_UNSAFE), str(CandidateState.EXPIRED)}:
                self.conn.execute(
                    "UPDATE candidates SET next_retry_at=NULL,lifecycle_reason=? WHERE id=?",
                    (reason, candidate_id),
                )
            if score:
                self.conn.execute(
                    "INSERT OR IGNORE INTO confidence_history(candidate_id,recorded_at,normalized_score,confidence,reason) "
                    "VALUES(?,?,?,?,?)",
                    (candidate_id, now, score.normalized_score, score.confidence, reason),
                )
            token_id = self.conn.execute(
                "SELECT token_id FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE token_outcomes SET final_lifecycle_state=?,"
                "non_signal_reason=CASE WHEN token_outcomes.signal_id IS NULL THEN ? ELSE NULL END,"
                "signal_id=COALESCE(signal_id,?),updated_at=? WHERE token_id=?",
                (str(state), reason, signal_id, now, token_id),
            )
            if str(old[0]) != str(state):
                self.conn.execute(
                    "INSERT INTO candidate_transitions(candidate_id,from_state,to_state,reason,score,confidence,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        old[0],
                        str(state),
                        reason,
                        score.normalized_score if score else None,
                        score.confidence if score else None,
                        now,
                    ),
                )

    def candidates_report(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT c.*,t.symbol,t.name,t.token_address,t.chain FROM candidates c JOIN tokens t ON t.id=c.token_id "
                "WHERE c.state NOT IN ('REJECTED_UNSAFE','EXPIRED','SIGNALLED') "
                "ORDER BY COALESCE(c.normalized_score,-1) DESC,c.updated_at DESC LIMIT ?",
                (limit,),
            )
        )

    def rejection_report(self, since: str) -> dict[str, Any]:
        hard: dict[str, int] = {}
        temporary: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT hard_rejections_json,waiting_reasons_json FROM candidates WHERE updated_at>=?",
            (since,),
        ):
            for reason in json.loads(row[0]):
                hard[reason] = hard.get(reason, 0) + 1
            for reason in json.loads(row[1]):
                temporary[reason] = temporary.get(reason, 0) + 1
        return {
            "hard": sorted(hard.items(), key=lambda x: (-x[1], x[0])),
            "temporary": sorted(temporary.items(), key=lambda x: (-x[1], x[0])),
        }

    def save_evaluation(
        self,
        token_id: int,
        score: ScoreResult,
        evidence: dict[str, Any],
        evaluated_at: str | None = None,
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO evaluations(token_id,evaluated_at,classification,score,confidence,"
                "hard_rejections_json,component_scores_json,evidence_json,scoring_version,normalized_score,available_weight) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    evaluated_at or iso(),
                    score.classification,
                    score.total,
                    score.confidence,
                    _json(score.hard_rejections),
                    _json(score.component_scores),
                    _json(evidence),
                    score.scoring_version,
                    score.normalized_score,
                    score.available_weight,
                ),
            )
            return int(cur.lastrowid)

    def save_v3_shadow_decision(
        self,
        *,
        candidate_id: int,
        token_id: int,
        envelope: dict[str, Any],
        control_decision: dict[str, Any],
        v2_decision: dict[str, Any],
        features: dict[str, Any],
        latency: dict[str, Any] | None = None,
        veto_reasons: list[str] | None = None,
    ) -> int:
        """Persist a V3 comparison without creating a signal or outbox event."""
        if envelope.get("public_route") is not False:
            raise ValueError("Intelligence V3 shadow decisions cannot use a public route")
        if envelope.get("model_versions", {}).get("v3") != "INTELLIGENCE_V3_RESEARCH":
            raise ValueError("invalid V3 research model identity")
        with self._lock, self.conn:
            before = int(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            self.conn.execute(
                "INSERT INTO intelligence_v3_shadow_decisions("
                "candidate_id,token_id,decision_timestamp,available_evidence_timestamp,"
                "control_decision_json,v2_decision_json,v3_decision_json,features_json,"
                "latency_json,veto_reasons_json,model_version,public_route,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id,decision_timestamp,"
                "model_version) DO UPDATE SET control_decision_json=excluded.control_decision_json,"
                "v2_decision_json=excluded.v2_decision_json,v3_decision_json=excluded.v3_decision_json,"
                "features_json=excluded.features_json,latency_json=excluded.latency_json,"
                "veto_reasons_json=excluded.veto_reasons_json",
                (
                    candidate_id,
                    token_id,
                    str(envelope["decision_timestamp"]),
                    str(envelope["available_evidence_timestamp"]),
                    _json(control_decision),
                    _json(v2_decision),
                    _json(envelope),
                    _json(features),
                    _json(latency or {}),
                    _json(veto_reasons or []),
                    "INTELLIGENCE_V3_RESEARCH",
                    0,
                    iso(),
                ),
            )
            after = int(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            if after != before:
                raise RuntimeError("V3 shadow persistence changed the public outbox")
            row = self.conn.execute(
                "SELECT id FROM intelligence_v3_shadow_decisions "
                "WHERE candidate_id=? AND decision_timestamp=? AND model_version=?",
                (
                    candidate_id,
                    str(envelope["decision_timestamp"]),
                    "INTELLIGENCE_V3_RESEARCH",
                ),
            ).fetchone()
            assert row is not None
            return int(row[0])

    def create_signal(
        self,
        token_id: int,
        snapshot: MarketSnapshot,
        score: ScoreResult,
        states: dict[str, Any],
        risk_flags: list[str],
        message_payload: dict[str, Any],
        holder_count: int | None = None,
    ) -> int | None:
        """Atomically create immutable signal and its outbox event; return None on dedupe."""
        if snapshot.market_cap_usd is None or snapshot.market_cap_usd <= 0:
            raise ValueError("A real positive market cap is required for performance tracking")
        now = snapshot.captured_at
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT id FROM signals WHERE token_id=? AND scoring_version=?",
                (token_id, score.scoring_version),
            ).fetchone()
            if existing:
                return None
            candidate = self.conn.execute(
                "SELECT first_discovered_at,radar_triggered_at,radar_market_cap_usd "
                "FROM candidates WHERE token_id=?",
                (token_id,),
            ).fetchone()
            radar_at = candidate["radar_triggered_at"] if candidate else None
            radar_mc = candidate["radar_market_cap_usd"] if candidate else None
            signalled_at = datetime.fromisoformat(now)
            discovery_seconds = None
            radar_seconds = None
            radar_multiple = None
            if candidate:
                discovery_seconds = (
                    signalled_at - datetime.fromisoformat(candidate["first_discovered_at"])
                ).total_seconds()
            if radar_at:
                radar_seconds = (signalled_at - datetime.fromisoformat(radar_at)).total_seconds()
                if radar_mc and float(radar_mc) > 0:
                    radar_multiple = snapshot.market_cap_usd / float(radar_mc)
            cur = self.conn.execute(
                "INSERT INTO signals(token_id,signal_timestamp,signal_price_usd,signal_market_cap_usd,"
                "signal_liquidity_usd,signal_holder_count,signal_volume_5m_usd,signal_score,signal_class,"
                "component_scores_json,developer_state_json,narrative_state_json,social_state_json,"
                "onchain_state_json,risk_flags_json,scoring_version,current_market_cap_usd,current_score,"
                "ath_market_cap_usd,atl_market_cap_usd,last_monitored_at,created_at,normalized_score,confidence,"
                "candidate_history_json,current_signal_class,radar_timestamp,radar_market_cap_usd,"
                "radar_to_signal_seconds,radar_to_signal_multiple,discovery_to_signal_seconds,"
                "v15_signal_tier) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token_id,
                    now,
                    snapshot.price_usd,
                    snapshot.market_cap_usd,
                    snapshot.liquidity_usd,
                    holder_count,
                    snapshot.volume_5m_usd,
                    score.total,
                    score.classification,
                    _json(score.component_scores),
                    _json(states.get("developer", {})),
                    _json(states.get("narrative", {})),
                    _json(states.get("social", {})),
                    _json(states.get("onchain", {})),
                    _json(risk_flags),
                    score.scoring_version,
                    snapshot.market_cap_usd,
                    score.normalized_score,
                    snapshot.market_cap_usd,
                    snapshot.market_cap_usd,
                    now,
                    now,
                    score.normalized_score,
                    score.confidence,
                    _json(states.get("candidate_history", {})),
                    str(score.classification),
                    radar_at,
                    radar_mc,
                    radar_seconds,
                    radar_multiple,
                    discovery_seconds,
                    str(message_payload.get("v15_signal_tier") or "UNKNOWN"),
                ),
            )
            signal_id = int(cur.lastrowid)
            payload = dict(message_payload, signal_id=signal_id)
            enqueue_at = iso()
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"signal:{signal_id}", "SIGNAL", _json(payload), enqueue_at),
            )
            trigger = (message_payload.get("realtime_intelligence") or {}).get("trigger_event_id")
            if trigger:
                self.conn.execute(
                    "UPDATE canonical_events SET discord_enqueue_timestamp=? WHERE event_id=?",
                    (enqueue_at, str(trigger)),
                )
            self.conn.execute(
                "UPDATE token_outcomes SET signal_id=?,final_lifecycle_state='SIGNALLED',"
                "non_signal_reason=NULL,updated_at=? WHERE token_id=?",
                (signal_id, now, token_id),
            )
            self.conn.execute(
                "UPDATE candidates SET state='QUALIFIED_SIGNAL',"
                "authoritative_state='QUALIFIED_SIGNAL',updated_at=? WHERE token_id=?",
                (now, token_id),
            )
            return signal_id

    def active_signals(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT s.*,t.token_address,t.symbol,t.name,t.chain,t.pair_address FROM signals s "
                "JOIN tokens t ON t.id=s.token_id WHERE s.active=1 ORDER BY s.id"
            )
        )

    def signal(self, signal_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()

    def update_signal_intelligence(
        self,
        signal_id: int,
        new_class: str,
        score: ScoreResult,
        reasons: list[str],
        payload: dict[str, Any],
    ) -> str | None:
        rank = {"WATCH": 1, "STRONG": 2, "HIGH_CONVICTION": 3}
        now = iso()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT signal_class,COALESCE(current_signal_class,signal_class) current_class,current_score "
                "FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()
            if not row:
                return None
            old = str(row["current_class"])
            update_type = (
                "UPGRADE" if rank.get(new_class, 0) > rank.get(old, 0) else "DETERIORATION"
            )
            event_key = (
                f"upgrade:{signal_id}:{new_class}"
                if update_type == "UPGRADE"
                else f"deterioration:{signal_id}:{new_class}:{','.join(sorted(reasons))}"
            )
            if self.conn.execute("SELECT 1 FROM outbox WHERE event_key=?", (event_key,)).fetchone():
                return None
            self.conn.execute(
                "INSERT INTO signal_updates(signal_id,update_timestamp,update_type,previous_score,new_score,reasons_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    signal_id,
                    now,
                    update_type,
                    row["current_score"],
                    score.normalized_score,
                    _json(reasons),
                ),
            )
            self.conn.execute(
                "UPDATE signals SET current_signal_class=?,current_score=? WHERE id=?",
                (new_class if update_type == "UPGRADE" else old, score.normalized_score, signal_id),
            )
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (
                    event_key,
                    update_type,
                    _json(
                        dict(
                            payload,
                            signal_id=signal_id,
                            previous_class=old,
                            new_class=new_class,
                            reasons=reasons,
                        )
                    ),
                    now,
                ),
            )
            return update_type

    def update_tracking(
        self,
        signal_id: int,
        market_cap: float,
        monitored_at: str,
        max_multiple: float,
        max_drawdown: float,
        ath: float,
        atl: float,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE signals SET current_market_cap_usd=?,last_monitored_at=?,max_multiple=?,"
                "max_drawdown=?,ath_market_cap_usd=?,atl_market_cap_usd=? WHERE id=?",
                (market_cap, monitored_at, max_multiple, max_drawdown, ath, atl, signal_id),
            )

    def record_milestones(
        self,
        signal_id: int,
        candidates: Iterable[tuple[float, float, float]],
        payload_base: dict[str, Any],
    ) -> list[float]:
        """Atomically claim unseen milestones and enqueue each exactly once."""
        hit: list[float] = []
        now = iso()
        with self._lock, self.conn:
            for multiple, market_cap, seconds_to_hit in candidates:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO milestones(signal_id,multiple,hit_at,market_cap_usd,seconds_to_hit) "
                    "VALUES(?,?,?,?,?)",
                    (signal_id, multiple, now, market_cap, seconds_to_hit),
                )
                if cur.rowcount != 1:
                    continue
                payload = dict(
                    payload_base,
                    signal_id=signal_id,
                    milestone=multiple,
                    market_cap_usd=market_cap,
                    seconds_to_hit=seconds_to_hit,
                )
                self.conn.execute(
                    "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (f"milestone:{signal_id}:{multiple:g}", "MILESTONE", _json(payload), now),
                )
                hit.append(multiple)
        return hit

    def fail_signal(self, signal_id: int, payload: dict[str, Any]) -> bool:
        now = iso()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE signals SET active=0,status='FAILED',last_monitored_at=? "
                "WHERE id=? AND active=1",
                (now, signal_id),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "INSERT OR IGNORE INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"failed:{signal_id}", "FAILED", _json(dict(payload, signal_id=signal_id)), now),
            )
            return True

    def pending_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,)
            )
        )

    def claim_outbox(self, limit: int = 20, lease_seconds: int = 120) -> list[sqlite3.Row]:
        """Atomically lease rows so concurrent flushers cannot deliver the same event."""
        token = uuid.uuid4().hex
        expired = (datetime.now(UTC) - timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [
                    int(row[0])
                    for row in self.conn.execute(
                        "SELECT id FROM outbox WHERE sent_at IS NULL "
                        "AND (claim_token IS NULL OR claimed_at<?) ORDER BY id LIMIT ?",
                        (expired, limit),
                    )
                ]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self.conn.execute(
                        f"UPDATE outbox SET claim_token=?,claimed_at=? WHERE id IN ({placeholders})",
                        (token, iso(), *ids),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return list(
            self.conn.execute(
                "SELECT * FROM outbox WHERE claim_token=? AND sent_at IS NULL ORDER BY id", (token,)
            )
        )

    def mark_outbox_sent(
        self, outbox_id: int, remote_id: str | None, claim_token: str | None = None
    ) -> None:
        with self._lock, self.conn:
            sql = (
                "UPDATE outbox SET sent_at=?,remote_message_id=?,attempts=attempts+1,last_error=NULL,"
                "claim_token=NULL,claimed_at=NULL WHERE id=?"
            )
            args: tuple[Any, ...] = (iso(), remote_id, outbox_id)
            if claim_token is not None:
                sql += " AND claim_token=?"
                args += (claim_token,)
            self.conn.execute(sql, args)

    def mark_outbox_error(self, outbox_id: int, error: str, claim_token: str | None = None) -> None:
        with self._lock, self.conn:
            sql = (
                "UPDATE outbox SET attempts=attempts+1,last_error=?,claim_token=NULL,claimed_at=NULL "
                "WHERE id=?"
            )
            args = (error[:1000], outbox_id)
            if claim_token is not None:
                sql += " AND claim_token=?"
                args += (claim_token,)
            self.conn.execute(sql, args)

    def ensure_alert_deliveries(self, outbox_id: int, channel_ids: Iterable[int]) -> None:
        with self._lock, self.conn:
            for channel_id in channel_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO alert_deliveries(outbox_id,channel_id) VALUES(?,?)",
                    (outbox_id, str(channel_id)),
                )

    def pending_alert_deliveries(self, outbox_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM alert_deliveries WHERE outbox_id=? AND status!='SENT' ORDER BY id",
                (outbox_id,),
            )
        )

    def mark_alert_delivery(
        self,
        delivery_id: int,
        success: bool,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE alert_deliveries SET status=?,attempts=attempts+1,remote_message_id=?,"
                "delivered_at=?,last_error=? WHERE id=?",
                (
                    "SENT" if success else "FAILED",
                    remote_id,
                    iso() if success else None,
                    None if success else str(error or "")[:1000],
                    delivery_id,
                ),
            )

    def set_provider_health(
        self,
        provider: str,
        healthy: bool,
        failures: int,
        error: str | None,
        state: str | None = None,
    ) -> None:
        now = iso()
        lowered = str(error or "").lower()
        state = state or (
            "DISABLED"
            if lowered == "disabled" or "_disabled" in lowered
            else "HEALTHY"
            if healthy
            else "RATE_LIMITED"
            if "429" in lowered or "rate limit" in lowered
            else "CIRCUIT_OPEN"
            if "circuit" in lowered and "open" in lowered
            else "DOWN"
            if failures >= 4
            else "DEGRADED"
            if error
            else "UNKNOWN"
        )
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO provider_health(provider,healthy,consecutive_failures,last_success_at,"
                "last_failure_at,last_error,updated_at,state,disabled_reason) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET healthy=excluded.healthy,"
                "consecutive_failures=excluded.consecutive_failures,"
                "last_success_at=CASE WHEN excluded.healthy=1 THEN excluded.updated_at ELSE provider_health.last_success_at END,"
                "last_failure_at=CASE WHEN excluded.healthy=0 THEN excluded.updated_at ELSE provider_health.last_failure_at END,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at,state=excluded.state,"
                "disabled_reason=excluded.disabled_reason",
                (
                    provider,
                    int(healthy),
                    failures,
                    now if healthy else None,
                    None if healthy else now,
                    error,
                    now,
                    state,
                    error if state == "DISABLED" else None,
                ),
            )

    def set_guild_settings(
        self,
        guild_id: int | str,
        channel_id: int | str | None,
        alerts_enabled: bool,
        alert_tier: str = "HOT_PLUS",
        updated_by: int | str | None = None,
        daily_report_enabled: bool = False,
        enabled_chains: Iterable[str] = ("solana", "bsc"),
    ) -> None:
        tier = alert_tier.upper()
        if tier not in {
            "ALL",
            "HOT",
            "PRIORITY",
            "QUALIFIED",
            "GENESIS_ALL",
            "HOT_PLUS",
            "PRIORITY_PLUS",
            "QUALIFIED_ONLY",
        }:
            raise ValueError("Invalid alert tier")
        legacy_tier = {
            "GENESIS_ALL": "ALL",
            "HOT_PLUS": "HOT",
            "PRIORITY_PLUS": "PRIORITY",
            "QUALIFIED_ONLY": "QUALIFIED",
        }.get(tier, tier)
        now = iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO guild_settings(guild_id,alert_channel_id,alerts_enabled,alert_tier,created_at,updated_by,"
                "updated_at,alert_tier_v14,daily_report_enabled,enabled_chains_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                "alert_channel_id=excluded.alert_channel_id,alerts_enabled=excluded.alerts_enabled,"
                "alert_tier=excluded.alert_tier,alert_tier_v14=excluded.alert_tier_v14,"
                "daily_report_enabled=excluded.daily_report_enabled,enabled_chains_json=excluded.enabled_chains_json,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (
                    str(guild_id),
                    str(channel_id) if channel_id is not None else None,
                    int(alerts_enabled),
                    legacy_tier,
                    now,
                    str(updated_by) if updated_by is not None else None,
                    now,
                    tier,
                    int(daily_report_enabled),
                    _json(sorted(set(enabled_chains))),
                ),
            )

    def guild_settings(self, guild_id: int | str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id=?", (str(guild_id),)
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["alert_tier"] = value.get("alert_tier_v14") or value["alert_tier"]
        value["enabled_chains"] = json.loads(value.get("enabled_chains_json") or "[]")
        return value

    def alert_destinations(self) -> list[dict[str, Any]]:
        return [
            dict(row) | {"alert_tier": row["alert_tier_v14"] or row["alert_tier"]}
            for row in self.conn.execute(
                "SELECT * FROM guild_settings WHERE alerts_enabled=1 AND alert_channel_id IS NOT NULL ORDER BY guild_id"
            )
        ]

    @staticmethod
    def alert_allowed(tier: str, event_type: str, payload: dict[str, Any]) -> bool:
        # V1.5 externally routes only public signal classes and post-call
        # intelligence. Genesis/Radar remain queryable internal states.
        if event_type in {
            "MILESTONE",
            "RADAR_MILESTONE",
            "RADAR_RISK",
            "FAILED",
            "DETERIORATION",
            "UPGRADE",
        }:
            return True
        if event_type != "SIGNAL":
            return False
        return str(payload.get("v15_signal_tier") or payload.get("signal_tier") or "").upper() in {
            "PREMIUM",
            "STRONG",
            "HIGH_RISK_MOMENTUM",
            "CATALYST_REVIVAL",
        }

    def ensure_guild_alert_delivery(
        self, outbox_id: int, guild_id: int | str, channel_id: int | str
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO alert_deliveries_v131(outbox_id,guild_id,channel_id) VALUES(?,?,?)",
                (outbox_id, str(guild_id), str(channel_id)),
            )

    def pending_guild_alert_deliveries(self, outbox_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM alert_deliveries_v131 WHERE outbox_id=? AND status!='SENT' ORDER BY id",
                (outbox_id,),
            )
        )

    def mark_guild_alert_delivery(
        self,
        delivery_id: int,
        success: bool,
        remote_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE alert_deliveries_v131 SET status=?,attempts=attempts+1,remote_message_id=?,"
                "delivered_at=?,last_error=? WHERE id=?",
                (
                    "SENT" if success else "FAILED",
                    remote_id,
                    iso() if success else None,
                    None if success else str(error or "")[:1000],
                    delivery_id,
                ),
            )

    def record_test_alert(
        self,
        guild_id: int | str | None,
        channel_id: int | str,
        requested_by: int | str | None,
        remote_id: str | None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO test_alert_events(guild_id,channel_id,requested_by,delivered_at,remote_message_id) "
                "VALUES(?,?,?,?,?)",
                (
                    str(guild_id) if guild_id is not None else None,
                    str(channel_id),
                    str(requested_by) if requested_by is not None else None,
                    iso(),
                    remote_id,
                ),
            )

    def save_gmgn_intelligence(
        self, token_id: int, snapshot: dict[str, Any], wallet: dict[str, Any]
    ) -> int:
        """Persist raw GMGN evidence and derived wallet evidence without flattening labels."""
        captured = str(snapshot.get("retrieved_at") or iso())
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO gmgn_snapshots(token_id,captured_at,payload_json,unavailable_json) "
                "VALUES(?,?,?,?)",
                (token_id, captured, _json(snapshot), _json(snapshot.get("unavailable", []))),
            )
            self.conn.execute(
                "INSERT INTO wallet_intelligence(token_id,captured_at,smart_money_state,buyer_diversity,"
                "activity_quality,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    token_id,
                    captured,
                    wallet.get("smart_money", "SMART_MONEY_UNKNOWN"),
                    wallet.get("buyer_diversity", "UNKNOWN"),
                    wallet.get("activity_quality", "UNKNOWN"),
                    _json(wallet),
                ),
            )
            for field in ("info", "security", "pool", "holders", "traders"):
                value = snapshot.get(field)
                self.conn.execute(
                    "INSERT OR IGNORE INTO provider_evidence(token_id,field_name,value_json,provider,"
                    "retrieved_at,confidence,raw_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        token_id,
                        field,
                        _json(value),
                        "gmgn",
                        captured,
                        "KNOWN" if value is not None else "UNKNOWN",
                        _json(value),
                    ),
                )
            return int(cur.lastrowid or 0)

    def record_intelligence_event(
        self, token_id: int, event_key: str, event_type: str, evidence: dict[str, Any]
    ) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO intelligence_events(token_id,event_key,event_type,detected_at,evidence_json) "
                "VALUES(?,?,?,?,?)",
                (token_id, event_key, event_type, iso(), _json(evidence)),
            )
            return cur.rowcount == 1

    def state_reconciliation(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])
        states = {
            str(r[0]): int(r[1])
            for r in self.conn.execute(
                "SELECT COALESCE(c.state,'DISCOVERED'),COUNT(*) FROM tokens t LEFT JOIN candidates c "
                "ON c.token_id=t.id GROUP BY COALESCE(c.state,'DISCOVERED')"
            )
        }
        accounted = sum(states.values())
        orphan_candidates = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM candidates c LEFT JOIN tokens t ON t.id=c.token_id "
                "WHERE t.id IS NULL"
            ).fetchone()[0]
        )
        orphan_signals = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM signals s LEFT JOIN tokens t ON t.id=s.token_id "
                "WHERE t.id IS NULL"
            ).fetchone()[0]
        )
        ghost_qualified = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM candidates c LEFT JOIN signals s ON s.id=c.signal_id "
                "WHERE c.authoritative_state='QUALIFIED_SIGNAL' AND s.id IS NULL"
            ).fetchone()[0]
        )
        duplicate_candidates = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT token_id FROM candidates GROUP BY token_id "
                "HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        integrity_difference = (
            total
            - accounted
            + orphan_candidates
            + orphan_signals
            + ghost_qualified
            + duplicate_candidates
        )
        return {
            "total_tracked": total,
            "states": states,
            "accounted": accounted,
            "orphan_candidates": orphan_candidates,
            "orphan_signals": orphan_signals,
            "ghost_qualified": ghost_qualified,
            "duplicate_candidates": duplicate_candidates,
            "difference": integrity_difference,
            "reconciled": integrity_difference == 0,
        }

    def database_integrity(self) -> dict[str, Any]:
        """Return bounded, read-only integrity evidence for operator health."""
        quick_check = str(self.conn.execute("PRAGMA quick_check(1)").fetchone()[0])
        foreign_key_violation = self.conn.execute("PRAGMA foreign_key_check").fetchone()
        journal_mode = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
        return {
            "quick_check": quick_check,
            "foreign_key_violations": 0 if foreign_key_violation is None else 1,
            "journal_mode": journal_mode,
            "healthy": quick_check == "ok"
            and foreign_key_violation is None
            and journal_mode == "WAL",
        }

    def token_intelligence(self, address: str, chain: str | None = None) -> dict[str, Any] | None:
        args: list[Any] = [address]
        chain_sql = ""
        if chain:
            chain_sql, args = " AND t.chain=?", [address, chain]
        row = self.conn.execute(
            "SELECT t.*,c.state,c.radar_score,c.radar_triggered_at,c.radar_market_cap_usd,"
            "c.current_market_cap_usd,c.current_liquidity_usd,c.normalized_score,c.confidence,"
            "c.unknown_fields_json,c.waiting_reasons_json,c.lifecycle_reason,c.last_attempted_at,c.next_retry_at,"
            "s.max_multiple,s.ath_market_cap_usd,s.atl_market_cap_usd,s.status signal_status "
            "FROM tokens t LEFT JOIN candidates c ON c.token_id=t.id LEFT JOIN signals s ON s.token_id=t.id "
            "WHERE t.token_address=?" + chain_sql + " ORDER BY t.id DESC LIMIT 1",
            args,
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        gmgn = self.conn.execute(
            "SELECT payload_json FROM gmgn_snapshots WHERE token_id=? ORDER BY captured_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        wallet = self.conn.execute(
            "SELECT payload_json FROM wallet_intelligence WHERE token_id=? ORDER BY captured_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        result["gmgn"] = json.loads(gmgn[0]) if gmgn else None
        result["wallet_intelligence"] = json.loads(wallet[0]) if wallet else None
        realtime_state = self.conn.execute(
            "SELECT * FROM token_realtime_state WHERE token_id=?", (row["id"],)
        ).fetchone()
        realtime_feature = self.conn.execute(
            "SELECT decision_timestamp,available_timestamp,evidence_mode,feature_json "
            "FROM trajectory_feature_snapshots_v15 WHERE token_id=? "
            "ORDER BY decision_timestamp DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        result["realtime_state"] = dict(realtime_state) if realtime_state else None
        result["realtime_intelligence"] = (
            {
                **json.loads(realtime_feature["feature_json"]),
                "available_timestamp": realtime_feature["available_timestamp"],
                "evidence_mode": realtime_feature["evidence_mode"],
            }
            if realtime_feature
            else None
        )
        result["unknown_fields"] = json.loads(result.pop("unknown_fields_json") or "[]")
        result["waiting_reasons"] = json.loads(result.pop("waiting_reasons_json") or "[]")
        result["timeline"] = [
            dict(x)
            for x in self.conn.execute(
                "SELECT event_type,detected_at,evidence_json FROM intelligence_events WHERE token_id=? ORDER BY detected_at",
                (row["id"],),
            )
        ]
        result["snapshots"] = [
            dict(x)
            for x in self.conn.execute(
                "SELECT captured_at,market_cap_usd,liquidity_usd FROM token_snapshots WHERE token_id=? ORDER BY captured_at",
                (row["id"],),
            )
        ]
        return result

    def radar_board(self, limit: int = 250) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT t.name,t.symbol,t.chain,t.token_address,c.state,c.radar_score,c.normalized_score,"
                "c.confidence,c.radar_market_cap_usd,c.current_market_cap_usd,c.current_liquidity_usd,"
                "c.radar_triggered_at,c.updated_at,COALESCE(s.max_multiple,ro.peak_multiple) max_multiple,"
                "s.ath_market_cap_usd,s.status signal_status,"
                "ro.peak_multiple radar_peak_multiple,ro.current_multiple radar_current_multiple,ro.status radar_outcome "
                "FROM candidates c JOIN tokens t ON t.id=c.token_id LEFT JOIN signals s ON s.id=c.signal_id "
                "LEFT JOIN radar_events re ON re.candidate_id=c.id AND re.event_level='EARLY_RADAR' "
                "LEFT JOIN radar_outcomes ro ON ro.radar_event_id=re.id "
                "ORDER BY COALESCE(c.radar_score,0) DESC,c.updated_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def status_stats(
        self, started_at: str, candidate_max_age_minutes: float = 180
    ) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        now = datetime.now(UTC)
        one_hour = (now - timedelta(hours=1)).isoformat()
        three_hours = (now - timedelta(hours=3)).isoformat()
        stale_cutoff = (now - timedelta(minutes=candidate_max_age_minutes)).isoformat()
        one = lambda sql, args=(): self.conn.execute(sql, args).fetchone()[0]
        providers = self.conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(CASE WHEN state IN ('HEALTHY','CONNECTED') "
            "THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN state NOT IN "
            "('DISABLED','NOT_CONFIGURED') THEN 1 ELSE 0 END),0) FROM provider_health"
        ).fetchone()
        provider_rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT provider,healthy,state,consecutive_failures,last_success_at,last_error,updated_at "
                "FROM provider_health ORDER BY provider"
            )
        ]
        result = {
            "started_at": started_at,
            "tokens_discovered": one("SELECT COUNT(*) FROM tokens"),
            "tokens_evaluated": one("SELECT COUNT(DISTINCT token_id) FROM evaluations"),
            "hard_rejected": one("SELECT COUNT(*) FROM candidates WHERE state='REJECTED_UNSAFE'"),
            "pending_evidence": one(
                "SELECT COUNT(*) FROM candidates WHERE state IN ('PENDING_EVIDENCE','FAILED_PROVIDER','DISCOVERED','SCREENING')"
            ),
            "pending_over_1h": one(
                "SELECT COUNT(*) FROM candidates WHERE state IN ('PENDING_EVIDENCE','FAILED_PROVIDER','DISCOVERED','SCREENING') AND first_discovered_at<?",
                (one_hour,),
            ),
            "pending_over_3h": one(
                "SELECT COUNT(*) FROM candidates WHERE state IN ('PENDING_EVIDENCE','FAILED_PROVIDER','DISCOVERED','SCREENING') AND first_discovered_at<?",
                (three_hours,),
            ),
            "stale_beyond_ttl": one(
                "SELECT COUNT(*) FROM candidates WHERE state NOT IN "
                "('REJECTED_UNSAFE','EXPIRED','SIGNALLED','QUALIFIED_SIGNAL') "
                "AND first_discovered_at<?",
                (stale_cutoff,),
            ),
            "candidates_watching": one("SELECT COUNT(*) FROM candidates WHERE state='CANDIDATE'"),
            "early_radar": one(
                "SELECT COUNT(*) FROM candidates WHERE radar_triggered_at IS NOT NULL"
            ),
            "expired": one("SELECT COUNT(*) FROM candidates WHERE state='EXPIRED'"),
            "signals": one("SELECT COUNT(*) FROM signals"),
            "outcomes_tracked": one("SELECT COUNT(*) FROM token_outcomes"),
            "watch": one("SELECT COUNT(*) FROM signals WHERE signal_class='WATCH'"),
            "strong": one("SELECT COUNT(*) FROM signals WHERE signal_class='STRONG'"),
            "high_conviction": one(
                "SELECT COUNT(*) FROM signals WHERE signal_class='HIGH_CONVICTION'"
            ),
            "active_signals": one("SELECT COUNT(*) FROM signals WHERE active=1"),
            "signals_today": one(
                "SELECT COUNT(*) FROM signals WHERE signal_timestamp LIKE ?", (today + "%",)
            ),
            "providers_healthy": int(providers[1]),
            "providers_total": int(providers[2]),
            "providers_configured": int(providers[0]),
            "provider_status": provider_rows,
            "outbox_pending": one("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL"),
            "discord_deliveries_pending": one(
                "SELECT (SELECT COUNT(*) FROM alert_deliveries WHERE status!='SENT') + "
                "(SELECT COUNT(*) FROM alert_deliveries_v131 WHERE status!='SENT')"
            ),
            "discord_deliveries_failed": one(
                "SELECT (SELECT COUNT(*) FROM alert_deliveries WHERE status='FAILED') + "
                "(SELECT COUNT(*) FROM alert_deliveries_v131 WHERE status='FAILED')"
            ),
        }
        last_alert = self.conn.execute(
            "SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        result["last_alert_error"] = last_alert[0] if last_alert else None
        result["state_reconciliation"] = self.state_reconciliation()
        result["database"] = self.database_integrity()
        result["status"] = (
            "DOWN"
            if not result["database"]["healthy"]
            else "DEGRADED"
            if not result["state_reconciliation"]["reconciled"]
            or result["discord_deliveries_failed"] > 0
            or (result["providers_total"] > 0 and result["providers_healthy"] == 0)
            else "HEALTHY"
        )
        return result

    def missed_report(
        self, since: str | None, threshold: float, limit: int = 10
    ) -> list[sqlite3.Row]:
        where = "WHERE m.signal_before_hit=0"
        args: list[Any] = [threshold]
        if since:
            where += " AND m.hit_at>=?"
            args.append(since)
        args.append(limit)
        return list(
            self.conn.execute(
                "SELECT o.*,m.radar_before_hit,m.signal_before_hit,m.hit_at threshold_hit_at,"
                "t.chain,t.token_address,t.symbol,t.name,c.reason,c.confidence,c.state "
                "FROM token_outcomes o JOIN tokens t ON t.id=o.token_id "
                "JOIN outcome_milestones m ON m.token_id=o.token_id AND m.multiple=? "
                "LEFT JOIN candidates c ON c.token_id=t.id "
                + where
                + " ORDER BY o.max_multiple_from_discovery DESC LIMIT ?",
                args,
            )
        )

    def outcome_watchlist(self, since: str, limit: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT o.*,t.chain,t.token_address FROM token_outcomes o "
                "JOIN tokens t ON t.id=o.token_id JOIN candidates c ON c.token_id=t.id "
                "WHERE o.signal_id IS NULL AND c.state IN ('EXPIRED','REJECTED_UNSAFE') "
                "AND t.first_discovered_at>=? ORDER BY o.last_observed_at LIMIT ?",
                (since, limit),
            )
        )

    def coverage(self, since: str | None, major_multiple: float) -> dict[str, Any]:
        where = "WHERE m.multiple=?"
        args: list[Any] = [major_multiple]
        if since:
            where += " AND m.hit_at>=?"
            args.append(since)
        rows = list(
            self.conn.execute(
                "SELECT o.*,m.radar_before_hit,m.signal_before_hit,m.hit_at threshold_hit_at "
                "FROM token_outcomes o JOIN outcome_milestones m ON m.token_id=o.token_id " + where,
                args,
            )
        )
        major = len(rows)
        radar = sum(bool(r["radar_before_hit"]) for r in rows)
        signalled = sum(bool(r["signal_before_hit"]) for r in rows)
        complete_miss = sum(not r["radar_before_hit"] and not r["signal_before_hit"] for r in rows)
        latency = list(
            self.conn.execute(
                "SELECT discovery_to_signal_seconds,radar_to_signal_seconds,radar_to_signal_multiple "
                "FROM signals WHERE discovery_to_signal_seconds IS NOT NULL"
                + (" AND signal_timestamp>=?" if since else ""),
                ([since] if since else []),
            )
        )
        return {
            "major_runner_multiple": major_multiple,
            "major_runners_discovered": major,
            "major_runners_radar": radar,
            "major_runners_signalled": signalled,
            "major_runners_completely_missed": complete_miss,
            "radar_recall": radar / major * 100 if major else None,
            "signal_recall": signalled / major * 100 if major else None,
            "complete_miss_rate": complete_miss / major * 100 if major else None,
            "median_discovery_to_signal_seconds": statistics.median(
                [float(r[0]) for r in latency if r[0] is not None]
            )
            if latency
            else None,
            "median_radar_to_signal_seconds": statistics.median(
                [float(r[1]) for r in latency if r[1] is not None]
            )
            if any(r[1] is not None for r in latency)
            else None,
            "median_radar_to_signal_multiple": statistics.median(
                [float(r[2]) for r in latency if r[2] is not None]
            )
            if any(r[2] is not None for r in latency)
            else None,
        }

    def performance(
        self, version: str, since: str | None = None, major_multiple: float = 10
    ) -> dict[str, Any]:
        where = "WHERE scoring_version=?"
        args: list[Any] = [version]
        if since:
            where += " AND signal_timestamp>=?"
            args.append(since)
        rows = list(self.conn.execute(f"SELECT * FROM signals {where}", args))
        milestones: dict[int, set[float]] = {}
        if rows:
            ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" for _ in ids)
            for m in self.conn.execute(
                f"SELECT signal_id,multiple FROM milestones WHERE signal_id IN ({marks})", ids
            ):
                milestones.setdefault(int(m[0]), set()).add(float(m[1]))
        total = len(rows)
        result: dict[str, Any] = {
            "scoring_version": version,
            "total_signals": total,
            "watch": sum(r["signal_class"] == "WATCH" for r in rows),
            "strong": sum(r["signal_class"] == "STRONG" for r in rows),
            "high_conviction": sum(r["signal_class"] == "HIGH_CONVICTION" for r in rows),
            "failed": sum(r["status"] == "FAILED" for r in rows),
        }
        for target in (1.5, 2, 3, 5, 10, 25, 50, 100):
            count = sum(any(x >= target for x in milestones.get(int(r["id"]), set())) for r in rows)
            label = f"{target:g}"
            result[f"{label}x_count"] = count
            result[f"{label}x_rate"] = (count / total * 100) if total else None
        multiples = sorted(float(r["max_multiple"]) for r in rows)
        drawdowns = sorted(float(r["max_drawdown"]) for r in rows)
        result["median_max_multiple"] = statistics.median(multiples) if total else None
        result["median_drawdown"] = statistics.median(drawdowns) if total else None
        for target in (2, 3, 5):
            durations = [
                float(x[0])
                for x in self.conn.execute(
                    "SELECT m.seconds_to_hit FROM milestones m JOIN signals s ON s.id=m.signal_id "
                    f"WHERE s.scoring_version=? AND m.multiple=?{' AND s.signal_timestamp>=?' if since else ''}",
                    [version, target] + ([since] if since else []),
                )
            ]
            result[f"median_seconds_to_{target}x"] = (
                statistics.median(durations) if durations else None
            )
        result["coverage"] = self.coverage(since, major_multiple)
        return result

    # V1.4 ultra-early, graph, watchlist, and attribution persistence.
    def record_launch_event(self, event: Any) -> tuple[int, bool]:
        received = datetime.fromisoformat(str(event.source_received_at))
        observed = datetime.fromisoformat(str(event.source_event_timestamp))
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        latency_ms = max(0.0, (received - observed).total_seconds() * 1000)
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO launch_events(event_key,source,chain,launchpad,token_address,"
                "creator_address,phase,source_event_timestamp,source_received_at,source_to_candidate_ms,"
                "slot_or_block,transaction_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_key,
                    event.source,
                    event.chain,
                    event.launchpad,
                    event.token_address,
                    event.creator_address,
                    event.phase,
                    event.source_event_timestamp,
                    event.source_received_at,
                    latency_ms,
                    event.slot_or_block,
                    event.transaction_id,
                    _json(event.metadata),
                    iso(),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM launch_events WHERE event_key=?", (event.event_key,)
            ).fetchone()
            assert row
            if cur.rowcount == 1:
                self.conn.execute(
                    "INSERT INTO latency_observations_v14(metric,observed_at,milliseconds,source,event_key) "
                    "VALUES('SOURCE_TO_RECEIVED',?,?,?,?)",
                    (event.source_received_at, latency_ms, event.source, event.event_key),
                )
            return int(row[0]), cur.rowcount == 1

    def launch_cursor(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM launch_source_cursors WHERE source=?", (source,)
        ).fetchone()
        return str(row[0]) if row else None

    def save_launch_cursor(
        self, source: str, cursor: str, metadata: dict[str, Any] | None = None
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO launch_source_cursors(source,cursor,updated_at,metadata_json) VALUES(?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at,"
                "metadata_json=excluded.metadata_json",
                (source, cursor, iso(), _json(metadata or {})),
            )

    def record_launch_events(self, events: Iterable[Any]) -> dict[str, int]:
        """Bulk discovery ingestion keeps the 10k-event path transactional and bounded."""
        inserted = duplicates = 0
        now = iso()
        with self._lock, self.conn:
            for event in events:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO launch_events(event_key,source,chain,launchpad,token_address,"
                    "creator_address,phase,source_event_timestamp,source_received_at,slot_or_block,"
                    "transaction_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_key,
                        event.source,
                        event.chain,
                        event.launchpad,
                        event.token_address,
                        event.creator_address,
                        event.phase,
                        event.source_event_timestamp,
                        event.source_received_at,
                        event.slot_or_block,
                        event.transaction_id,
                        _json(event.metadata),
                        now,
                    ),
                )
                inserted += int(cur.rowcount == 1)
                duplicates += int(cur.rowcount != 1)
        return {"inserted": inserted, "duplicates": duplicates}

    def link_launch_candidate(
        self, launch_event_id: int, candidate_id: int, created_at: str
    ) -> None:
        row = self.conn.execute(
            "SELECT source_event_timestamp,source_received_at FROM launch_events WHERE id=?",
            (launch_event_id,),
        ).fetchone()
        if not row:
            raise KeyError(launch_event_id)
        source = datetime.fromisoformat(str(row[0]))
        created = datetime.fromisoformat(created_at)
        if source.tzinfo is None:
            source = source.replace(tzinfo=UTC)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        latency_ms = max(0.0, (created - source).total_seconds() * 1000)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE launch_events SET candidate_created_at=?,source_to_candidate_ms=?,"
                "processing_status='CANDIDATE_CREATED' WHERE id=?",
                (created_at, latency_ms, launch_event_id),
            )
            self.conn.execute(
                "UPDATE candidates SET source_event_timestamp=?,source_received_at=?,"
                "candidate_created_at=?,authoritative_state='DISCOVERED' WHERE id=?",
                (row[0], row[1], created_at, candidate_id),
            )
            self.conn.execute(
                "INSERT INTO latency_observations_v14(metric,observed_at,milliseconds,event_key) "
                "SELECT 'SOURCE_TO_CANDIDATE',?,?,event_key FROM launch_events WHERE id=?",
                (created_at, latency_ms, launch_event_id),
            )

    def record_v14_decision(
        self,
        candidate_id: int,
        launch_event_id: int | None,
        decision: Any,
        settings: Any,
        observed_at: str | None = None,
        decided_at: str | None = None,
        providers: dict[str, Any] | None = None,
    ) -> bool:
        observed_at = observed_at or iso()
        decided_at = decided_at or iso()
        start = datetime.fromisoformat(observed_at)
        end = datetime.fromisoformat(decided_at)
        latency_ms = max(0.0, (end - start).total_seconds() * 1000)
        state = str(decision.state)
        with self._lock, self.conn:
            previous = self.conn.execute(
                "SELECT authoritative_state FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not previous:
                raise KeyError(candidate_id)
            lifecycle_rank = {
                "DISCOVERED": 0,
                "SCREENING": 1,
                "GENESIS_RADAR": 2,
                "STANDARD_RADAR": 3,
                "HOT_RADAR": 4,
                "PRIORITY_RADAR": 5,
                "QUALIFIED_SIGNAL": 6,
            }
            if lifecycle_rank.get(str(previous[0]), -1) > lifecycle_rank.get(state, -1):
                state = str(previous[0])
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO evaluation_stages_v14(candidate_id,launch_event_id,stage,"
                "observed_at,decided_at,decision,authoritative_state,entry_state,confidence,"
                "feature_vector_json,provider_evidence_json,unknowns_json,reasons_json,latency_ms,"
                "software_version,scoring_version,feature_version,model_version,config_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    launch_event_id,
                    decision.stage,
                    observed_at,
                    decided_at,
                    state,
                    state,
                    str(decision.entry_state),
                    decision.confidence,
                    _json(decision.feature_vector),
                    _json(providers or {}),
                    _json(decision.unknowns),
                    _json(decision.reasons),
                    latency_ms,
                    settings.software_version,
                    settings.scoring_version,
                    settings.feature_version,
                    settings.model_version,
                    settings.config_fingerprint(),
                ),
            )
            self.conn.execute(
                "UPDATE candidates SET authoritative_state=?,state=?,evaluation_stage=?,entry_state=?,"
                "survival_grade=?,payoff_grade=?,updated_at=? WHERE id=?",
                (
                    state,
                    state,
                    decision.stage,
                    str(decision.entry_state),
                    str(decision.survival),
                    str(decision.payoff),
                    decided_at,
                    candidate_id,
                ),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO lifecycle_transitions_v14(candidate_id,from_state,to_state,reason,"
                "created_at,evidence_json) VALUES(?,?,?,?,?,?)",
                (
                    candidate_id,
                    previous[0],
                    state,
                    decision.reasons[0] if decision.reasons else "V14_DECISION",
                    decided_at,
                    _json({"stage": decision.stage, "unknowns": decision.unknowns}),
                ),
            )
            metric = "T0_DECISION" if decision.stage == "T0" else "STAGED_DECISION"
            self.conn.execute(
                "INSERT INTO latency_observations_v14(metric,observed_at,milliseconds,event_key) "
                "VALUES(?,?,?,?)",
                (metric, decided_at, latency_ms, str(launch_event_id or "")),
            )
            if launch_event_id and decision.stage == "T0":
                self.conn.execute(
                    "UPDATE launch_events SET processing_status='T0_EVALUATED' WHERE id=?",
                    (launch_event_id,),
                )
            return cur.rowcount == 1

    def record_immutable_call(
        self,
        candidate_id: int,
        tier: str,
        snapshot: dict[str, Any],
        settings: Any,
        signal_id: int | None = None,
    ) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO immutable_call_snapshots(candidate_id,signal_id,tier,call_timestamp,"
                "call_market_cap_usd,call_price_usd,call_liquidity_usd,call_score,confidence,entry_state,"
                "feature_vector_json,provider_evidence_json,software_version,scoring_version,feature_version,"
                "model_version,config_fingerprint,radar_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    signal_id,
                    tier,
                    snapshot.get("call_timestamp") or iso(),
                    snapshot.get("market_cap_usd"),
                    snapshot.get("price_usd"),
                    snapshot.get("liquidity_usd"),
                    snapshot.get("score"),
                    snapshot.get("confidence"),
                    snapshot.get("entry_state", "UNKNOWN"),
                    _json(snapshot.get("features") or {}),
                    _json(snapshot.get("providers") or {}),
                    settings.software_version,
                    settings.scoring_version,
                    settings.feature_version,
                    settings.model_version,
                    settings.config_fingerprint(),
                    settings.radar_version,
                ),
            )
            return cur.rowcount == 1

    def record_v15_decision(
        self,
        candidate_id: int,
        decision: Any,
        settings: Any,
        token_address: str,
        chain: str,
        market: Any,
    ) -> bool:
        """Persist a production V1.5 decision and an immutable first public-call truth."""
        now = market.captured_at
        payload = decision.to_dict()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO v15_decisions(candidate_id,observed_at,stage,runner_score,"
                "runner_grade,failure_score,failure_grade,survival_grade,setup_conviction,"
                "evidence_coverage,entry_status,signal_tier,critical_unknowns_json,failure_reasons_json,"
                "provider_conflicts_json,feature_vector_json,software_version,model_version,"
                "config_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    now,
                    str(decision.stage),
                    decision.runner_score,
                    decision.runner_grade,
                    decision.failure_score,
                    decision.failure_grade,
                    decision.survival_grade,
                    decision.setup_conviction,
                    decision.evidence_coverage,
                    str(decision.entry_status),
                    str(decision.signal_tier),
                    _json(decision.critical_unknowns),
                    _json(decision.failure_reasons),
                    _json(decision.provider_conflicts),
                    _json(decision.feature_vector),
                    settings.software_version,
                    settings.model_version,
                    settings.config_fingerprint(),
                ),
            )
            self.conn.execute(
                "UPDATE candidates SET runner_score=?,runner_grade=?,failure_score=?,failure_grade=?,"
                "survival_grade=?,setup_conviction=?,evidence_coverage=?,entry_state=?,"
                "critical_unknowns_json=?,failure_reasons_json=?,v15_signal_tier=?,updated_at=? WHERE id=?",
                (
                    decision.runner_score,
                    decision.runner_grade,
                    decision.failure_score,
                    decision.failure_grade,
                    decision.survival_grade,
                    decision.setup_conviction,
                    decision.evidence_coverage,
                    str(decision.entry_status),
                    _json(decision.critical_unknowns),
                    _json(decision.failure_reasons),
                    str(decision.signal_tier),
                    now,
                    candidate_id,
                ),
            )
            if str(decision.signal_tier) not in {"SILENT_WATCH", "REJECT"}:
                self.conn.execute(
                    "INSERT OR IGNORE INTO v15_t0_calls(candidate_id,call_timestamp,token_address,chain,"
                    "stage,market_cap_usd,price_usd,liquidity_usd,runner_score,failure_score,"
                    "setup_conviction,evidence_coverage,entry_status,fingerprint_json,software_version,"
                    "model_version,config_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        now,
                        token_address,
                        chain,
                        str(decision.stage),
                        market.market_cap_usd,
                        market.price_usd,
                        market.liquidity_usd,
                        decision.runner_score,
                        decision.failure_score,
                        decision.setup_conviction,
                        decision.evidence_coverage,
                        str(decision.entry_status),
                        _json(payload),
                        settings.software_version,
                        settings.model_version,
                        settings.config_fingerprint(),
                    ),
                )
            return cur.rowcount == 1

    def record_v15_provider_evidence(
        self, candidate_id: int, rows: Iterable[dict[str, Any]]
    ) -> int:
        """Persist sanitized provenance/freshness inputs consumed by a V1.5 decision."""
        inserted = 0
        with self._lock, self.conn:
            for row in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO provider_evidence_v15(candidate_id,field_name,value_json,"
                    "provider,retrieved_at,age_seconds,confidence,conflict_state) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        row["field_name"],
                        _json(row.get("value")),
                        row["provider"],
                        row["retrieved_at"],
                        max(0.0, float(row.get("age_seconds") or 0)),
                        max(0.0, min(1.0, float(row.get("confidence", 1)))),
                        row.get("conflict_state", "KNOWN"),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def record_v15_tradeability(
        self, candidate_id: int, observed_at: str, result: dict[str, Any]
    ) -> int:
        """Persist each notional impact estimate used by the production decision."""
        inserted = 0
        with self._lock, self.conn:
            for notional, estimate in (result.get("estimates") or {}).items():
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO tradeability_v15(candidate_id,observed_at,notional_usd,"
                    "buy_impact_percent,sell_impact_percent,exitability_grade,source_type,evidence_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        observed_at,
                        float(notional),
                        estimate.get("buy_impact_percent"),
                        estimate.get("sell_impact_percent"),
                        result.get("grade", "UNKNOWN"),
                        estimate.get("source", "ESTIMATE"),
                        _json(estimate),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def record_latency(
        self, metric: str, milliseconds: float, source: str | None = None, metadata: Any = None
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO latency_observations_v14(metric,observed_at,milliseconds,source,metadata_json) "
                "VALUES(?,?,?,?,?)",
                (metric, iso(), milliseconds, source, _json(metadata or {})),
            )

    def enqueue_v14_event(self, event_key: str, event_type: str, payload: dict[str, Any]) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (event_key, event_type, _json(payload), iso()),
            )
            return cur.rowcount == 1

    def latency_report(self) -> dict[str, dict[str, Any]]:
        from memecoin_bot.alpha_engine import latency_summary

        grouped: dict[str, list[float]] = {}
        for row in self.conn.execute(
            "SELECT metric,milliseconds FROM latency_observations_v14 ORDER BY metric"
        ):
            grouped.setdefault(str(row[0]), []).append(float(row[1]))
        return {metric: latency_summary(values) for metric, values in grouped.items()}

    def save_wallet_graph(self, chain: str, token_address: str, result: Any) -> list[int]:
        now = iso()
        cluster_ids: list[int] = []
        with self._lock, self.conn:
            for cluster in result.clusters:
                cluster_key = f"{chain}:" + ":".join(sorted(cluster))
                self.conn.execute(
                    "INSERT INTO wallet_clusters(chain,cluster_key,first_seen_at,last_seen_at,risk_state,evidence_json) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(chain,cluster_key) DO UPDATE SET last_seen_at=excluded.last_seen_at,"
                    "risk_state=excluded.risk_state,evidence_json=excluded.evidence_json",
                    (
                        chain,
                        cluster_key,
                        now,
                        now,
                        "COORDINATED" if result.coordinated else "CONNECTED",
                        _json(result.evidence),
                    ),
                )
                row = self.conn.execute(
                    "SELECT id FROM wallet_clusters WHERE chain=? AND cluster_key=?",
                    (chain, cluster_key),
                ).fetchone()
                assert row
                cluster_id = int(row[0])
                cluster_ids.append(cluster_id)
                for wallet in cluster:
                    self.conn.execute(
                        "INSERT INTO wallet_nodes(chain,wallet_address,first_seen_at,last_seen_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(chain,wallet_address) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                        (chain, wallet, now, now),
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO wallet_cluster_members(cluster_id,wallet_address,first_seen_at) "
                        "VALUES(?,?,?)",
                        (cluster_id, wallet, now),
                    )
                for left, right in pairwise(cluster):
                    self.conn.execute(
                        "INSERT INTO wallet_edges(chain,from_wallet,to_wallet,relationship,token_address,"
                        "observed_at,confidence,evidence_json) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(chain,from_wallet,to_wallet,relationship,token_address) DO UPDATE SET "
                        "observed_at=excluded.observed_at,evidence_json=excluded.evidence_json",
                        (
                            chain,
                            left,
                            right,
                            "SHARED_FUNDER",
                            token_address,
                            now,
                            0.8,
                            _json(result.evidence),
                        ),
                    )
        return cluster_ids

    def save_creator_profile(
        self, chain: str, creator: str, token_id: int, quality: dict[str, Any]
    ) -> None:
        now = iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO creator_profiles_v14(chain,creator_address,quality,launches,survived,rugs,runners,"
                "first_seen_at,last_seen_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(chain,creator_address) DO UPDATE SET quality=excluded.quality,launches=excluded.launches,"
                "survived=excluded.survived,rugs=excluded.rugs,runners=excluded.runners,last_seen_at=excluded.last_seen_at,"
                "evidence_json=excluded.evidence_json",
                (
                    chain,
                    creator,
                    str(quality.get("quality", "UNKNOWN")),
                    quality.get("sample", 0),
                    quality.get("survived", 0),
                    quality.get("rugs", 0),
                    quality.get("runners", 0),
                    now,
                    now,
                    _json(quality),
                ),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO creator_launches_v14(creator_address,token_id,launched_at) VALUES(?,?,?)",
                (creator, token_id, now),
            )

    def save_narrative_election(
        self, narrative_key: str, label: str, token_ids: dict[str, int], election: dict[str, Any]
    ) -> int:
        now = iso()
        leader_id = token_ids.get(str(election.get("leader")))
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO narratives_v14(narrative_key,label,first_seen_at,last_seen_at,freshness,saturation,"
                "leader_token_id,evidence_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(narrative_key) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,freshness=excluded.freshness,saturation=excluded.saturation,"
                "leader_token_id=excluded.leader_token_id,evidence_json=excluded.evidence_json",
                (
                    narrative_key,
                    label,
                    now,
                    now,
                    "FRESH",
                    election["saturation"],
                    leader_id,
                    _json(election),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM narratives_v14 WHERE narrative_key=?", (narrative_key,)
            ).fetchone()
            assert row
            narrative_id = int(row[0])
            for member in election.get("members", []):
                token_id = token_ids.get(str(member["token_address"]))
                if token_id:
                    self.conn.execute(
                        "INSERT INTO narrative_members_v14(narrative_id,token_id,role,joined_at,clone_penalty,"
                        "evidence_json) VALUES(?,?,?,?,?,?) ON CONFLICT(narrative_id,token_id) DO UPDATE SET "
                        "role=excluded.role,clone_penalty=excluded.clone_penalty,evidence_json=excluded.evidence_json",
                        (
                            narrative_id,
                            token_id,
                            member["role"],
                            member["detected_at"],
                            member["clone_penalty"],
                            _json(member),
                        ),
                    )
            return narrative_id

    def add_watch(self, guild_id: Any, user_id: Any, chain: str, token_address: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO watchlists(guild_id,user_id,chain,token_address,created_at) VALUES(?,?,?,?,?)",
                (str(guild_id), str(user_id), chain, token_address, iso()),
            )
            return cur.rowcount == 1

    def remove_watch(self, guild_id: Any, user_id: Any, chain: str, token_address: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM watchlists WHERE guild_id=? AND user_id=? AND chain=? AND token_address=?",
                (str(guild_id), str(user_id), chain, token_address),
            )
            return cur.rowcount == 1

    def user_watchlist(self, guild_id: Any, user_id: Any, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM watchlists WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
                (str(guild_id), str(user_id), limit),
            )
        ]

    def record_manual_scan(
        self,
        chain: str,
        token_address: str,
        requested_at: str,
        payload: dict[str, Any],
        latency_ms: float,
        guild_id: Any = None,
        user_id: Any = None,
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO manual_scans(guild_id,user_id,chain,token_address,requested_at,completed_at,"
                "result_state,payload_json,latency_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(guild_id) if guild_id is not None else None,
                    str(user_id) if user_id is not None else None,
                    chain,
                    token_address,
                    requested_at,
                    iso(),
                    str(payload.get("state", "UNKNOWN")),
                    _json(payload),
                    latency_ms,
                ),
            )
            return int(cur.lastrowid)

    def wallet_report(self, wallet: str) -> dict[str, Any]:
        nodes = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM wallet_nodes WHERE wallet_address=?", (wallet,)
            )
        ]
        edges = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM wallet_edges WHERE from_wallet=? OR to_wallet=? ORDER BY observed_at DESC LIMIT 20",
                (wallet, wallet),
            )
        ]
        clusters = [
            dict(row)
            for row in self.conn.execute(
                "SELECT wc.* FROM wallet_clusters wc JOIN wallet_cluster_members wm ON wm.cluster_id=wc.id "
                "WHERE wm.wallet_address=? ORDER BY wc.last_seen_at DESC",
                (wallet,),
            )
        ]
        return {"wallet": wallet, "nodes": nodes, "edges": edges, "clusters": clusters}

    def cluster_report(self, limit: int = 10) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT wc.*,COUNT(wm.wallet_address) member_count FROM wallet_clusters wc "
                "LEFT JOIN wallet_cluster_members wm ON wm.cluster_id=wc.id GROUP BY wc.id "
                "ORDER BY wc.last_seen_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def creator_report(self, creator: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM creator_profiles_v14 WHERE creator_address=? ORDER BY last_seen_at DESC LIMIT 1",
            (creator,),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["launch_history"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT cl.*,t.token_address,t.symbol FROM creator_launches_v14 cl JOIN tokens t ON t.id=cl.token_id "
                "WHERE cl.creator_address=? ORDER BY cl.launched_at DESC LIMIT 10",
                (creator,),
            )
        ]
        return value

    def narrative_report(self, query: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        sql = (
            "SELECT n.*,t.token_address leader_address,t.symbol leader_symbol FROM narratives_v14 n "
            "LEFT JOIN tokens t ON t.id=n.leader_token_id"
        )
        args: list[Any] = []
        if query:
            sql += " WHERE n.narrative_key LIKE ? OR n.label LIKE ?"
            args.extend((f"%{query}%", f"%{query}%"))
        sql += " ORDER BY n.last_seen_at DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.conn.execute(sql, args)]

    def reconcile_v14_state(self) -> dict[str, int]:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE candidates SET retry_count=attempt_count,"
                "consecutive_provider_failures=consecutive_provider_failure_count,"
                "consecutive_pair_missing=consecutive_missing_pair_count"
            )
            self.conn.execute(
                "UPDATE candidates SET authoritative_state=CASE WHEN state='EARLY_RADAR' THEN 'STANDARD_RADAR' "
                "WHEN state='SIGNALLED' THEN 'QUALIFIED_SIGNAL' ELSE state END "
                "WHERE authoritative_state IS NULL OR authoritative_state='DISCOVERED'"
            )
            # authoritative_state is the single lifecycle truth. `state` remains
            # a restart-compatible mirror for older queries and deployments.
            self.conn.execute(
                "UPDATE candidates SET state=authoritative_state "
                "WHERE authoritative_state IS NOT NULL AND authoritative_state!='' "
                "AND state!=authoritative_state"
            )
        row = self.conn.execute(
            "SELECT COUNT(*) total,SUM(CASE WHEN authoritative_state IS NULL OR authoritative_state='' "
            "OR state!=authoritative_state THEN 1 ELSE 0 END) missing "
            "FROM candidates"
        ).fetchone()
        return {"total": int(row[0] or 0), "difference": int(row[1] or 0)}

    def v14_health(self) -> dict[str, Any]:
        oldest = self.conn.execute(
            "SELECT MIN(first_discovered_at) FROM candidates WHERE state NOT IN ('EXPIRED','REJECTED_UNSAFE','SIGNALLED')"
        ).fetchone()[0]
        return {
            "event_queue_persisted": int(
                self.conn.execute("SELECT COUNT(*) FROM launch_events").fetchone()[0]
            ),
            "oldest_pending_at": oldest,
            "watchlist_entries": int(
                self.conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0]
            ),
            "wallet_clusters": int(
                self.conn.execute("SELECT COUNT(*) FROM wallet_clusters").fetchone()[0]
            ),
            "outbox_pending": len(self.pending_outbox()),
            "latency": self.latency_report(),
            "state_reconciliation": self.reconcile_v14_state(),
            "realtime_fabric": self.realtime_fabric_health(),
        }

    def realtime_curve_targets(self, limit: int = 500) -> list[dict[str, Any]]:
        """Active authoritative tokens whose real Pump curve account is known."""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT t.token_address,r.bonding_curve_address,r.monitoring_temperature,"
                "r.last_event_at FROM token_realtime_state r JOIN tokens t ON t.id=r.token_id "
                "LEFT JOIN candidates c ON c.token_id=t.id WHERE r.bonding_curve_address IS NOT NULL "
                "AND COALESCE(r.curve_complete,0)!=1 AND COALESCE(c.authoritative_state,'DISCOVERED') "
                "NOT IN ('EXPIRED','REJECTED_UNSAFE','FAILED_OUTCOME') "
                "ORDER BY CASE r.monitoring_temperature WHEN 'GENESIS' THEN 0 WHEN 'HOT' THEN 1 "
                "WHEN 'WARM' THEN 2 ELSE 3 END,r.last_event_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def realtime_fabric_health(self) -> dict[str, Any]:
        tables = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('canonical_events','token_event_timeline_v15','curve_observations_v15')"
            )
        }
        if "canonical_events" not in tables:
            return {"state": "NOT_MIGRATED"}
        row = self.conn.execute(
            "SELECT COUNT(*) total,SUM(processing_status='PENDING') pending,"
            "SUM(processing_status='PROCESSING') processing,SUM(processing_status='FAILED') failed,"
            "SUM(processing_status='PROCESSED') processed FROM canonical_events"
        ).fetchone()
        coverage = self.conn.execute(
            "SELECT COUNT(DISTINCT token_id) tokens,"
            "SUM(real_sol_reserves IS NOT NULL) real_sol_observations FROM curve_observations_v15"
        ).fetchone()
        return {
            "state": "ACTIVE",
            "total": int(row[0] or 0),
            "pending": int(row[1] or 0),
            "processing": int(row[2] or 0),
            "failed": int(row[3] or 0),
            "processed": int(row[4] or 0),
            "curve_tokens": int(coverage[0] or 0),
            "real_sol_observations": int(coverage[1] or 0),
            "runner_theses": int(
                self.conn.execute("SELECT COUNT(*) FROM runner_theses_v15").fetchone()[0]
            ),
            "prospective_shadow_calls": int(
                self.conn.execute("SELECT COUNT(*) FROM prospective_shadow_calls_v15").fetchone()[0]
            ),
            "prospective_shadow_outcomes": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM prospective_shadow_outcomes_v15"
                ).fetchone()[0]
            ),
            "runner_reflections": int(
                self.conn.execute("SELECT COUNT(*) FROM runner_reflections_v15").fetchone()[0]
            ),
            "latency": self.realtime_latency_report(),
        }

    def realtime_latency_report(self) -> dict[str, dict[str, Any]]:
        from memecoin_bot.alpha_engine import latency_summary, percentile

        fields = {
            "provider": ("source_timestamp", "received_timestamp"),
            "availability": ("received_timestamp", "available_timestamp"),
            "source_to_feature": ("source_timestamp", "feature_ready_timestamp"),
            "model": ("model_start_timestamp", "model_finish_timestamp"),
            "source_to_decision": ("source_timestamp", "decision_timestamp"),
            "discord_queue": ("decision_timestamp", "discord_enqueue_timestamp"),
            "discord_delivery": ("discord_enqueue_timestamp", "discord_sent_timestamp"),
            "source_to_discord": ("source_timestamp", "discord_sent_timestamp"),
        }
        grouped: dict[str, list[float]] = {name: [] for name in fields}
        rows = self.conn.execute(
            "SELECT source_timestamp,received_timestamp,available_timestamp,"
            "feature_ready_timestamp,model_start_timestamp,model_finish_timestamp,"
            "decision_timestamp,discord_enqueue_timestamp,discord_sent_timestamp "
            "FROM canonical_events WHERE event_type!='PROVIDER_HEALTH'"
        )
        for row in rows:
            for name, (start, finish) in fields.items():
                if row[start] is None or row[finish] is None:
                    continue
                elapsed = (
                    datetime.fromisoformat(str(row[finish]))
                    - datetime.fromisoformat(str(row[start]))
                ).total_seconds() * 1_000
                if elapsed >= 0:
                    grouped[name].append(elapsed)
        return {
            name: {
                **latency_summary(values),
                "p90_ms": percentile(values, 0.90),
                "p99_ms": percentile(values, 0.99),
            }
            for name, values in grouped.items()
        }

    def right_tail_performance(self, min_sample: int = 30) -> dict[str, Any]:
        from memecoin_bot.alpha_engine import right_tail_metrics

        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT t.token_address,COALESCE(o.max_multiple_from_discovery,1) peak_multiple,"
                "CASE WHEN s.id IS NOT NULL THEN 'QUALIFIED_SIGNAL' ELSE c.authoritative_state END highest_tier "
                "FROM tokens t LEFT JOIN candidates c ON c.token_id=t.id "
                "LEFT JOIN token_outcomes o ON o.token_id=t.id LEFT JOIN signals s ON s.token_id=t.id"
            )
        ]
        return right_tail_metrics(rows, min_sample)

    def v15_performance(self, min_sample: int = 30) -> dict[str, Any]:
        """Observed cohort discrimination; never labels small samples as edge."""
        rows = [
            dict(row)
            for row in self.conn.execute(
                "WITH latest AS (SELECT candidate_id,MAX(id) id FROM v15_decisions GROUP BY candidate_id) "
                "SELECT d.signal_tier,d.stage,t.chain,COUNT(*) sample,"
                "SUM(CASE WHEN o.max_multiple_from_discovery>=2 THEN 1 ELSE 0 END) runners_2x,"
                "SUM(CASE WHEN o.max_multiple_from_discovery>=5 THEN 1 ELSE 0 END) runners_5x,"
                "SUM(CASE WHEN o.max_multiple_from_discovery>=10 THEN 1 ELSE 0 END) runners_10x,"
                "SUM(CASE WHEN o.final_lifecycle_state LIKE 'FAILED%' "
                "AND COALESCE(o.max_multiple_from_discovery,0)<2 "
                "THEN 1 ELSE 0 END) failed_before_2x "
                "FROM latest l JOIN v15_decisions d ON d.id=l.id "
                "JOIN candidates c ON c.id=d.candidate_id "
                "JOIN tokens t ON t.id=c.token_id LEFT JOIN token_outcomes o ON o.token_id=t.id "
                "GROUP BY d.signal_tier,d.stage,t.chain ORDER BY d.signal_tier,d.stage,t.chain"
            )
        ]
        for row in rows:
            sample = int(row["sample"] or 0)
            row["runner_2x_rate"] = (row["runners_2x"] or 0) / sample if sample else None
            row["failure_rate"] = (row["failed_before_2x"] or 0) / sample if sample else None
            row["mature_enough_for_edge_claim"] = sample >= min_sample
        return {
            "cohorts": rows,
            "warning": (
                None
                if rows and all(row["mature_enough_for_edge_claim"] for row in rows)
                else "SMALL_OR_IMMATURE_SAMPLE_NO_EDGE_CLAIM"
            ),
        }
