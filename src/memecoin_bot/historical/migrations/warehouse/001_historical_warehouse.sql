CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    chain TEXT NOT NULL,
    acquisition_method TEXT NOT NULL,
    refresh_method TEXT NOT NULL,
    earliest_timestamp TEXT,
    latest_timestamp TEXT,
    entity_count INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    missing_ranges_json TEXT NOT NULL DEFAULT '[]',
    timestamp_precision TEXT NOT NULL,
    reliability TEXT NOT NULL,
    rate_limit_json TEXT NOT NULL DEFAULT '{}',
    estimated_completeness REAL,
    history_kind TEXT NOT NULL CHECK(history_kind IN ('TRUE_HISTORICAL','RECONSTRUCTED','UNKNOWN')),
    point_in_time_safe INTEGER NOT NULL CHECK(point_in_time_safe IN (0,1)),
    cost_json TEXT NOT NULL DEFAULT '{}',
    storage_bytes INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset_id, dataset_version)
);

CREATE TABLE IF NOT EXISTS raw_evidence (
    evidence_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    provider TEXT NOT NULL,
    chain TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    availability_timestamp TEXT NOT NULL,
    ingestion_timestamp TEXT NOT NULL,
    endpoint_type TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    acquisition_version TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    UNIQUE(dataset_id, provider, payload_hash)
);

CREATE TABLE IF NOT EXISTS canonical_entities (
    entity_key TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    chain TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    first_available_at TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    UNIQUE(entity_type, chain, canonical_id)
);

CREATE TABLE IF NOT EXISTS normalized_events (
    event_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES raw_evidence(evidence_id),
    dataset_version TEXT NOT NULL,
    entity_key TEXT NOT NULL REFERENCES canonical_entities(entity_key),
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    values_json TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    UNIQUE(evidence_id, event_type, observed_at)
);

CREATE TABLE IF NOT EXISTS point_in_time_features (
    feature_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    entity_key TEXT NOT NULL REFERENCES canonical_entities(entity_key),
    feature_name TEXT NOT NULL,
    feature_value_json TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    missing_state TEXT NOT NULL,
    confidence REAL,
    UNIQUE(dataset_version, feature_version, entity_key, feature_name, observed_at, available_at)
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    outcome_version TEXT NOT NULL,
    entity_key TEXT NOT NULL REFERENCES canonical_entities(entity_key),
    decision_at TEXT NOT NULL,
    measurement_end_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    peak_multiple REAL,
    time_to_1_5x_seconds REAL,
    time_to_2x_seconds REAL,
    time_to_3x_seconds REAL,
    time_to_5x_seconds REAL,
    time_to_10x_seconds REAL,
    time_to_20x_seconds REAL,
    time_to_peak_seconds REAL,
    max_adverse_excursion REAL,
    max_favourable_excursion REAL,
    drawdown_before_peak REAL,
    drawdown_after_signal REAL,
    liquidity_survival_seconds REAL,
    token_survival_seconds REAL,
    final_market_cap_usd REAL,
    final_liquidity_usd REAL,
    rugged INTEGER,
    class_name TEXT NOT NULL,
    UNIQUE(dataset_version, outcome_version, entity_key, decision_at)
);

CREATE TABLE IF NOT EXISTS backfill_jobs (
    job_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    cursor_json TEXT,
    queue_remaining INTEGER,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    records_ingested INTEGER NOT NULL DEFAULT 0,
    earliest_timestamp TEXT,
    latest_timestamp TEXT,
    last_checkpoint_at TEXT,
    last_error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_runs (
    research_run_id TEXT PRIMARY KEY,
    research_type TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    provider_set_json TEXT NOT NULL,
    chain TEXT,
    train_start TEXT NOT NULL,
    train_end TEXT NOT NULL,
    validation_start TEXT NOT NULL,
    validation_end TEXT NOT NULL,
    test_start TEXT NOT NULL,
    test_end TEXT NOT NULL,
    methodology_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    artifact_path TEXT,
    leakage_state TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_findings (
    finding_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL REFERENCES research_runs(research_run_id),
    family TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    cohort TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    effect_json TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    shadow_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    live_version TEXT NOT NULL,
    challenger_version TEXT NOT NULL,
    live_decision_json TEXT NOT NULL,
    challenger_decision_json TEXT NOT NULL,
    outcome_id TEXT,
    UNIQUE(entity_key, observed_at, challenger_version)
);

CREATE TABLE IF NOT EXISTS drift_observations (
    drift_id TEXT PRIMARY KEY,
    feature_name TEXT NOT NULL,
    segment_type TEXT NOT NULL,
    segment_value TEXT NOT NULL,
    baseline_window TEXT NOT NULL,
    current_window TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    state TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_entity_time ON raw_evidence(chain, entity_id, availability_timestamp);
CREATE INDEX IF NOT EXISTS idx_events_entity_time ON normalized_events(entity_key, available_at, observed_at);
CREATE INDEX IF NOT EXISTS idx_features_pit ON point_in_time_features(entity_key, feature_name, available_at, observed_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON outcomes(entity_key, decision_at, available_at);

CREATE TRIGGER IF NOT EXISTS immutable_raw_update BEFORE UPDATE ON raw_evidence
BEGIN SELECT RAISE(ABORT, 'raw historical evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_raw_delete BEFORE DELETE ON raw_evidence
BEGIN SELECT RAISE(ABORT, 'raw historical evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON normalized_events
BEGIN SELECT RAISE(ABORT, 'normalized historical events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_feature_update BEFORE UPDATE ON point_in_time_features
BEGIN SELECT RAISE(ABORT, 'point-in-time features are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_outcome_update BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'historical outcomes are immutable'); END;
