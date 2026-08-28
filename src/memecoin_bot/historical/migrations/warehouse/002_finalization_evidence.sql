CREATE TABLE IF NOT EXISTS dataset_coverage_assessments (
    dataset_id TEXT PRIMARY KEY REFERENCES datasets(dataset_id),
    launch_platform TEXT,
    normalized_rows INTEGER NOT NULL DEFAULT 0,
    missing_ranges_json TEXT NOT NULL DEFAULT '[]',
    completeness_estimate REAL,
    point_in_time_safe INTEGER NOT NULL CHECK(point_in_time_safe IN (0,1)),
    timestamp_precision TEXT NOT NULL,
    survivorship_bias TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    licensing_limitations TEXT NOT NULL,
    cost_class TEXT NOT NULL,
    information_gain TEXT NOT NULL,
    assessed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_decisions (
    decision_id TEXT PRIMARY KEY,
    research_run_id TEXT,
    feature_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    train_window_json TEXT NOT NULL,
    validation_window_json TEXT NOT NULL,
    test_window_json TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    ablation_json TEXT NOT NULL,
    leakage_state TEXT NOT NULL,
    drift_state TEXT NOT NULL,
    approval_state TEXT NOT NULL CHECK(approval_state IN ('APPROVED','REJECTED','RESEARCH_ONLY')),
    approved_by TEXT,
    merge_policy TEXT NOT NULL CHECK(merge_policy IN ('EXPLANATION_ONLY','FILL_UNKNOWN','BOUNDED_BLEND')),
    limitations_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS latency_measurements_v15 (
    measurement_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    p50_ms REAL,
    p95_ms REAL,
    max_ms REAL,
    throughput_per_second REAL,
    storage_bytes INTEGER,
    measured_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acquisition_requirements (
    source_name TEXT PRIMARY KEY,
    credential_name TEXT,
    expected_coverage TEXT NOT NULL,
    cost_class TEXT NOT NULL,
    expected_information_gain TEXT NOT NULL,
    state TEXT NOT NULL,
    limitation TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_decision_state
ON research_decisions(approval_state, feature_name);
CREATE INDEX IF NOT EXISTS idx_latency_operation
ON latency_measurements_v15(operation, measured_at);
