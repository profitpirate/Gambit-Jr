from __future__ import annotations

import json
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
                self.conn.executescript(file.read_text(encoding="utf-8"))
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
            return int(row["id"]), created

    def token_id(self, address: str) -> int | None:
        row = self.conn.execute("SELECT id FROM tokens WHERE token_address=?", (address,)).fetchone()
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

    def active_candidates(self, limit: int = 250) -> list[sqlite3.Row]:
        terminal = (CandidateState.REJECTED_UNSAFE, CandidateState.EXPIRED)
        return list(self.conn.execute(
            "SELECT c.*,t.token_address,t.symbol,t.name,t.pair_address FROM candidates c "
            "JOIN tokens t ON t.id=c.token_id WHERE c.state NOT IN (?,?) "
            "ORDER BY COALESCE(c.last_monitored_at,c.first_discovered_at),c.id LIMIT ?",
            (*terminal, limit),
        ))

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
            if str(old[0]) != str(state):
                self.conn.execute(
                    "INSERT INTO candidate_transitions(candidate_id,from_state,to_state,reason,score,confidence,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (candidate_id, old[0], str(state), reason,
                     score.normalized_score if score else None, score.confidence if score else None, now),
                )

    def candidates_report(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT c.*,t.symbol,t.name,t.token_address FROM candidates c JOIN tokens t ON t.id=c.token_id "
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
            cur = self.conn.execute(
                "INSERT INTO signals(token_id,signal_timestamp,signal_price_usd,signal_market_cap_usd,"
                "signal_liquidity_usd,signal_holder_count,signal_volume_5m_usd,signal_score,signal_class,"
                "component_scores_json,developer_state_json,narrative_state_json,social_state_json,"
                "onchain_state_json,risk_flags_json,scoring_version,current_market_cap_usd,current_score,"
                "ath_market_cap_usd,atl_market_cap_usd,last_monitored_at,created_at,normalized_score,confidence,"
                "candidate_history_json,current_signal_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (token_id, now, snapshot.price_usd, snapshot.market_cap_usd, snapshot.liquidity_usd,
                 holder_count, snapshot.volume_5m_usd, score.total, score.classification,
                 _json(score.component_scores), _json(states.get("developer", {})),
                 _json(states.get("narrative", {})), _json(states.get("social", {})),
                 _json(states.get("onchain", {})), _json(risk_flags), score.scoring_version,
                 snapshot.market_cap_usd, score.normalized_score, snapshot.market_cap_usd,
                 snapshot.market_cap_usd, now, now, score.normalized_score, score.confidence,
                 _json(states.get("candidate_history", {})), str(score.classification)),
            )
            signal_id = int(cur.lastrowid)
            payload = dict(message_payload, signal_id=signal_id)
            self.conn.execute(
                "INSERT INTO outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (f"signal:{signal_id}", "SIGNAL", _json(payload), now),
            )
            return signal_id

    def active_signals(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT s.*,t.token_address,t.symbol,t.name FROM signals s "
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

    def status_stats(self, started_at: str) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        one = lambda sql, args=(): self.conn.execute(sql, args).fetchone()[0]
        providers = self.conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(healthy),0) FROM provider_health"
        ).fetchone()
        return {
            "started_at": started_at,
            "tokens_discovered": one("SELECT COUNT(*) FROM tokens"),
            "tokens_evaluated": one("SELECT COUNT(DISTINCT token_id) FROM evaluations"),
            "hard_rejected": one("SELECT COUNT(*) FROM candidates WHERE state='REJECTED_UNSAFE'"),
            "pending_evidence": one("SELECT COUNT(*) FROM candidates WHERE state IN ('PENDING_EVIDENCE','FAILED_PROVIDER','DISCOVERED','SCREENING')"),
            "candidates_watching": one("SELECT COUNT(*) FROM candidates WHERE state='CANDIDATE'"),
            "expired": one("SELECT COUNT(*) FROM candidates WHERE state='EXPIRED'"),
            "signals": one("SELECT COUNT(*) FROM signals"),
            "watch": one("SELECT COUNT(*) FROM signals WHERE signal_class='WATCH'"),
            "strong": one("SELECT COUNT(*) FROM signals WHERE signal_class='STRONG'"),
            "high_conviction": one("SELECT COUNT(*) FROM signals WHERE signal_class='HIGH_CONVICTION'"),
            "active_signals": one("SELECT COUNT(*) FROM signals WHERE active=1"),
            "signals_today": one("SELECT COUNT(*) FROM signals WHERE signal_timestamp LIKE ?", (today + "%",)),
            "providers_healthy": int(providers[1]),
            "providers_total": int(providers[0]),
            "database": "OK",
        }

    def performance(self, version: str, since: str | None = None) -> dict[str, Any]:
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
        return result
