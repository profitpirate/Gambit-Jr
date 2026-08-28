from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(UTC)


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


@dataclass(frozen=True, slots=True)
class RawEvidence:
    dataset_id: str
    provider: str
    chain: str
    entity_type: str
    entity_id: str
    source_timestamp: str
    availability_timestamp: str
    endpoint_type: str
    payload: Any
    schema_version: str
    acquisition_version: str
    quality_state: str = "KNOWN"
    provenance: dict[str, Any] | None = None

    def validate(self) -> None:
        source = _parse_timestamp(self.source_timestamp)
        available = _parse_timestamp(self.availability_timestamp)
        if available < source:
            raise ValueError("availability_timestamp cannot precede source_timestamp")
        if not self.dataset_id or not self.provider or not self.entity_id:
            raise ValueError("dataset, provider and entity identifiers are required")


class _SqliteStore:
    def __init__(self, path: str | Path, migration_family: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate(migration_family)

    def _migrate(self, family: str) -> None:
        migration_dir = Path(__file__).with_name("migrations") / family
        with self._lock, self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in self.conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(migration_dir.glob("*.sql")):
                if path.name in applied:
                    continue
                self.conn.executescript(path.read_text(encoding="utf-8"))
                self.conn.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                    (path.name, _now()),
                )

    def close(self) -> None:
        self.conn.close()


