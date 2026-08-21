-- Gambit Jr V1.4: additive ultra-early alpha, graph, product, and attribution schema.
ALTER TABLE candidates ADD COLUMN source_event_timestamp TEXT;
ALTER TABLE candidates ADD COLUMN source_received_at TEXT;
ALTER TABLE candidates ADD COLUMN candidate_created_at TEXT;
ALTER TABLE candidates ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN consecutive_provider_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN consecutive_pair_missing INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN authoritative_state TEXT NOT NULL DEFAULT 'DISCOVERED';
ALTER TABLE candidates ADD COLUMN evaluation_stage TEXT NOT NULL DEFAULT 'T0';
ALTER TABLE candidates ADD COLUMN genesis_triggered_at TEXT;
ALTER TABLE candidates ADD COLUMN hot_triggered_at TEXT;
ALTER TABLE candidates ADD COLUMN priority_triggered_at TEXT;
ALTER TABLE candidates ADD COLUMN entry_state TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE candidates ADD COLUMN setup_grade TEXT;
ALTER TABLE candidates ADD COLUMN survival_grade TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE candidates ADD COLUMN payoff_grade TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE guild_settings ADD COLUMN alert_tier_v14 TEXT;
ALTER TABLE guild_settings ADD COLUMN daily_report_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_settings ADD COLUMN enabled_chains_json TEXT NOT NULL DEFAULT '["solana","bsc"]';
ALTER TABLE guild_settings ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS launch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    chain TEXT NOT NULL,
    launchpad TEXT,
    token_address TEXT NOT NULL,
    creator_address TEXT,
    phase TEXT NOT NULL DEFAULT 'CREATED',
    source_event_timestamp TEXT NOT NULL,
    source_received_at TEXT NOT NULL,
    candidate_created_at TEXT,
    source_to_candidate_ms REAL,
    slot_or_block TEXT,
    transaction_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_stages_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    launch_event_id INTEGER REFERENCES launch_events(id),
    stage TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    authoritative_state TEXT NOT NULL,
    entry_state TEXT NOT NULL,
    confidence REAL,
    feature_vector_json TEXT NOT NULL,
    provider_evidence_json TEXT NOT NULL,
    unknowns_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    software_version TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    UNIQUE(candidate_id, stage, observed_at)
);

CREATE TABLE IF NOT EXISTS immutable_call_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    signal_id INTEGER REFERENCES signals(id),
    tier TEXT NOT NULL,
    call_timestamp TEXT NOT NULL,
    call_market_cap_usd REAL,
    call_price_usd REAL,
    call_liquidity_usd REAL,
    call_score REAL,
    confidence REAL,
    entry_state TEXT NOT NULL,
    feature_vector_json TEXT NOT NULL,
    provider_evidence_json TEXT NOT NULL,
    software_version TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    radar_version TEXT NOT NULL,
    UNIQUE(candidate_id, tier)
);

CREATE TABLE IF NOT EXISTS wallet_nodes (
    chain TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(chain, wallet_address)
);

CREATE TABLE IF NOT EXISTS wallet_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    from_wallet TEXT NOT NULL,
    to_wallet TEXT NOT NULL,
    relationship TEXT NOT NULL,
    token_address TEXT,
    observed_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(chain, from_wallet, to_wallet, relationship, token_address)
);

CREATE TABLE IF NOT EXISTS wallet_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    cluster_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    risk_state TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(chain, cluster_key)
);

CREATE TABLE IF NOT EXISTS wallet_cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES wallet_clusters(id),
    wallet_address TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'BUYER',
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(cluster_id, wallet_address)
);

CREATE TABLE IF NOT EXISTS buyer_cohorts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    cohort_size INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    retained_count INTEGER,
    sold_count INTEGER,
    connected_count INTEGER,
    concentration_percent REAL,
    evidence_json TEXT NOT NULL,
    UNIQUE(token_id, cohort_size, observed_at)
);

CREATE TABLE IF NOT EXISTS creator_profiles_v14 (
    chain TEXT NOT NULL,
    creator_address TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT 'UNKNOWN',
    launches INTEGER NOT NULL DEFAULT 0,
    survived INTEGER NOT NULL DEFAULT 0,
    rugs INTEGER NOT NULL DEFAULT 0,
    runners INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(chain, creator_address)
);

CREATE TABLE IF NOT EXISTS creator_launches_v14 (
    creator_address TEXT NOT NULL,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    launched_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'UNKNOWN',
    peak_multiple REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(creator_address, token_id)
);

