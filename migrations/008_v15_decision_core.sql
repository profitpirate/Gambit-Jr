-- Gambit Jr V1.5 decision core. Additive; existing V1.4 rows remain valid.
ALTER TABLE candidates ADD COLUMN runner_score REAL;
ALTER TABLE candidates ADD COLUMN runner_grade TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE candidates ADD COLUMN failure_score REAL;
ALTER TABLE candidates ADD COLUMN failure_grade TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE candidates ADD COLUMN setup_conviction REAL;
ALTER TABLE candidates ADD COLUMN evidence_coverage REAL;
ALTER TABLE candidates ADD COLUMN critical_unknowns_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE candidates ADD COLUMN failure_reasons_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE candidates ADD COLUMN v15_signal_tier TEXT NOT NULL DEFAULT 'SILENT_WATCH';

CREATE TABLE IF NOT EXISTS v15_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    observed_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    runner_score REAL NOT NULL,
    runner_grade TEXT NOT NULL,
    failure_score REAL NOT NULL,
    failure_grade TEXT NOT NULL,
    survival_grade TEXT NOT NULL,
    setup_conviction REAL NOT NULL,
    evidence_coverage REAL NOT NULL,
    entry_status TEXT NOT NULL,
    signal_tier TEXT NOT NULL,
    critical_unknowns_json TEXT NOT NULL,
    failure_reasons_json TEXT NOT NULL,
    provider_conflicts_json TEXT NOT NULL,
    feature_vector_json TEXT NOT NULL,
    software_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    UNIQUE(candidate_id, observed_at, stage)
);

CREATE TABLE IF NOT EXISTS v15_t0_calls (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id),
    call_timestamp TEXT NOT NULL,
    token_address TEXT NOT NULL,
    chain TEXT NOT NULL,
    stage TEXT NOT NULL,
    market_cap_usd REAL,
    price_usd REAL,
    liquidity_usd REAL,
    runner_score REAL NOT NULL,
    failure_score REAL NOT NULL,
    setup_conviction REAL NOT NULL,
    evidence_coverage REAL NOT NULL,
    entry_status TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,
    software_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS immutable_v15_t0_update
BEFORE UPDATE ON v15_t0_calls BEGIN SELECT RAISE(ABORT, 'v15 T0 call is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_v15_t0_delete
BEFORE DELETE ON v15_t0_calls BEGIN SELECT RAISE(ABORT, 'v15 T0 call is immutable'); END;

CREATE TABLE IF NOT EXISTS provider_evidence_v15 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    field_name TEXT NOT NULL,
    value_json TEXT,
    provider TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    age_seconds REAL NOT NULL,
    confidence REAL NOT NULL,
    conflict_state TEXT NOT NULL,
    UNIQUE(candidate_id, field_name, provider, retrieved_at)
);

CREATE TABLE IF NOT EXISTS tradeability_v15 (
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    observed_at TEXT NOT NULL,
    notional_usd REAL NOT NULL,
    buy_impact_percent REAL,
    sell_impact_percent REAL,
    exitability_grade TEXT NOT NULL,
    source_type TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(candidate_id, observed_at, notional_usd)
);

CREATE INDEX IF NOT EXISTS idx_v15_decisions_tier ON v15_decisions(signal_tier, stage, observed_at);
CREATE INDEX IF NOT EXISTS idx_v15_provider_field ON provider_evidence_v15(field_name, retrieved_at);
