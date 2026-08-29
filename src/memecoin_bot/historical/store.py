from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
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

    def ingest_raw(
        self, evidence: RawEvidence, *, refresh_coverage: bool = True
    ) -> tuple[str, bool]:
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
            if inserted and refresh_coverage:
                self._refresh_dataset_coverage(evidence.dataset_id)
        return evidence_id, inserted

    def refresh_dataset_coverage(self, dataset_id: str) -> None:
        with self._lock, self.conn:
            self._refresh_dataset_coverage(dataset_id)

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
        event_id = _uuid(evidence_id, event_type, entity_key, observed_at, _json(values))
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
            "FROM (SELECT feature_name,feature_value_json,observed_at,available_at,confidence,"
            "missing_state,ROW_NUMBER() OVER(PARTITION BY feature_name ORDER BY observed_at DESC,"
            "available_at DESC) AS recency FROM point_in_time_features WHERE entity_key=? AND "
            "feature_version=? AND observed_at<=? AND available_at<=?) WHERE recency=1",
            (entity_key, feature_version, decision_at, decision_at),
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
            raise ValueError(
                "outcomes must be measured after decision and available after measurement"
            )
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
        return [
            dict(row) for row in self.conn.execute("SELECT * FROM datasets ORDER BY dataset_id")
        ]

    def assess_coverage(self, dataset_id: str, assessment: dict[str, Any]) -> None:
        required = {
            "timestamp_precision",
            "survivorship_bias",
            "quality_state",
            "licensing_limitations",
            "cost_class",
            "information_gain",
        }
        missing = sorted(required - assessment.keys())
        if missing:
            raise ValueError(f"coverage assessment fields missing: {', '.join(missing)}")
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO dataset_coverage_assessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(dataset_id) DO UPDATE SET launch_platform=excluded.launch_platform,"
                "normalized_rows=excluded.normalized_rows,missing_ranges_json=excluded.missing_ranges_json,"
                "completeness_estimate=excluded.completeness_estimate,"
                "point_in_time_safe=excluded.point_in_time_safe,"
                "timestamp_precision=excluded.timestamp_precision,"
                "survivorship_bias=excluded.survivorship_bias,quality_state=excluded.quality_state,"
                "licensing_limitations=excluded.licensing_limitations,cost_class=excluded.cost_class,"
                "information_gain=excluded.information_gain,assessed_at=excluded.assessed_at",
                (
                    dataset_id,
                    assessment.get("launch_platform"),
                    int(assessment.get("normalized_rows") or 0),
                    _json(assessment.get("missing_ranges") or []),
                    assessment.get("completeness_estimate"),
                    int(bool(assessment.get("point_in_time_safe"))),
                    assessment["timestamp_precision"],
                    assessment["survivorship_bias"],
                    assessment["quality_state"],
                    assessment["licensing_limitations"],
                    assessment["cost_class"],
                    assessment["information_gain"],
                    _now(),
                ),
            )

    def coverage_manifest(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT d.*,a.launch_platform,a.normalized_rows,a.completeness_estimate AS "
            "assessed_completeness,a.point_in_time_safe AS assessed_point_in_time_safe,"
            "a.survivorship_bias,a.quality_state,a.licensing_limitations,a.cost_class,"
            "a.information_gain,a.assessed_at FROM datasets d LEFT JOIN "
            "dataset_coverage_assessments a ON a.dataset_id=d.dataset_id ORDER BY d.dataset_id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw_bytes"] = item.pop("storage_bytes")
            item["missing_ranges"] = json.loads(item.pop("missing_ranges_json"))
            item["cost"] = json.loads(item.pop("cost_json"))
            item["point_in_time_safe"] = bool(item["point_in_time_safe"])
            result.append(item)
        return result

    def record_research_decision(self, record: dict[str, Any]) -> str:
        decision_id = _uuid(
            record["feature_name"],
            record["feature_version"],
            record["dataset_version"],
            record["approval_state"],
        )
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO research_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    record.get("research_run_id"),
                    record["feature_name"],
                    record["feature_version"],
                    record["dataset_version"],
                    int(record.get("sample_size") or 0),
                    _json(record.get("train_window") or {}),
                    _json(record.get("validation_window") or {}),
                    _json(record.get("test_window") or {}),
                    _json(record.get("baseline") or {}),
                    _json(record.get("ablation") or {}),
                    record.get("leakage_state", "UNKNOWN"),
                    record.get("drift_state", "UNKNOWN"),
                    record["approval_state"],
                    record.get("approved_by"),
                    record.get("merge_policy", "EXPLANATION_ONLY"),
                    _json(record.get("limitations") or []),
                    _now(),
                ),
            )
        return decision_id

    def record_latency(
        self,
        operation: str,
        samples_ms: list[float],
        *,
        throughput_per_second: float | None = None,
        storage_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if not samples_ms:
            raise ValueError("latency measurement requires samples")
        ordered = sorted(samples_ms)
        p95_index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
        measurement_id = str(uuid.uuid4())
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO latency_measurements_v15 VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    measurement_id,
                    operation,
                    len(ordered),
                    statistics.median(ordered),
                    ordered[p95_index],
                    max(ordered),
                    throughput_per_second,
                    storage_bytes,
                    _now(),
                    _json(metadata or {}),
                ),
            )
        return measurement_id

    def record_acquisition_requirement(self, record: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO acquisition_requirements VALUES(?,?,?,?,?,?,?,?)",
                (
                    record["source_name"],
                    record.get("credential_name"),
                    record["expected_coverage"],
                    record["cost_class"],
                    record["expected_information_gain"],
                    record["state"],
                    record["limitation"],
                    _now(),
                ),
            )

    def record_research_source(self, record: dict[str, Any]) -> str:
        """Record one actually examined source and its falsifiable evidence trail."""

        required = {
            "title",
            "url",
            "source_type",
            "category",
            "publisher",
            "access_state",
            "reliability_state",
            "relevance_state",
            "research_method",
            "data_window",
            "population",
            "claim",
            "test_method",
            "result",
            "limitations",
            "license_state",
            "acquisition_state",
            "provenance",
        }
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(f"research source fields missing: {', '.join(missing)}")
        for name in required - {"provenance"}:
            if not str(record[name]).strip():
                raise ValueError(f"research source field cannot be empty: {name}")
        if record["access_state"] == "METADATA_ONLY" and record["relevance_state"] == "HIGH":
            raise ValueError("metadata-only sources cannot be counted as high relevance")
        provenance_json = _json(record["provenance"])
        provenance_hash = hashlib.sha256(provenance_json.encode()).hexdigest()
        source_id = _uuid(record["url"], provenance_hash)
        examined_at = record.get("examined_at") or _now()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO research_sources_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET title=excluded.title,source_type=excluded.source_type,"
                "category=excluded.category,publisher=excluded.publisher,published_at=excluded.published_at,"
                "examined_at=excluded.examined_at,access_state=excluded.access_state,"
                "reliability_state=excluded.reliability_state,relevance_state=excluded.relevance_state,"
                "research_method=excluded.research_method,data_window=excluded.data_window,"
                "population=excluded.population,claim=excluded.claim,test_method=excluded.test_method,"
                "result=excluded.result,limitations=excluded.limitations,license_state=excluded.license_state,"
                "acquisition_state=excluded.acquisition_state,provenance_hash=excluded.provenance_hash,"
                "provenance_json=excluded.provenance_json,updated_at=excluded.updated_at",
                (
                    source_id,
                    record["title"],
                    record["url"],
                    record["source_type"],
                    record["category"],
                    record["publisher"],
                    record.get("published_at"),
                    examined_at,
                    record["access_state"],
                    record["reliability_state"],
                    record["relevance_state"],
                    record["research_method"],
                    record["data_window"],
                    record["population"],
                    record["claim"],
                    record["test_method"],
                    record["result"],
                    record["limitations"],
                    record["license_state"],
                    record["acquisition_state"],
                    provenance_hash,
                    provenance_json,
                    examined_at,
                    _now(),
                ),
            )
            actual_id = self.conn.execute(
                "SELECT source_id FROM research_sources_v15 WHERE url=?", (record["url"],)
            ).fetchone()[0]
            for hypothesis in record.get("hypotheses") or []:
                self.conn.execute(
                    "INSERT INTO research_source_hypotheses_v15 VALUES(?,?,?) "
                    "ON CONFLICT(source_id,hypothesis) DO UPDATE SET "
                    "evidence_direction=excluded.evidence_direction",
                    (actual_id, hypothesis["name"], hypothesis["direction"]),
                )
        return str(actual_id)

    def research_source_report(self, *, required_high_relevance: int = 200) -> dict[str, Any]:
        counts = {
            row[0]: int(row[1])
            for row in self.conn.execute(
                "SELECT relevance_state,COUNT(*) FROM research_sources_v15 GROUP BY relevance_state"
            )
        }
        access = {
            row[0]: int(row[1])
            for row in self.conn.execute(
                "SELECT access_state,COUNT(*) FROM research_sources_v15 GROUP BY access_state"
            )
        }
        categories = {
            row[0]: int(row[1])
            for row in self.conn.execute(
                "SELECT category,COUNT(*) FROM research_sources_v15 "
                "WHERE relevance_state='HIGH' GROUP BY category ORDER BY category"
            )
        }
        high = counts.get("HIGH", 0)
        return {
            "total_examined": sum(counts.values()),
            "high_relevance": high,
            "required_high_relevance": required_high_relevance,
            "gate_passed": high >= required_high_relevance,
            "remaining": max(0, required_high_relevance - high),
            "by_relevance": counts,
            "by_access": access,
            "high_relevance_by_category": categories,
        }

    def operator_status(self) -> dict[str, Any]:
        def scalar(query: str) -> int:
            return int(self.conn.execute(query).fetchone()[0])

        latest_research = self.conn.execute(
            "SELECT research_run_id,metrics_json,result_json,leakage_state,created_at "
            "FROM research_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        research_summary = None
        if latest_research:
            research_summary = dict(latest_research)
            research_summary["metrics"] = json.loads(research_summary.pop("metrics_json"))
            research_summary["result"] = json.loads(research_summary.pop("result_json"))
        sizes = {
            "warehouse_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "raw_archive_bytes": sum(
                path.stat().st_size for path in self.archive.root.rglob("*.json")
            )
            if self.archive.root.exists()
            else 0,
        }
        return {
            "datasets": self.coverage_manifest(),
            "backfills": [
                dict(row)
                for row in self.conn.execute("SELECT * FROM backfill_jobs ORDER BY updated_at DESC")
            ],
            "entities": scalar("SELECT COUNT(*) FROM canonical_entities"),
            "normalized_events": scalar("SELECT COUNT(*) FROM normalized_events"),
            "point_in_time_features": scalar("SELECT COUNT(*) FROM point_in_time_features"),
            "outcomes": scalar("SELECT COUNT(*) FROM outcomes"),
            "runner_corpus": scalar("SELECT COUNT(*) FROM outcomes WHERE peak_multiple>=5"),
            "failure_corpus": scalar(
                "SELECT COUNT(*) FROM outcomes WHERE rugged=1 OR peak_multiple<1"
            ),
            "research_runs": scalar("SELECT COUNT(*) FROM research_runs"),
            "research_sources": self.research_source_report(),
            "latest_research": research_summary,
            "wallet_memory_entities": scalar(
                "SELECT COUNT(DISTINCT entity_key) FROM point_in_time_features "
                "WHERE feature_name LIKE 'wallet_%' OR feature_name LIKE '%alpha_wallet%'"
            ),
            "creator_memory_entities": scalar(
                "SELECT COUNT(DISTINCT entity_key) FROM point_in_time_features "
                "WHERE feature_name LIKE 'creator_%' OR feature_name LIKE 'deployer_%'"
            ),
            "buyer_memory_entities": scalar(
                "SELECT COUNT(DISTINCT entity_key) FROM point_in_time_features "
                "WHERE feature_name LIKE 'buyer_%'"
            ),
            "approved_research_features": scalar(
                "SELECT COUNT(*) FROM research_decisions WHERE approval_state='APPROVED'"
            ),
            "research_decisions": [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM research_decisions ORDER BY decided_at DESC"
                )
            ],
            "shadow_decisions": scalar("SELECT COUNT(*) FROM shadow_decisions"),
            "drift_observations": scalar("SELECT COUNT(*) FROM drift_observations"),
            "latency": [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM latency_measurements_v15 ORDER BY measured_at DESC"
                )
            ],
            "acquisition_requirements": [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM acquisition_requirements ORDER BY source_name"
                )
            ],
            **sizes,
        }

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

    def save_backfill_cursor(self, job_id: str, cursor: Any) -> None:
        """Persist an upstream execution handle before its first result page completes."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE backfill_jobs SET cursor_json=?,last_checkpoint_at=?,updated_at=? "
                "WHERE job_id=?",
                (_json(cursor), _now(), _now(), job_id),
            )

    def record_dune_partition(self, record: dict[str, Any]) -> None:
        month = datetime.strptime(str(record["month"]), "%Y-%m").replace(tzinfo=UTC)
        parquet = record.get("parquet") or {}
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO dune_partition_state_v15(query_name,schema_version,partition_year,"
                "partition_month,execution_id,result_offset,row_count,content_sha256,schema_sha256,"
                "source_coverage_json,quality_state,parquet_path,state,last_error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?) ON CONFLICT(query_name,schema_version,"
                "partition_year,partition_month) DO UPDATE SET execution_id=excluded.execution_id,"
                "result_offset=excluded.result_offset,row_count=MAX(dune_partition_state_v15.row_count,"
                "excluded.row_count),content_sha256=COALESCE(excluded.content_sha256,content_sha256),"
                "schema_sha256=COALESCE(excluded.schema_sha256,schema_sha256),source_coverage_json="
                "excluded.source_coverage_json,parquet_path="
                "COALESCE(excluded.parquet_path,parquet_path),state=excluded.state,"
                "quality_state=excluded.quality_state,last_error=excluded.last_error,"
                "updated_at=excluded.updated_at",
                (
                    record["query_name"],
                    record["schema_version"],
                    month.year,
                    month.month,
                    record.get("execution_id"),
                    int(record.get("offset") or 0),
                    int(record.get("total_rows") or 0),
                    parquet.get("content_sha256"),
                    parquet.get("schema_sha256"),
                    _json(
                        {
                            "repository_sql": True,
                            "partial_results": bool(record.get("partial_results")),
                            "source_total_rows": record.get("source_total_rows"),
                            "source_result_bytes": record.get("source_result_bytes"),
                            "materialization_mode": record.get(
                                "materialization_mode", "FULL_RESULT"
                            ),
                        }
                    ),
                    (
                        "PILOT_SAMPLE_SCHEMA_VALIDATED"
                        if record.get("partial_results")
                        else "SCHEMA_VALIDATED"
                        if int(record.get("total_rows") or 0) > 0
                        else "VALID_EMPTY_RESULT"
                    ),
                    parquet.get("parquet_path"),
                    record.get("state", "RUNNING"),
                    record.get("error"),
                    _now(),
                ),
            )
            partition_key = (
                f"{record['query_name']}:{record['schema_version']}:"
                f"{month.year:04d}-{month.month:02d}"
            )
            self.conn.execute(
                "INSERT INTO data_quality_v15 VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(partition_key) "
                "DO UPDATE SET assessed_at=excluded.assessed_at,row_count=excluded.row_count,"
                "coverage_json=excluded.coverage_json,quality_state=excluded.quality_state,"
                "evidence_json=excluded.evidence_json",
                (
                    partition_key,
                    _now(),
                    int(record.get("total_rows") or 0),
                    0,
                    _json({"state": "NOT_PROFILED_UNTIL_COMPLETE_CORPUS"}),
                    _json(
                        {
                            "repository_sql": True,
                            "partial_results": bool(record.get("partial_results")),
                            "source_total_rows": record.get("source_total_rows"),
                        }
                    ),
                    (
                        "PILOT_SAMPLE_SCHEMA_VALIDATED"
                        if record.get("partial_results")
                        else "SCHEMA_VALIDATED"
                        if int(record.get("total_rows") or 0) > 0
                        else "VALID_EMPTY_RESULT"
                    ),
                    _json(
                        {
                            "execution_id": record.get("execution_id"),
                            "content_sha256": parquet.get("content_sha256"),
                            "schema_sha256": parquet.get("schema_sha256"),
                        }
                    ),
                ),
            )

    def normalize_dune_evidence(self, evidence: RawEvidence, evidence_id: str) -> None:
        """Populate compact owned tables from a schema-validated repository query row."""
        payload = evidence.payload
        query_name = str(evidence.provenance.get("query_name") or "")
        token = str(payload.get("token_address") or evidence.entity_id)
        observed = evidence.source_timestamp
        available = evidence.availability_timestamp
        token_queries = {
            "monthly_universe",
            "pumpfun_launches",
            "pumpfun_trades",
            "pumpswap_trades",
            "migrations",
            "wallet_activity",
            "outcome_reconstruction",
        }
        with self._lock, self.conn:
            if query_name in token_queries and token:
                self.conn.execute(
                    "INSERT INTO historical_tokens_v15 VALUES(?,?,?,?,?,?) ON CONFLICT(token_id) "
                    "DO UPDATE SET first_seen_at=MIN(first_seen_at,excluded.first_seen_at),"
                    "available_at=MAX(available_at,excluded.available_at)",
                    (
                        token,
                        evidence.chain,
                        payload.get("creator"),
                        observed,
                        available,
                        _json({"evidence_id": evidence_id, "query_name": query_name}),
                    ),
                )
            if query_name in {"monthly_universe", "pumpfun_launches"}:
                launch_id = _uuid("dune-launch", token, str(payload.get("tx_id") or observed))
                self.conn.execute(
                    "INSERT OR IGNORE INTO historical_launches_v15 VALUES(?,?,?,?,?,?,?)",
                    (
                        launch_id,
                        token,
                        observed,
                        payload.get("creator"),
                        payload.get("tx_id"),
                        available,
                        _json({"evidence_id": evidence_id}),
                    ),
                )
            elif query_name in {"pumpfun_trades", "pumpswap_trades"}:
                trade_id = _uuid(
                    "dune-trade",
                    str(payload.get("tx_id") or observed),
                    token,
                    str(payload.get("side") or ""),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO historical_trades_v15 VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        trade_id,
                        token,
                        observed,
                        payload.get("trader"),
                        payload.get("side"),
                        None,
                        payload.get("token_amount"),
                        (
                            float(payload["amount_usd"]) / float(payload["token_amount"])
                            if payload.get("amount_usd") is not None and payload.get("token_amount")
                            else None
                        ),
                        available,
                        _json(
                            {"evidence_id": evidence_id, "amount_usd": payload.get("amount_usd")}
                        ),
                    ),
                )
                wallet = payload.get("trader")
                side = str(payload.get("side") or "").lower()
                landmark_table = (
                    "buyer_landmarks_v15"
                    if side == "buy"
                    else "seller_landmarks_v15"
                    if side == "sell"
                    else None
                )
                if wallet and landmark_table:
                    self.conn.execute(
                        f"INSERT OR IGNORE INTO {landmark_table} VALUES(?,?,?,?,?,?)",
                        (
                            token,
                            wallet,
                            "TRADE",
                            observed,
                            _json(
                                {
                                    "token_amount": payload.get("token_amount"),
                                    "amount_usd": payload.get("amount_usd"),
                                    "tx_id": payload.get("tx_id"),
                                }
                            ),
                            available,
                        ),
                    )
            elif query_name == "migrations":
                self.conn.execute(
                    "INSERT OR IGNORE INTO migration_events_v15 VALUES(?,?,?,?,?,?)",
                    (
                        token,
                        observed,
                        payload.get("venue"),
                        payload.get("tx_id"),
                        available,
                        _json({"evidence_id": evidence_id}),
                    ),
                )
            elif query_name == "wallet_activity":
                for wallet, direction in (
                    (payload.get("from_owner"), "OUT"),
                    (payload.get("to_owner"), "IN"),
                ):
                    if wallet:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO wallet_token_entries_v15 VALUES(?,?,?,?,?)",
                            (
                                token,
                                wallet,
                                observed,
                                _json(
                                    {
                                        "direction": direction,
                                        "amount": payload.get("amount"),
                                        "amount_usd": payload.get("amount_usd"),
                                        "evidence_id": evidence_id,
                                    }
                                ),
                                available,
                            ),
                        )
                        self.conn.execute(
                            "INSERT OR IGNORE INTO wallet_history_v15 VALUES(?,?,?,?)",
                            (
                                wallet,
                                observed,
                                _json(
                                    {
                                        "token_id": token,
                                        "direction": direction,
                                        "amount": payload.get("amount"),
                                        "amount_usd": payload.get("amount_usd"),
                                        "tx_id": payload.get("tx_id"),
                                    }
                                ),
                                available,
                            ),
                        )
            elif query_name == "creator_activity" and payload.get("creator"):
                self.conn.execute(
                    "INSERT OR IGNORE INTO creator_history_v15 VALUES(?,?,?,?)",
                    (
                        payload["creator"],
                        observed,
                        _json(
                            {
                                "tx_id": payload.get("tx_id"),
                                "success": payload.get("success"),
                                "evidence_id": evidence_id,
                            }
                        ),
                        available,
                    ),
                )
            elif query_name == "outcome_reconstruction":
                self.conn.execute(
                    "INSERT OR IGNORE INTO token_landmarks_v15 VALUES(?,?,?,?,?)",
                    (
                        token,
                        "MARKET_PRICE_OBSERVATION",
                        observed,
                        _json(
                            {
                                "price_usd": payload.get("price_usd"),
                                "amount_usd": payload.get("amount_usd"),
                                "tx_id": payload.get("tx_id"),
                                "evidence_id": evidence_id,
                            }
                        ),
                        available,
                    ),
                )

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
        drift_id = _uuid(feature_name, segment_type, segment_value, current_window, metric_name)
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
        governance_fields = {
            "dataset_version",
            "train_window",
            "validation_window",
            "test_window",
            "baseline",
            "ablation",
            "leakage_state",
            "drift_state",
            "approval_state",
        }
        missing = sorted(governance_fields - record.keys())
        if missing:
            raise ValueError(f"feature approval evidence missing: {', '.join(missing)}")
        if record["approval_state"] != "APPROVED" or record["leakage_state"] != "PASS":
            raise ValueError("production features require APPROVED state and PASS leakage result")
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
            self.conn.execute(
                "INSERT INTO feature_approval_evidence_v15 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["feature_name"],
                    record["feature_version"],
                    record["dataset_version"],
                    _json(record["train_window"]),
                    _json(record["validation_window"]),
                    _json(record["test_window"]),
                    _json(record["baseline"]),
                    _json(record["ablation"]),
                    record["leakage_state"],
                    record["drift_state"],
                    record["approval_state"],
                    record["approved_by"],
                    record.get("approved_at") or _now(),
                ),
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

    def context_at(
        self, chain: str, entity_id: str, decision_at: str, stage: str
    ) -> list[dict[str, Any]]:
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
