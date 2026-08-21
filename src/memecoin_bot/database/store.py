from __future__ import annotations

import json
import re
import sqlite3
import threading
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
                    existing = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
                    return "" if column in existing else match.group(0)
                script = re.sub(
                    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+[^;]+;",
                    skip_existing_column, script, flags=re.IGNORECASE | re.MULTILINE,
                )
                self.conn.executescript(script)
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (file.name, iso()),
                )

    def close(self) -> None:
        self.conn.close()

    def register_scoring_version(self, version: str, weights: dict[str, float], thresholds: dict[str, float]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scoring_versions VALUES (?, ?, ?, ?)",
                (version, _json(weights), _json(thresholds), iso()),
            )

    def register_config_fingerprint(self, fingerprint: str, software_version: str,
                                    scoring_version: str, radar_version: str,
                                    config: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO config_fingerprints VALUES(?,?,?,?,?,?)",
                (fingerprint, software_version, scoring_version, radar_version, _json(config), iso()),
            )

    def upsert_discovery(self, event: DiscoveryEvent) -> tuple[int, bool]:
        """Return token id and whether it was newly inserted."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO tokens(chain,token_address,symbol,name,source,"
                "first_discovered_at,estimated_created_at,pair_address,deployer,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event.chain, event.token_address, event.symbol, event.name, event.source,
                 event.discovered_at, event.estimated_creation_timestamp, event.pair_address,
                 event.deployer, _json(event.metadata)),
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
                (row["id"], event.source, event.discovered_at, event.discovered_at, _json(event.metadata)),
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
        return self.conn.execute(
            "SELECT 1 FROM evaluations WHERE token_id=? LIMIT 1", (token_id,)
        ).fetchone() is not None

    def save_snapshot(self, token_id: int, snapshot: MarketSnapshot, holder_count: int | None = None) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO token_snapshots(token_id,captured_at,source,market_cap_usd,price_usd,"
                "liquidity_usd,volume_5m_usd,holder_count,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (token_id, snapshot.captured_at, snapshot.source, snapshot.market_cap_usd,
                 snapshot.price_usd, snapshot.liquidity_usd, snapshot.volume_5m_usd,
                 holder_count, _json(snapshot.to_dict())),
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
                    (token_id, snapshot.market_cap_usd, snapshot.market_cap_usd, 1.0, now, now, now),
                )
                outcome = self.conn.execute(
                    "SELECT discovery_market_cap_usd,max_multiple_from_discovery,radar_occurred,signal_id "
                    "FROM token_outcomes WHERE token_id=?", (token_id,),
                ).fetchone()
                for multiple in (2, 3, 5, 10, 20, 50, 100):
                    if float(outcome["max_multiple_from_discovery"] or 0) >= multiple:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO outcome_milestones(token_id,multiple,hit_at,market_cap_usd,"
                            "radar_before_hit,signal_before_hit) VALUES(?,?,?,?,?,?)",
                            (token_id, multiple, now, snapshot.market_cap_usd,
                             int(bool(outcome["radar_occurred"])), int(outcome["signal_id"] is not None)),
                        )
            return int(cur.lastrowid)

    def recent_snapshots(self, token_id: int, limit: int = 3) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM token_snapshots WHERE token_id=? ORDER BY id DESC LIMIT ?",
            (token_id, limit),
        ))

    def ensure_candidate(self, token_id: int, discovered_at: str, scoring_version: str) -> tuple[int, bool]:
        now = iso()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO candidates(token_id,state,reason,first_discovered_at,"
                "scoring_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (token_id, CandidateState.DISCOVERED, "AWAITING_INITIAL_SCREEN", discovered_at,
                 scoring_version, now, now),
            )
            row = self.conn.execute("SELECT id FROM candidates WHERE token_id=?", (token_id,)).fetchone()
            assert row
            candidate_id = int(row[0])
            if cur.rowcount == 1:
                self.conn.execute(
                    "INSERT INTO candidate_transitions(candidate_id,to_state,reason,created_at) VALUES(?,?,?,?)",
                    (candidate_id, CandidateState.DISCOVERED, "DISCOVERY", now),
                )
            return candidate_id, cur.rowcount == 1

    def candidate_for_token(self, token_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM candidates WHERE token_id=?", (token_id,)).fetchone()

    def active_candidates(self, limit: int = 250, per_chain: int | None = None) -> list[sqlite3.Row]:
        terminal = (CandidateState.REJECTED_UNSAFE, CandidateState.EXPIRED)
        chain_limit = per_chain or limit
        return list(self.conn.execute(
            "WITH ranked AS (SELECT c.*,t.token_address,t.symbol,t.name,t.pair_address,t.chain,t.metadata_json,"
            "t.first_discovered_at token_discovered_at,ROW_NUMBER() OVER (PARTITION BY t.chain "
            "ORDER BY COALESCE(c.last_monitored_at,c.first_discovered_at),c.id) chain_rank "
            "FROM candidates c JOIN tokens t ON t.id=c.token_id WHERE c.state NOT IN (?,?)) "
            "SELECT * FROM ranked WHERE chain_rank<=? "
            "ORDER BY COALESCE(last_monitored_at,first_discovered_at),id LIMIT ?",
            (*terminal, chain_limit, limit),
        ))

    def trigger_radar(self, candidate_id: int, score: float, reasons: list[str],
                      snapshot: MarketSnapshot, payload: dict[str, Any], level: str = "EARLY_RADAR") -> bool:
        now = snapshot.captured_at
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO radar_events(candidate_id,event_level,triggered_at,radar_score,"
                "reason_json,market_cap_usd,price_usd,liquidity_usd,snapshot_count,immutable_payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,(SELECT COUNT(*) FROM token_snapshots WHERE token_id="
                "(SELECT token_id FROM candidates WHERE id=?)),?)",
                (candidate_id, level, now, score, _json(reasons), snapshot.market_cap_usd,
                 snapshot.price_usd, snapshot.liquidity_usd, candidate_id, _json(payload)),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "UPDATE candidates SET state='EARLY_RADAR',radar_score=?,radar_reason=?,radar_triggered_at=?,"
                "radar_market_cap_usd=?,radar_price_usd=?,radar_liquidity_usd=?,updated_at=? WHERE id=?",
                (score, ";".join(reasons), now, snapshot.market_cap_usd, snapshot.price_usd,
                 snapshot.liquidity_usd, now, candidate_id),
            )
            token_id = self.conn.execute("SELECT token_id FROM candidates WHERE id=?", (candidate_id,)).fetchone()[0]
            self.conn.execute(
                "UPDATE token_outcomes SET radar_occurred=1,updated_at=? WHERE token_id=?", (now, token_id)
            )
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"radar:{candidate_id}:{level}", "EARLY_RADAR", _json(payload), now),
            )
            return True

    def update_candidate(
        self, candidate_id: int, state: CandidateState | str, reason: str,
        snapshot: MarketSnapshot | None = None, score: ScoreResult | None = None,
        hard_rejections: list[str] | None = None, waiting_reasons: list[str] | None = None,
        unknown_fields: list[str] | None = None, signal_id: int | None = None,
        expired: bool = False,
    ) -> None:
        now = snapshot.captured_at if snapshot else iso()
        with self._lock, self.conn:
            old = self.conn.execute("SELECT state FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not old:
                raise KeyError(candidate_id)
            values = {
                "state": str(state), "reason": reason, "updated_at": now,
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
                values.update({
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
                })
            assignments = ["state=:state", "reason=:reason", "updated_at=:updated_at",
                           "hard_rejections_json=:hard_rejections_json",
                           "waiting_reasons_json=:waiting_reasons_json",
                           "unknown_fields_json=:unknown_fields_json"]
            for key in ("last_monitored_at", "last_evaluated_at", "raw_points", "normalized_score",
                        "confidence", "classification", "signal_id", "expired_at"):
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
                    "current_volume_5m_usd=:current_volume_5m_usd", "current_buys_5m=:current_buys_5m",
                    "current_sells_5m=:current_sells_5m",
                    "snapshot_count=(SELECT COUNT(*) FROM token_snapshots WHERE token_id=(SELECT token_id FROM candidates WHERE id=:id))",
                ]
            values["id"] = candidate_id
            self.conn.execute(f"UPDATE candidates SET {','.join(assignments)} WHERE id=:id", values)
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
                    (candidate_id, old[0], str(state), reason,
                     score.normalized_score if score else None, score.confidence if score else None, now),
                )

    def candidates_report(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT c.*,t.symbol,t.name,t.token_address,t.chain FROM candidates c JOIN tokens t ON t.id=c.token_id "
            "WHERE c.state NOT IN ('REJECTED_UNSAFE','EXPIRED','SIGNALLED') "
            "ORDER BY COALESCE(c.normalized_score,-1) DESC,c.updated_at DESC LIMIT ?", (limit,)
        ))

    def rejection_report(self, since: str) -> dict[str, Any]:
        hard: dict[str, int] = {}
        temporary: dict[str, int] = {}
        for row in self.conn.execute("SELECT hard_rejections_json,waiting_reasons_json FROM candidates WHERE updated_at>=?", (since,)):
            for reason in json.loads(row[0]): hard[reason] = hard.get(reason, 0) + 1
            for reason in json.loads(row[1]): temporary[reason] = temporary.get(reason, 0) + 1
        return {"hard": sorted(hard.items(), key=lambda x: (-x[1], x[0])),
                "temporary": sorted(temporary.items(), key=lambda x: (-x[1], x[0]))}

    def save_evaluation(
        self, token_id: int, score: ScoreResult, evidence: dict[str, Any], evaluated_at: str | None = None
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO evaluations(token_id,evaluated_at,classification,score,confidence,"
                "hard_rejections_json,component_scores_json,evidence_json,scoring_version,normalized_score,available_weight) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (token_id, evaluated_at or iso(), score.classification, score.total, score.confidence,
                 _json(score.hard_rejections), _json(score.component_scores), _json(evidence),
                 score.scoring_version, score.normalized_score, score.available_weight),
            )
            return int(cur.lastrowid)

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
                "FROM candidates WHERE token_id=?", (token_id,),
            ).fetchone()
            radar_at = candidate["radar_triggered_at"] if candidate else None
            radar_mc = candidate["radar_market_cap_usd"] if candidate else None
            signalled_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
            discovery_seconds = None
            radar_seconds = None
            radar_multiple = None
            if candidate:
                discovery_seconds = (signalled_at - datetime.fromisoformat(
                    candidate["first_discovered_at"].replace("Z", "+00:00")
                )).total_seconds()
            if radar_at:
                radar_seconds = (signalled_at - datetime.fromisoformat(
                    radar_at.replace("Z", "+00:00")
                )).total_seconds()
                if radar_mc and float(radar_mc) > 0:
                    radar_multiple = snapshot.market_cap_usd / float(radar_mc)
            cur = self.conn.execute(
                "INSERT INTO signals(token_id,signal_timestamp,signal_price_usd,signal_market_cap_usd,"
                "signal_liquidity_usd,signal_holder_count,signal_volume_5m_usd,signal_score,signal_class,"
                "component_scores_json,developer_state_json,narrative_state_json,social_state_json,"
                "onchain_state_json,risk_flags_json,scoring_version,current_market_cap_usd,current_score,"
                "ath_market_cap_usd,atl_market_cap_usd,last_monitored_at,created_at,normalized_score,confidence,"
                "candidate_history_json,current_signal_class,radar_timestamp,radar_market_cap_usd,"
                "radar_to_signal_seconds,radar_to_signal_multiple,discovery_to_signal_seconds) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (token_id, now, snapshot.price_usd, snapshot.market_cap_usd, snapshot.liquidity_usd,
                 holder_count, snapshot.volume_5m_usd, score.total, score.classification,
                 _json(score.component_scores), _json(states.get("developer", {})),
                 _json(states.get("narrative", {})), _json(states.get("social", {})),
                 _json(states.get("onchain", {})), _json(risk_flags), score.scoring_version,
                 snapshot.market_cap_usd, score.normalized_score, snapshot.market_cap_usd,
                 snapshot.market_cap_usd, now, now, score.normalized_score, score.confidence,
                 _json(states.get("candidate_history", {})), str(score.classification), radar_at, radar_mc,
                 radar_seconds, radar_multiple, discovery_seconds),
            )
            signal_id = int(cur.lastrowid)
            payload = dict(message_payload, signal_id=signal_id)
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"signal:{signal_id}", "SIGNAL", _json(payload), now),
            )
            self.conn.execute(
                "UPDATE token_outcomes SET signal_id=?,final_lifecycle_state='SIGNALLED',"
                "non_signal_reason=NULL,updated_at=? WHERE token_id=?", (signal_id, now, token_id),
            )
            return signal_id

    def active_signals(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT s.*,t.token_address,t.symbol,t.name,t.chain,t.pair_address FROM signals s "
            "JOIN tokens t ON t.id=s.token_id WHERE s.active=1 ORDER BY s.id"
        ))

    def signal(self, signal_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()

    def update_signal_intelligence(self, signal_id: int, new_class: str, score: ScoreResult,
                                   reasons: list[str], payload: dict[str, Any]) -> str | None:
        rank = {"WATCH": 1, "STRONG": 2, "HIGH_CONVICTION": 3}
        now = iso()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT signal_class,COALESCE(current_signal_class,signal_class) current_class,current_score "
                "FROM signals WHERE id=?", (signal_id,),
            ).fetchone()
            if not row:
                return None
            old = str(row["current_class"])
            update_type = "UPGRADE" if rank.get(new_class, 0) > rank.get(old, 0) else "DETERIORATION"
            event_key = (f"upgrade:{signal_id}:{new_class}" if update_type == "UPGRADE" else
                         f"deterioration:{signal_id}:{new_class}:{','.join(sorted(reasons))}")
            if self.conn.execute("SELECT 1 FROM outbox WHERE event_key=?", (event_key,)).fetchone():
                return None
            self.conn.execute(
                "INSERT INTO signal_updates(signal_id,update_timestamp,update_type,previous_score,new_score,reasons_json) "
                "VALUES(?,?,?,?,?,?)",
                (signal_id, now, update_type, row["current_score"], score.normalized_score, _json(reasons)),
            )
            self.conn.execute(
                "UPDATE signals SET current_signal_class=?,current_score=? WHERE id=?",
                (new_class if update_type == "UPGRADE" else old, score.normalized_score, signal_id),
            )
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (event_key, update_type, _json(dict(payload, signal_id=signal_id,
                                                   previous_class=old, new_class=new_class,
                                                   reasons=reasons)), now),
            )
            return update_type

    def update_tracking(
        self, signal_id: int, market_cap: float, monitored_at: str,
        max_multiple: float, max_drawdown: float, ath: float, atl: float,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE signals SET current_market_cap_usd=?,last_monitored_at=?,max_multiple=?,"
                "max_drawdown=?,ath_market_cap_usd=?,atl_market_cap_usd=? WHERE id=?",
                (market_cap, monitored_at, max_multiple, max_drawdown, ath, atl, signal_id),
            )

    def record_milestones(
        self, signal_id: int, candidates: Iterable[tuple[float, float, float]], payload_base: dict[str, Any]
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
                payload = dict(payload_base, signal_id=signal_id, milestone=multiple,
                               market_cap_usd=market_cap, seconds_to_hit=seconds_to_hit)
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
                "WHERE id=? AND active=1", (now, signal_id),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "INSERT OR IGNORE INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"failed:{signal_id}", "FAILED", _json(dict(payload, signal_id=signal_id)), now),
            )
            return True

    def pending_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,)
        ))

    def mark_outbox_sent(self, outbox_id: int, remote_id: str | None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE outbox SET sent_at=?,remote_message_id=?,attempts=attempts+1,last_error=NULL WHERE id=?",
                (iso(), remote_id, outbox_id),
            )

    def mark_outbox_error(self, outbox_id: int, error: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=?",
                (error[:1000], outbox_id),
            )

    def ensure_alert_deliveries(self, outbox_id: int, channel_ids: Iterable[int]) -> None:
        with self._lock, self.conn:
            for channel_id in channel_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO alert_deliveries(outbox_id,channel_id) VALUES(?,?)",
                    (outbox_id, str(channel_id)),
                )

    def pending_alert_deliveries(self, outbox_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM alert_deliveries WHERE outbox_id=? AND status!='SENT' ORDER BY id", (outbox_id,)
        ))

    def mark_alert_delivery(self, delivery_id: int, success: bool, remote_id: str | None = None,
                            error: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE alert_deliveries SET status=?,attempts=attempts+1,remote_message_id=?,"
                "delivered_at=?,last_error=? WHERE id=?",
                ("SENT" if success else "FAILED", remote_id, iso() if success else None,
                 None if success else str(error or "")[:1000], delivery_id),
            )

    def set_provider_health(self, provider: str, healthy: bool, failures: int, error: str | None) -> None:
        now = iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO provider_health(provider,healthy,consecutive_failures,last_success_at,"
                "last_failure_at,last_error,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET healthy=excluded.healthy,"
                "consecutive_failures=excluded.consecutive_failures,"
                "last_success_at=CASE WHEN excluded.healthy=1 THEN excluded.updated_at ELSE provider_health.last_success_at END,"
                "last_failure_at=CASE WHEN excluded.healthy=0 THEN excluded.updated_at ELSE provider_health.last_failure_at END,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at",
                (provider, int(healthy), failures, now if healthy else None,
                 None if healthy else now, error, now),
            )

    def save_gmgn_intelligence(self, token_id: int, snapshot: dict[str, Any],
                               wallet: dict[str, Any]) -> int:
        """Persist raw GMGN evidence and derived wallet evidence without flattening labels."""
        captured = str(snapshot.get("retrieved_at") or iso())
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO gmgn_snapshots(token_id,captured_at,payload_json,unavailable_json) "
                "VALUES(?,?,?,?)", (token_id, captured, _json(snapshot), _json(snapshot.get("unavailable", []))),
            )
            self.conn.execute(
                "INSERT INTO wallet_intelligence(token_id,captured_at,smart_money_state,buyer_diversity,"
                "activity_quality,payload_json) VALUES(?,?,?,?,?,?)",
                (token_id, captured, wallet.get("smart_money", "SMART_MONEY_UNKNOWN"),
                 wallet.get("buyer_diversity", "UNKNOWN"), wallet.get("activity_quality", "UNKNOWN"),
                 _json(wallet)),
            )
            for field in ("info", "security", "pool", "holders", "traders"):
                value = snapshot.get(field)
                self.conn.execute(
                    "INSERT OR IGNORE INTO provider_evidence(token_id,field_name,value_json,provider,"
                    "retrieved_at,confidence,raw_json) VALUES(?,?,?,?,?,?,?)",
                    (token_id, field, _json(value), "gmgn", captured,
                     "KNOWN" if value is not None else "UNKNOWN", _json(value)),
                )
            return int(cur.lastrowid or 0)

    def record_intelligence_event(self, token_id: int, event_key: str, event_type: str,
                                  evidence: dict[str, Any]) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO intelligence_events(token_id,event_key,event_type,detected_at,evidence_json) "
                "VALUES(?,?,?,?,?)", (token_id, event_key, event_type, iso(), _json(evidence)),
            )
            return cur.rowcount == 1

    def state_reconciliation(self) -> dict[str, Any]:
        total = int(self.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])
        states = {str(r[0]): int(r[1]) for r in self.conn.execute(
            "SELECT COALESCE(c.state,'DISCOVERED'),COUNT(*) FROM tokens t LEFT JOIN candidates c "
            "ON c.token_id=t.id GROUP BY COALESCE(c.state,'DISCOVERED')"
        )}
        accounted = sum(states.values())
        return {"total_tracked": total, "states": states, "accounted": accounted,
                "difference": total - accounted, "reconciled": total == accounted}

    def token_intelligence(self, address: str, chain: str | None = None) -> dict[str, Any] | None:
        args: list[Any] = [address]
        chain_sql = ""
        if chain:
            chain_sql, args = " AND t.chain=?", [address, chain]
        row = self.conn.execute(
            "SELECT t.*,c.state,c.radar_score,c.radar_triggered_at,c.radar_market_cap_usd,"
            "c.current_market_cap_usd,c.current_liquidity_usd,c.normalized_score,c.confidence,"
            "s.max_multiple,s.ath_market_cap_usd,s.atl_market_cap_usd,s.status signal_status "
            "FROM tokens t LEFT JOIN candidates c ON c.token_id=t.id LEFT JOIN signals s ON s.token_id=t.id "
            "WHERE t.token_address=?" + chain_sql + " ORDER BY t.id DESC LIMIT 1", args,
        ).fetchone()
        if not row: return None
        result = dict(row)
        gmgn = self.conn.execute(
            "SELECT payload_json FROM gmgn_snapshots WHERE token_id=? ORDER BY captured_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        wallet = self.conn.execute(
            "SELECT payload_json FROM wallet_intelligence WHERE token_id=? ORDER BY captured_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        result["gmgn"] = json.loads(gmgn[0]) if gmgn else None
        result["wallet_intelligence"] = json.loads(wallet[0]) if wallet else None
        result["timeline"] = [dict(x) for x in self.conn.execute(
            "SELECT event_type,detected_at,evidence_json FROM intelligence_events WHERE token_id=? ORDER BY detected_at", (row["id"],)
        )]
        result["snapshots"] = [dict(x) for x in self.conn.execute(
            "SELECT captured_at,market_cap_usd,liquidity_usd FROM token_snapshots WHERE token_id=? ORDER BY captured_at", (row["id"],)
        )]
        return result

    def radar_board(self, limit: int = 250) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT t.name,t.symbol,t.chain,t.token_address,c.state,c.radar_score,c.normalized_score,"
            "c.confidence,c.radar_market_cap_usd,c.current_market_cap_usd,c.current_liquidity_usd,"
            "c.radar_triggered_at,c.updated_at,s.max_multiple,s.ath_market_cap_usd,s.status signal_status "
            "FROM candidates c JOIN tokens t ON t.id=c.token_id LEFT JOIN signals s ON s.id=c.signal_id "
            "ORDER BY COALESCE(c.radar_score,0) DESC,c.updated_at DESC LIMIT ?", (limit,)
        )]

    def status_stats(self, started_at: str) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        one = lambda sql, args=(): self.conn.execute(sql, args).fetchone()[0]
        providers = self.conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(healthy),0) FROM provider_health"
        ).fetchone()
        provider_rows = [dict(r) for r in self.conn.execute(
            "SELECT provider,healthy,consecutive_failures,last_success_at,last_error,updated_at FROM provider_health ORDER BY provider"
        )]
        for provider in provider_rows:
            provider["state"] = ("OK" if provider["healthy"] else
                "PARTIAL" if provider.get("last_success_at") else "OFFLINE")
        result = {
            "started_at": started_at,
            "tokens_discovered": one("SELECT COUNT(*) FROM tokens"),
            "tokens_evaluated": one("SELECT COUNT(DISTINCT token_id) FROM evaluations"),
            "hard_rejected": one("SELECT COUNT(*) FROM candidates WHERE state='REJECTED_UNSAFE'"),
            "pending_evidence": one("SELECT COUNT(*) FROM candidates WHERE state IN ('PENDING_EVIDENCE','FAILED_PROVIDER','DISCOVERED','SCREENING')"),
            "candidates_watching": one("SELECT COUNT(*) FROM candidates WHERE state='CANDIDATE'"),
            "early_radar": one("SELECT COUNT(*) FROM candidates WHERE radar_triggered_at IS NOT NULL"),
            "expired": one("SELECT COUNT(*) FROM candidates WHERE state='EXPIRED'"),
            "signals": one("SELECT COUNT(*) FROM signals"),
            "outcomes_tracked": one("SELECT COUNT(*) FROM token_outcomes"),
            "watch": one("SELECT COUNT(*) FROM signals WHERE signal_class='WATCH'"),
            "strong": one("SELECT COUNT(*) FROM signals WHERE signal_class='STRONG'"),
            "high_conviction": one("SELECT COUNT(*) FROM signals WHERE signal_class='HIGH_CONVICTION'"),
            "active_signals": one("SELECT COUNT(*) FROM signals WHERE active=1"),
            "signals_today": one("SELECT COUNT(*) FROM signals WHERE signal_timestamp LIKE ?", (today + "%",)),
            "providers_healthy": int(providers[1]),
            "providers_total": int(providers[0]),
            "provider_status": provider_rows,
            "database": "OK",
        }
        result["state_reconciliation"] = self.state_reconciliation()
        return result

    def missed_report(self, since: str | None, threshold: float, limit: int = 10) -> list[sqlite3.Row]:
        where = "WHERE m.signal_before_hit=0"
        args: list[Any] = [threshold]
        if since:
            where += " AND m.hit_at>=?"
            args.append(since)
        args.append(limit)
        return list(self.conn.execute(
            "SELECT o.*,m.radar_before_hit,m.signal_before_hit,m.hit_at threshold_hit_at,"
            "t.chain,t.token_address,t.symbol,t.name,c.reason,c.confidence,c.state "
            "FROM token_outcomes o JOIN tokens t ON t.id=o.token_id "
            "JOIN outcome_milestones m ON m.token_id=o.token_id AND m.multiple=? "
            "LEFT JOIN candidates c ON c.token_id=t.id " + where +
            " ORDER BY o.max_multiple_from_discovery DESC LIMIT ?", args,
        ))

    def outcome_watchlist(self, since: str, limit: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT o.*,t.chain,t.token_address FROM token_outcomes o "
            "JOIN tokens t ON t.id=o.token_id JOIN candidates c ON c.token_id=t.id "
            "WHERE o.signal_id IS NULL AND c.state IN ('EXPIRED','REJECTED_UNSAFE') "
            "AND t.first_discovered_at>=? ORDER BY o.last_observed_at LIMIT ?", (since, limit),
        ))

    def coverage(self, since: str | None, major_multiple: float) -> dict[str, Any]:
        where = "WHERE m.multiple=?"
        args: list[Any] = [major_multiple]
        if since:
            where += " AND m.hit_at>=?"
            args.append(since)
        rows = list(self.conn.execute(
            "SELECT o.*,m.radar_before_hit,m.signal_before_hit,m.hit_at threshold_hit_at "
            "FROM token_outcomes o JOIN outcome_milestones m ON m.token_id=o.token_id " + where,
            args,
        ))
        major = len(rows)
        radar = sum(bool(r["radar_before_hit"]) for r in rows)
        signalled = sum(bool(r["signal_before_hit"]) for r in rows)
        complete_miss = sum(not r["radar_before_hit"] and not r["signal_before_hit"] for r in rows)
        latency = list(self.conn.execute(
            "SELECT discovery_to_signal_seconds,radar_to_signal_seconds,radar_to_signal_multiple "
            "FROM signals WHERE discovery_to_signal_seconds IS NOT NULL" +
            (" AND signal_timestamp>=?" if since else ""), ([since] if since else []),
        ))
        return {
            "major_runner_multiple": major_multiple, "major_runners_discovered": major,
            "major_runners_radar": radar, "major_runners_signalled": signalled,
            "major_runners_completely_missed": complete_miss,
            "radar_recall": radar / major * 100 if major else None,
            "signal_recall": signalled / major * 100 if major else None,
            "complete_miss_rate": complete_miss / major * 100 if major else None,
            "median_discovery_to_signal_seconds": statistics.median(
                [float(r[0]) for r in latency if r[0] is not None]
            ) if latency else None,
            "median_radar_to_signal_seconds": statistics.median(
                [float(r[1]) for r in latency if r[1] is not None]
            ) if any(r[1] is not None for r in latency) else None,
            "median_radar_to_signal_multiple": statistics.median(
                [float(r[2]) for r in latency if r[2] is not None]
            ) if any(r[2] is not None for r in latency) else None,
        }

    def performance(self, version: str, since: str | None = None,
                    major_multiple: float = 10) -> dict[str, Any]:
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
            for m in self.conn.execute(f"SELECT signal_id,multiple FROM milestones WHERE signal_id IN ({marks})", ids):
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
            durations = [float(x[0]) for x in self.conn.execute(
                "SELECT m.seconds_to_hit FROM milestones m JOIN signals s ON s.id=m.signal_id "
                f"WHERE s.scoring_version=? AND m.multiple=?{' AND s.signal_timestamp>=?' if since else ''}",
                [version, target] + ([since] if since else []),
            )]
            result[f"median_seconds_to_{target}x"] = statistics.median(durations) if durations else None
        result["coverage"] = self.coverage(since, major_multiple)
        return result
