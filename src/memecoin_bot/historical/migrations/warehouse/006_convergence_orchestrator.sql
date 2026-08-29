CREATE TABLE IF NOT EXISTS convergence_runs (
    run_id TEXT PRIMARY KEY,
    orchestration_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'PENDING','RUNNING','BLOCKED_EXTERNAL','RETRYABLE_FAILURE','FAILED_RESEARCH',
        'PASSED_ENGINEERING','PASSED_RESEARCH','AWAITING_MATURITY','SHADOW',
        'APPROVED_FOR_HUMAN_REVIEW'
    )),
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route = 0),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    configuration_json TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS convergence_phases (
    run_id TEXT NOT NULL REFERENCES convergence_runs(run_id),
    phase_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'PENDING','RUNNING','BLOCKED_EXTERNAL','RETRYABLE_FAILURE','FAILED_RESEARCH',
        'PASSED_ENGINEERING','PASSED_RESEARCH','AWAITING_MATURITY','SHADOW',
        'APPROVED_FOR_HUMAN_REVIEW'
    )),
    attempt INTEGER NOT NULL DEFAULT 0,
    maximum_attempts INTEGER NOT NULL DEFAULT 3,
    dependency_json TEXT NOT NULL DEFAULT '[]',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    lease_owner TEXT,
    lease_expires_at TEXT,
    next_retry_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    PRIMARY KEY(run_id, phase_name)
);

CREATE TABLE IF NOT EXISTS convergence_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES convergence_runs(run_id),
    phase_name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    artifact_path TEXT,
    content_sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, phase_name, artifact_type, content_sha256)
);

CREATE TABLE IF NOT EXISTS provider_capabilities_v15 (
    provider TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    access_class TEXT NOT NULL,
    credential_required INTEGER NOT NULL,
    credential_env_json TEXT NOT NULL,
    documentation_url TEXT NOT NULL,
    signup_url TEXT,
    current_docs_checked_at TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    rate_limit_json TEXT NOT NULL,
    cost_json TEXT NOT NULL,
    production_role TEXT NOT NULL,
    configured INTEGER NOT NULL DEFAULT 0,
    admission_state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_probes_v15 (
    probe_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL REFERENCES provider_capabilities_v15(provider),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    state TEXT NOT NULL,
    events_seen INTEGER NOT NULL DEFAULT 0,
    matching_events INTEGER NOT NULL DEFAULT 0,
    tokens_seen INTEGER NOT NULL DEFAULT 0,
    latency_p50_ms REAL,
    latency_p95_ms REAL,
    error_count INTEGER NOT NULL DEFAULT 0,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS historical_month_coverage_v15 (
    month TEXT NOT NULL,
    universe_type TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    events INTEGER NOT NULL DEFAULT 0,
    outcome_labels INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    pit_quality TEXT NOT NULL,
    bias_state TEXT NOT NULL,
    state TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(month, universe_type, dataset_id)
);

CREATE TABLE IF NOT EXISTS retired_holdouts_v15 (
    holdout_id TEXT PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    target TEXT NOT NULL,
    inspected_by_run_id TEXT NOT NULL REFERENCES convergence_runs(run_id),
    inspected_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(dataset_version, window_start, window_end, target)
);

CREATE TABLE IF NOT EXISTS audit_findings_v15 (
    finding_id TEXT PRIMARY KEY,
    audit_run_id TEXT NOT NULL,
    category TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol TEXT NOT NULL,
    problem TEXT NOT NULL,
    severity TEXT NOT NULL,
    fix TEXT NOT NULL,
    test_added TEXT,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_convergence_reports_v15 (
    report_date TEXT PRIMARY KEY,
    run_id TEXT REFERENCES convergence_runs(run_id),
    generated_at TEXT NOT NULL,
    report_json TEXT NOT NULL,
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route = 0)
);

CREATE INDEX IF NOT EXISTS idx_convergence_phase_state
    ON convergence_phases(state, next_retry_at, ordinal);
CREATE INDEX IF NOT EXISTS idx_convergence_run_ordinal
    ON convergence_phases(run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_provider_probe_time
    ON provider_probes_v15(provider, completed_at);
CREATE INDEX IF NOT EXISTS idx_historical_month_state
    ON historical_month_coverage_v15(month, state, pit_quality);
CREATE INDEX IF NOT EXISTS idx_audit_findings_status
    ON audit_findings_v15(category, status, severity);

CREATE TRIGGER IF NOT EXISTS convergence_no_public_route
BEFORE UPDATE OF public_route ON convergence_runs
WHEN NEW.public_route != 0 BEGIN SELECT RAISE(ABORT, 'convergence research cannot route publicly'); END;

CREATE TRIGGER IF NOT EXISTS daily_report_no_public_route
BEFORE UPDATE OF public_route ON daily_convergence_reports_v15
WHEN NEW.public_route != 0 BEGIN SELECT RAISE(ABORT, 'convergence reports are internal only'); END;
