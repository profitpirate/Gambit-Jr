CREATE TABLE IF NOT EXISTS social_links_v15 (
    link_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    source_url TEXT,
    classification TEXT NOT NULL CHECK (
        classification IN ('COMMUNITY','PROFILE','OFFICIAL_PROJECT','UNKNOWN','NONE')
    ),
    classification_confidence REAL NOT NULL CHECK (
        classification_confidence >= 0 AND classification_confidence <= 1
    ),
    observed_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS social_evidence_v15 (
    evidence_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    mentions INTEGER NOT NULL DEFAULT 0 CHECK (mentions >= 0),
    engagement REAL,
    velocity REAL,
    acceleration REAL,
    first_seen TEXT,
    community_profile_class TEXT NOT NULL CHECK (
        community_profile_class IN ('COMMUNITY','PROFILE','OFFICIAL_PROJECT','UNKNOWN','NONE')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dune_pilot_runs_v15 (
    pilot_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    start_month TEXT NOT NULL,
    end_month TEXT NOT NULL,
    query_names_json TEXT NOT NULL,
    maximum_executions INTEGER NOT NULL,
    executions_started INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dune_pilot_partitions_v15 (
    pilot_id TEXT NOT NULL REFERENCES dune_pilot_runs_v15(pilot_id),
    query_name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    month TEXT NOT NULL,
    execution_id TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    source_total_rows INTEGER NOT NULL DEFAULT 0,
    source_result_bytes INTEGER NOT NULL DEFAULT 0,
    materialization_mode TEXT NOT NULL DEFAULT 'FULL_RESULT',
    output_bytes INTEGER NOT NULL DEFAULT 0,
    credits_used REAL,
    schema_state TEXT NOT NULL DEFAULT 'PENDING',
    semantic_state TEXT NOT NULL DEFAULT 'PENDING',
    semantic_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(pilot_id, query_name, schema_version, month)
);

ALTER TABLE dune_partition_state_v15 ADD COLUMN output_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dune_partition_state_v15 ADD COLUMN credits_used REAL;
ALTER TABLE dune_partition_state_v15 ADD COLUMN semantic_state TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE dune_partition_state_v15 ADD COLUMN semantic_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_social_evidence_token_time
    ON social_evidence_v15(token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_social_links_token_time
    ON social_links_v15(token_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_dune_pilot_partition_state
    ON dune_pilot_partitions_v15(state, month, query_name);