CREATE TABLE IF NOT EXISTS narratives_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    narrative_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    freshness TEXT NOT NULL,
    saturation TEXT NOT NULL,
    leader_token_id INTEGER REFERENCES tokens(id),
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_members_v14 (
    narrative_id INTEGER NOT NULL REFERENCES narratives_v14(id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    role TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    clone_penalty REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(narrative_id, token_id)
);

CREATE TABLE IF NOT EXISTS capital_rotation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    chain TEXT NOT NULL,
    from_narrative TEXT,
    to_narrative TEXT,
    strength REAL,
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlists (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notes TEXT,
    PRIMARY KEY(guild_id, user_id, chain, token_address)
);

CREATE TABLE IF NOT EXISTS manual_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT,
    user_id TEXT,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    result_state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    latency_ms REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS missed_runner_audits_v14 (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    audited_at TEXT NOT NULL,
    peak_multiple REAL NOT NULL,
    highest_tier TEXT,
    miss_category TEXT NOT NULL,
    decision_evidence_json TEXT NOT NULL,
    false_negative_features_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_benchmarks_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_date TEXT NOT NULL,
    chain TEXT NOT NULL,
    launchpad TEXT NOT NULL DEFAULT 'UNKNOWN',
    cohort TEXT NOT NULL,
    selected_count INTEGER NOT NULL,
    universe_count INTEGER NOT NULL,
    runners_2x INTEGER NOT NULL,
    runners_5x INTEGER NOT NULL,
    runners_10x INTEGER NOT NULL,
    runners_20x INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    UNIQUE(benchmark_date, chain, launchpad, cohort)
);

CREATE TABLE IF NOT EXISTS latency_observations_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    milliseconds REAL NOT NULL,
    source TEXT,
    event_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS adverse_excursions_v14 (
    call_snapshot_id INTEGER NOT NULL REFERENCES immutable_call_snapshots(id),
    before_multiple REAL NOT NULL,
    maximum_adverse_excursion REAL NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(call_snapshot_id, before_multiple)
);

CREATE TABLE IF NOT EXISTS discord_card_updates_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_snapshot_id INTEGER NOT NULL REFERENCES immutable_call_snapshots(id),
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    remote_message_id TEXT NOT NULL,
    update_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(call_snapshot_id, guild_id, channel_id, update_type)
);

CREATE TABLE IF NOT EXISTS daily_reports_v14 (
    report_date TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    remote_message_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(report_date, guild_id)
);

CREATE TABLE IF NOT EXISTS lifecycle_transitions_v14 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(candidate_id, to_state, reason)
);

CREATE INDEX IF NOT EXISTS idx_launch_event_time ON launch_events(source_event_timestamp, chain);
CREATE INDEX IF NOT EXISTS idx_launch_event_token ON launch_events(chain, token_address, source_event_timestamp);
CREATE INDEX IF NOT EXISTS idx_candidates_v14_retry ON candidates(authoritative_state, next_retry_at, scheduling_lane);
CREATE INDEX IF NOT EXISTS idx_eval_stage_candidate ON evaluation_stages_v14(candidate_id, stage, observed_at);
CREATE INDEX IF NOT EXISTS idx_wallet_edge_token ON wallet_edges(chain, token_address, relationship);
CREATE INDEX IF NOT EXISTS idx_wallet_edge_funding ON wallet_edges(chain, from_wallet, relationship);
CREATE INDEX IF NOT EXISTS idx_wallet_cluster_time ON wallet_clusters(chain, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_creator_history ON creator_launches_v14(creator_address, launched_at);
CREATE INDEX IF NOT EXISTS idx_narrative_time ON narratives_v14(last_seen_at, freshness);
CREATE INDEX IF NOT EXISTS idx_calls_tier_time ON immutable_call_snapshots(tier, call_timestamp);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlists(guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_benchmark_date ON performance_benchmarks_v14(benchmark_date, chain);
CREATE INDEX IF NOT EXISTS idx_latency_metric_time ON latency_observations_v14(metric, observed_at);
CREATE INDEX IF NOT EXISTS idx_adverse_call ON adverse_excursions_v14(call_snapshot_id, before_multiple);

-- Reconcile naming aliases and preserve prior lifecycle history without deleting rows.
UPDATE candidates SET
    retry_count=attempt_count,
    consecutive_provider_failures=consecutive_provider_failure_count,
    consecutive_pair_missing=consecutive_missing_pair_count,
    candidate_created_at=COALESCE(candidate_created_at, first_discovered_at),
    source_received_at=COALESCE(source_received_at, first_discovered_at),
    source_event_timestamp=COALESCE(source_event_timestamp, first_discovered_at),
    authoritative_state=CASE
        WHEN state='EARLY_RADAR' THEN 'STANDARD_RADAR'
        WHEN state='SIGNALLED' THEN 'QUALIFIED_SIGNAL'
        ELSE state
    END;