class RawArchive:
    """Content-addressed raw evidence archive, separate from query databases."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def preserve(self, evidence: RawEvidence, payload_hash: str) -> Path:
        stamp = _parse_timestamp(evidence.source_timestamp)
        relative = Path(
            f"{stamp.year:04d}{stamp.month:02d}",
            f"{payload_hash}.json",
        )
        target = self.root / relative
        if target.exists():
            return relative
        target.parent.mkdir(parents=True, exist_ok=True)
        envelope = asdict(evidence)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            handle.write(_json(envelope))
            temporary = Path(handle.name)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return relative


class HistoricalWarehouse(_SqliteStore):
    """Offline historical warehouse with immutable point-in-time evidence."""

    def __init__(self, path: str | Path, archive_root: str | Path):
        super().__init__(path, "warehouse")
        self.archive = RawArchive(archive_root)
        if self.path.resolve().parent == self.archive.root.resolve():
            raise ValueError("raw archive and historical warehouse must use separate directories")

    def register_dataset(self, record: dict[str, Any]) -> None:
        required = {
            "dataset_id",
            "dataset_version",
            "provider",
            "chain",
            "acquisition_method",
            "refresh_method",
            "timestamp_precision",
            "reliability",
            "history_kind",
            "point_in_time_safe",
        }
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(f"dataset coverage fields missing: {', '.join(missing)}")
        values = {
            "earliest_timestamp": None,
            "latest_timestamp": None,
            "entity_count": 0,
            "observation_count": 0,
            "missing_ranges_json": "[]",
            "rate_limit_json": "{}",
            "estimated_completeness": None,
            "cost_json": "{}",
            "storage_bytes": 0,
            **record,
            "updated_at": _now(),
        }
        for name in ("missing_ranges_json", "rate_limit_json", "cost_json"):
            if not isinstance(values[name], str):
                values[name] = _json(values[name])
        columns = tuple(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{name}=excluded.{name}"
            for name in columns
            if name
            not in {
                "dataset_id",
                "dataset_version",
                "earliest_timestamp",
                "latest_timestamp",
                "entity_count",
                "observation_count",
                "storage_bytes",
            }
        )
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO datasets({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT(dataset_id) DO UPDATE SET {updates}",
                tuple(values[name] for name in columns),
            )

    def ingest_raw(self, evidence: RawEvidence) -> tuple[str, bool]:
        evidence.validate()
        payload_bytes = _json(evidence.payload).encode()
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        evidence_id = _uuid(evidence.dataset_id, evidence.provider, payload_hash)
        archive_path = self.archive.preserve(evidence, payload_hash)
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO raw_evidence("
                "evidence_id,dataset_id,provider,chain,entity_type,entity_id,source_timestamp,"
                "availability_timestamp,ingestion_timestamp,endpoint_type,payload_hash,"
                "schema_version,acquisition_version,quality_state,provenance_json,archive_path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    evidence.dataset_id,
                    evidence.provider,
                    evidence.chain,
                    evidence.entity_type,
                    evidence.entity_id,
                    evidence.source_timestamp,
                    evidence.availability_timestamp,
                    _now(),
                    evidence.endpoint_type,
                    payload_hash,
                    evidence.schema_version,
                    evidence.acquisition_version,
                    evidence.quality_state,
                    _json(evidence.provenance or {}),
                    str(archive_path),
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                self._refresh_dataset_coverage(evidence.dataset_id)
        return evidence_id, inserted

    def _refresh_dataset_coverage(self, dataset_id: str) -> None:
        archive_paths = self.conn.execute(
            "SELECT archive_path FROM raw_evidence WHERE dataset_id=?", (dataset_id,)
        ).fetchall()
        storage_bytes = sum(
            path.stat().st_size
            for row in archive_paths
            if (path := self.archive.root / row["archive_path"]).exists()
        )
        self.conn.execute(
            "UPDATE datasets SET earliest_timestamp=(SELECT MIN(source_timestamp) FROM raw_evidence "
            "WHERE dataset_id=?), latest_timestamp=(SELECT MAX(source_timestamp) FROM raw_evidence "
            "WHERE dataset_id=?), entity_count=(SELECT COUNT(DISTINCT chain||':'||entity_type||':'||entity_id) "
            "FROM raw_evidence WHERE dataset_id=?), observation_count=(SELECT COUNT(*) FROM raw_evidence "
            "WHERE dataset_id=?), storage_bytes=?, updated_at=? WHERE dataset_id=?",
            (
                dataset_id,
                dataset_id,
                dataset_id,
                dataset_id,
                storage_bytes,
                _now(),
                dataset_id,
            ),
        )

    def upsert_entity(
        self,
        entity_type: str,
        chain: str,
        canonical_id: str,
        first_available_at: str,
        attributes: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        _parse_timestamp(first_available_at)
        entity_key = _uuid(entity_type, chain, canonical_id)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_entities VALUES(?,?,?,?,?,?,?)",
                (
                    entity_key,
                    entity_type,
                    chain,
                    canonical_id,
                    first_available_at,
                    _json(attributes or {}),
                    _json(provenance or {}),
                ),
            )
        return entity_key

    def normalize_event(
        self,
        evidence_id: str,
        dataset_version: str,
        entity_key: str,
        event_type: str,
        observed_at: str,
        available_at: str,
        values: dict[str, Any],
        quality_state: str = "KNOWN",
    ) -> str:
        if _parse_timestamp(available_at) < _parse_timestamp(observed_at):
            raise ValueError("event availability cannot precede observation")
        event_id = _uuid(evidence_id, event_type, observed_at)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO normalized_events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    evidence_id,
                    dataset_version,
                    entity_key,
                    event_type,
                    observed_at,
                    available_at,
                    _json(values),
                    quality_state,
                ),
            )
        return event_id

    def write_feature(
        self,
        *,
        dataset_version: str,
        feature_version: str,
        entity_key: str,
        feature_name: str,
        value: Any,
        observed_at: str,
        available_at: str,
        source_event_ids: list[str],
        missing_state: str = "KNOWN",
        confidence: float | None = None,
    ) -> str:
        if _parse_timestamp(available_at) < _parse_timestamp(observed_at):
            raise ValueError("feature availability cannot precede observation")
        feature_id = _uuid(
            dataset_version,
            feature_version,
            entity_key,
            feature_name,
            observed_at,
            available_at,
        )
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO point_in_time_features VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    feature_id,
                    dataset_version,
                    feature_version,
                    entity_key,
                    feature_name,
                    None if value is None else _json(value),
                    observed_at,
                    available_at,
                    _now(),
                    _json(source_event_ids),
                    missing_state,
                    confidence,
                ),
            )
        return feature_id

    def features_at(
        self, entity_key: str, decision_at: str, feature_version: str
    ) -> dict[str, Any]:
        _parse_timestamp(decision_at)
        rows = self.conn.execute(
            "SELECT feature_name,feature_value_json,observed_at,available_at,confidence,missing_state "
            "FROM point_in_time_features f WHERE entity_key=? AND feature_version=? "
            "AND observed_at<=? AND available_at<=? AND feature_id=(SELECT feature_id FROM "
            "point_in_time_features newer WHERE newer.entity_key=f.entity_key AND "
            "newer.feature_name=f.feature_name AND newer.feature_version=f.feature_version "
            "AND newer.observed_at<=? AND newer.available_at<=? ORDER BY newer.observed_at DESC,"
            "newer.available_at DESC LIMIT 1)",
            (entity_key, feature_version, decision_at, decision_at, decision_at, decision_at),
        ).fetchall()
        return {
            row["feature_name"]: {
                "value": None
                if row["feature_value_json"] is None
                else json.loads(row["feature_value_json"]),
                "observed_at": row["observed_at"],
                "available_at": row["available_at"],
                "confidence": row["confidence"],
                "state": row["missing_state"],
            }
            for row in rows
        }

    def record_outcome(self, record: dict[str, Any]) -> str:
        decision = _parse_timestamp(record["decision_at"])
        measured = _parse_timestamp(record["measurement_end_at"])
        available = _parse_timestamp(record["available_at"])
        if measured < decision or available < measured:
            raise ValueError("outcomes must be measured after decision and available after measurement")
        peak = record.get("peak_multiple")
        rugged = bool(record.get("rugged"))
        class_name = record.get("class_name") or self.classify_outcome(peak, rugged)
        outcome_id = _uuid(
            record["dataset_version"],
            record["outcome_version"],
            record["entity_key"],
            record["decision_at"],
        )
        columns = (
            "outcome_id",
            "dataset_version",
            "outcome_version",
            "entity_key",
            "decision_at",
            "measurement_end_at",
            "available_at",
            "peak_multiple",
            "time_to_1_5x_seconds",
            "time_to_2x_seconds",
            "time_to_3x_seconds",
            "time_to_5x_seconds",
            "time_to_10x_seconds",
            "time_to_20x_seconds",
            "time_to_peak_seconds",
            "max_adverse_excursion",
            "max_favourable_excursion",
            "drawdown_before_peak",
            "drawdown_after_signal",
            "liquidity_survival_seconds",
            "token_survival_seconds",
            "final_market_cap_usd",
            "final_liquidity_usd",
            "rugged",
            "class_name",
        )
        values = {
            **{name: None for name in columns},
            **record,
            "outcome_id": outcome_id,
            "rugged": int(rugged),
            "class_name": class_name,
        }
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT OR IGNORE INTO outcomes({','.join(columns)}) VALUES("
                f"{','.join('?' for _ in columns)})",
                tuple(values[name] for name in columns),
            )
        return outcome_id

    @staticmethod
    def classify_outcome(peak_multiple: float | None, rugged: bool) -> str:
        peak = float(peak_multiple or 0)
        if rugged:
            return "PROBABLE_RUG" if peak < 2 else "RUG_AFTER_RUNNER"
        for threshold, name in (
            (50, "50X_PLUS"),
            (20, "20X"),
            (10, "10X"),
            (5, "5X"),
            (3, "3X"),
            (2, "2X"),
            (1.5, "1_5X"),
        ):
            if peak >= threshold:
                return name
        return "SURVIVED" if peak > 0 else "DEAD"

    def coverage_map(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM datasets ORDER BY dataset_id")]

    def begin_backfill(self, dataset_id: str, provider: str, job_id: str | None = None) -> str:
        job_id = job_id or str(uuid.uuid4())
        now = _now()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO backfill_jobs(job_id,dataset_id,provider,state,started_at,"
                "updated_at) VALUES(?,?,?,'RUNNING',?,?)",
                (job_id, dataset_id, provider, now, now),
            )
        return job_id

    def checkpoint_backfill(
        self,
        job_id: str,
        *,
        cursor: Any,
        ingested: int,
        queue_remaining: int | None,
        earliest: str | None,
        latest: str | None,
        state: str = "RUNNING",
        error: str | None = None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE backfill_jobs SET state=?,cursor_json=?,queue_remaining=?,pages_completed="
                "pages_completed+1,records_ingested=records_ingested+?,earliest_timestamp="
                "COALESCE(earliest_timestamp,?),latest_timestamp=COALESCE(?,latest_timestamp),"
                "last_checkpoint_at=?,last_error=?,updated_at=? WHERE job_id=?",
                (
                    state,
                    _json(cursor) if cursor is not None else None,
                    queue_remaining,
                    ingested,
                    earliest,
                    latest,
                    _now(),
                    error,
                    _now(),
                    job_id,
                ),
            )

    def backfill_status(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM backfill_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def record_shadow_decision(
        self,
        *,
        entity_key: str,
        observed_at: str,
        live_version: str,
        challenger_version: str,
        live_decision: dict[str, Any],
        challenger_decision: dict[str, Any],
        outcome_id: str | None = None,
    ) -> str:
        _parse_timestamp(observed_at)
        shadow_id = _uuid(entity_key, observed_at, challenger_version)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO shadow_decisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    shadow_id,
                    entity_key,
                    observed_at,
                    live_version,
                    challenger_version,
                    _json(live_decision),
                    _json(challenger_decision),
                    outcome_id,
                ),
            )
        return shadow_id

    def record_drift(
        self,
        *,
        feature_name: str,
        segment_type: str,
        segment_value: str,
        baseline_window: str,
        current_window: str,
        sample_size: int,
        metric_name: str,
        metric_value: float,
        warning_threshold: float,
    ) -> str:
        drift_id = _uuid(
            feature_name, segment_type, segment_value, current_window, metric_name
        )
        state = "WARNING" if abs(metric_value) >= warning_threshold else "STABLE"
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO drift_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    drift_id,
                    feature_name,
                    segment_type,
                    segment_value,
                    baseline_window,
                    current_window,
                    sample_size,
                    metric_name,
                    metric_value,
                    state,
                    _now(),
                ),
            )
        return drift_id


class ApprovedFeatureStore(_SqliteStore):
    """Small live store containing only explicitly approved historical features."""

    def __init__(self, path: str | Path):
        super().__init__(path, "production")

    def approve(self, record: dict[str, Any]) -> None:
        if not record.get("approved_by") or not record.get("research_run_id"):
            raise ValueError("manual approver and research run are required")
        if int(record.get("sample_size") or 0) <= 0:
            raise ValueError("an approved feature requires a positive research sample")
        max_contribution = float(record.get("max_contribution", 0))
        if not 0 <= max_contribution <= 0.25:
            raise ValueError("historical contribution must be bounded to 25%")
        values = {
            "feature_name": record["feature_name"],
            "feature_version": record["feature_version"],
            "target_stage": record["target_stage"],
            "target_feature": record["target_feature"],
            "research_run_id": record["research_run_id"],
            "research_evidence_json": _json(record.get("research_evidence") or {}),
            "sample_size": record["sample_size"],
            "walk_forward_json": _json(record.get("walk_forward") or {}),
            "approved_at": record.get("approved_at") or _now(),
            "approved_by": record["approved_by"],
            "production_use": int(bool(record.get("production_use", True))),
            "merge_policy": record.get("merge_policy", "EXPLANATION_ONLY"),
            "max_contribution": max_contribution,
            "limitations_json": _json(record.get("limitations") or []),
        }
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO approved_feature_registry({','.join(values)}) VALUES("
                f"{','.join('?' for _ in values)})",
                tuple(values.values()),
            )

    def publish_snapshot(
        self,
        *,
        chain: str,
        entity_id: str,
        feature_name: str,
        feature_version: str,
        value: Any,
        observed_at: str,
        available_at: str,
        source_research_run_id: str,
        provenance: dict[str, Any],
        expires_at: str | None = None,
    ) -> str:
        if _parse_timestamp(available_at) < _parse_timestamp(observed_at):
            raise ValueError("production feature availability cannot precede observation")
        snapshot_id = _uuid(
            chain, entity_id, feature_name, feature_version, observed_at, available_at
        )
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO production_feature_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    chain,
                    entity_id,
                    feature_name,
                    feature_version,
                    _json(value) if value is not None else None,
                    observed_at,
                    available_at,
                    expires_at,
                    source_research_run_id,
                    _json(provenance),
                ),
            )
        return snapshot_id

    def context_at(self, chain: str, entity_id: str, decision_at: str, stage: str) -> list[dict[str, Any]]:
        _parse_timestamp(decision_at)
        rows = self.conn.execute(
            "SELECT r.*,s.feature_value_json,s.observed_at,s.available_at,s.expires_at,"
            "s.provenance_json FROM approved_feature_registry r JOIN production_feature_snapshots s "
            "ON s.feature_name=r.feature_name AND s.feature_version=r.feature_version "
            "WHERE r.production_use=1 AND r.target_stage IN (?, 'ALL') AND s.chain=? AND "
            "s.entity_id=? AND s.observed_at<=? AND s.available_at<=? AND "
            "(s.expires_at IS NULL OR s.expires_at>?) AND s.snapshot_id=(SELECT snapshot_id FROM "
            "production_feature_snapshots newer WHERE newer.chain=s.chain AND newer.entity_id=s.entity_id "
            "AND newer.feature_name=s.feature_name AND newer.feature_version=s.feature_version "
            "AND newer.observed_at<=? AND newer.available_at<=? AND "
            "(newer.expires_at IS NULL OR newer.expires_at>?) ORDER BY newer.observed_at DESC,"
            "newer.available_at DESC LIMIT 1)",
            (
                stage,
                chain,
                entity_id,
                decision_at,
                decision_at,
                decision_at,
                decision_at,
                decision_at,
                decision_at,
            ),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["value"] = (
                None
                if value.pop("feature_value_json") is None
                else json.loads(row["feature_value_json"])
            )
            value["provenance"] = json.loads(value.pop("provenance_json"))
            result.append(value)
        return result

    def audit_lookup(
        self,
        chain: str,
        entity_id: str,
        decision_at: str,
        started_at: str,
        latency_ms: float,
        state: str,
        feature_names: list[str],
        fallback_reason: str | None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO production_context_audit VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    chain,
                    entity_id,
                    decision_at,
                    started_at,
                    latency_ms,
                    state,
                    _json(feature_names),
                    fallback_reason,
                ),
            )


class HistoricalContextReader:
    """Bounded live reader. It never opens the raw archive or research warehouse."""

    def __init__(self, store: ApprovedFeatureStore, latency_budget_ms: float = 25):
        self.store = store
        self.latency_budget_ms = latency_budget_ms

    def apply(
        self,
        chain: str,
        entity_id: str,
        decision_at: str,
        stage: str,
        live_features: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = _now()
        started = time.monotonic()
        state = "EMPTY"
        fallback_reason = None
        applied: list[dict[str, Any]] = []
        try:
            context = self.store.context_at(chain, entity_id, decision_at, stage)
            latency_ms = (time.monotonic() - started) * 1000
            if latency_ms > self.latency_budget_ms:
                context = []
                state = "FALLBACK"
                fallback_reason = "LATENCY_BUDGET_EXCEEDED"
            for row in context:
                target = row["target_feature"]
                value = row["value"]
                policy = row["merge_policy"]
                contribution = float(row["max_contribution"])
                if policy == "FILL_UNKNOWN" and live_features.get(target) is None:
                    live_features[target] = value
                elif (
                    policy == "BOUNDED_BLEND"
                    and isinstance(value, (int, float))
                    and isinstance(live_features.get(target), (int, float))
                ):
                    live_features[target] = round(
                        float(live_features[target]) * (1 - contribution)
                        + float(value) * contribution,
                        4,
                    )
                applied.append(
                    {
                        "feature": row["feature_name"],
                        "target": target,
                        "policy": policy,
                        "contribution": contribution,
                        "observed_at": row["observed_at"],
                        "available_at": row["available_at"],
                    }
                )
            if applied:
                state = "APPLIED"
        except (sqlite3.Error, ValueError) as error:
            latency_ms = (time.monotonic() - started) * 1000
            state = "FALLBACK"
            fallback_reason = type(error).__name__
        live_features["historical_context"] = {
            "state": state,
            "features": applied,
            "fallback_reason": fallback_reason,
        }
        try:
            self.store.audit_lookup(
                chain,
                entity_id,
                decision_at,
                started_at,
                latency_ms,
                state,
                [row["feature"] for row in applied],
                fallback_reason,
            )
        except sqlite3.Error:
            # Historical context is additive; audit persistence cannot stop live scoring.
            live_features["historical_context"]["audit_state"] = "WRITE_FAILED"
        return live_features

    def status(self) -> dict[str, Any]:
        try:
            approved = self.store.conn.execute(
                "SELECT COUNT(*) FROM approved_feature_registry WHERE production_use=1"
            ).fetchone()[0]
            snapshots = self.store.conn.execute(
                "SELECT COUNT(*) FROM production_feature_snapshots"
            ).fetchone()[0]
            latest = self.store.conn.execute(
                "SELECT state,latency_ms,fallback_reason,decision_at FROM production_context_audit "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as error:
            return {
                "enabled": True,
                "approved_features": None,
                "published_snapshots": None,
                "latest_lookup": {"state": "DEGRADED", "latency_ms": None},
                "latency_budget_ms": self.latency_budget_ms,
                "error": type(error).__name__,
            }
        return {
            "enabled": True,
            "approved_features": int(approved),
            "published_snapshots": int(snapshots),
            "latest_lookup": dict(latest) if latest else None,
            "latency_budget_ms": self.latency_budget_ms,
        }
