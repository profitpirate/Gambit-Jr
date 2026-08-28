CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approved_feature_registry (
    feature_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    target_stage TEXT NOT NULL,
    target_feature TEXT NOT NULL,
    research_run_id TEXT NOT NULL,
    research_evidence_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    walk_forward_json TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    production_use INTEGER NOT NULL CHECK(production_use IN (0,1)),
    merge_policy TEXT NOT NULL CHECK(merge_policy IN ('FILL_UNKNOWN','BOUNDED_BLEND','EXPLANATION_ONLY')),
    max_contribution REAL NOT NULL CHECK(max_contribution >= 0 AND max_contribution <= 0.25),
    limitations_json TEXT NOT NULL,
    PRIMARY KEY(feature_name, feature_version)
);

CREATE TABLE IF NOT EXISTS production_feature_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_value_json TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    expires_at TEXT,
    source_research_run_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    UNIQUE(chain, entity_id, feature_name, feature_version, observed_at, available_at),
    FOREIGN KEY(feature_name, feature_version)
      REFERENCES approved_feature_registry(feature_name, feature_version)
);

CREATE TABLE IF NOT EXISTS production_context_audit (
    audit_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    lookup_started_at TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    state TEXT NOT NULL,
    feature_names_json TEXT NOT NULL,
    fallback_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_production_context_pit
ON production_feature_snapshots(chain, entity_id, available_at, observed_at);

CREATE TRIGGER IF NOT EXISTS immutable_approval_update BEFORE UPDATE ON approved_feature_registry
BEGIN SELECT RAISE(ABORT, 'feature approvals are versioned and immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_approval_delete BEFORE DELETE ON approved_feature_registry
BEGIN SELECT RAISE(ABORT, 'feature approvals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_production_snapshot_update BEFORE UPDATE ON production_feature_snapshots
BEGIN SELECT RAISE(ABORT, 'production feature snapshots are immutable'); END;
