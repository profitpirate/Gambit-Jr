-- V1.5 canonical realtime intelligence fabric. Additive; CONTROL/public routing is unchanged.
ALTER TABLE provider_health ADD COLUMN last_message_at TEXT;
ALTER TABLE provider_health ADD COLUMN last_valid_event_at TEXT;
ALTER TABLE provider_health ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_health ADD COLUMN rate_limit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_health ADD COLUMN latency_ms REAL;
ALTER TABLE provider_health ADD COLUMN reconnect_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_health ADD COLUMN gap_detected_at TEXT;
ALTER TABLE provider_health ADD COLUMN gap_recovered_at TEXT;
ALTER TABLE provider_health ADD COLUMN events_received INTEGER NOT NULL DEFAULT 0;
ALTER TABLE provider_health ADD COLUMN credits_used REAL NOT NULL DEFAULT 0;
ALTER TABLE provider_health ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE candidates ADD COLUMN monitoring_temperature TEXT NOT NULL DEFAULT 'COLD';
ALTER TABLE candidates ADD COLUMN realtime_priority REAL NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN last_realtime_event_at TEXT;
ALTER TABLE candidates ADD COLUMN next_monitor_at TEXT;

CREATE TABLE IF NOT EXISTS canonical_events (
    event_id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    canonical_token TEXT NOT NULL,
    token_id INTEGER REFERENCES tokens(id),
    chain TEXT NOT NULL,
    platform TEXT NOT NULL,
    first_seen_source TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    available_timestamp TEXT NOT NULL,
    normalized_timestamp TEXT NOT NULL,
    slot_or_block TEXT,
    transaction_signature TEXT,
    pool_identity TEXT,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    semantic_fingerprint TEXT NOT NULL,
    raw_provenance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    confirmation_sources_json TEXT NOT NULL,
    provider_latency_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    processing_status TEXT NOT NULL DEFAULT 'PENDING',
    processing_attempts INTEGER NOT NULL DEFAULT 0,
    processing_error TEXT,
    claimed_at TEXT,
    feature_ready_timestamp TEXT,
    model_start_timestamp TEXT,
    model_finish_timestamp TEXT,
    decision_timestamp TEXT,
    discord_enqueue_timestamp TEXT,
    discord_sent_timestamp TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_event_sources (
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    source TEXT NOT NULL,
    source_event_id TEXT NOT NULL DEFAULT '',
    source_timestamp TEXT NOT NULL,
    received_timestamp TEXT NOT NULL,
    available_timestamp TEXT NOT NULL,
    provider_latency_ms REAL NOT NULL,
    availability_latency_ms REAL NOT NULL,
    confidence REAL NOT NULL,
    semantic_fingerprint TEXT NOT NULL,
    raw_provenance_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(event_id, source, source_event_id)
);

CREATE TABLE IF NOT EXISTS canonical_event_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    source TEXT NOT NULL,
    existing_fingerprint TEXT NOT NULL,
    incoming_fingerprint TEXT NOT NULL,
    existing_payload_json TEXT NOT NULL,
    incoming_payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    UNIQUE(event_id, source, incoming_fingerprint)
);

CREATE TABLE IF NOT EXISTS token_realtime_state (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    platform TEXT NOT NULL,
    launched_at TEXT NOT NULL,
    creator_address TEXT,
    bonding_curve_address TEXT,
    quote_mint TEXT,
    initial_real_token_reserves INTEGER,
    latest_real_token_reserves INTEGER,
    latest_real_quote_reserves INTEGER,
    latest_virtual_token_reserves INTEGER,
    latest_virtual_quote_reserves INTEGER,
    token_total_supply INTEGER,
    curve_complete INTEGER,
    migration_state TEXT NOT NULL DEFAULT 'PRE_MIGRATION',
    migration_started_at TEXT,
    migration_completed_at TEXT,
    pool_identity TEXT,
    monitoring_temperature TEXT NOT NULL DEFAULT 'GENESIS',
    last_event_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS curve_observations_v15 (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    slot_or_block TEXT,
    virtual_token_reserves INTEGER,
    virtual_quote_reserves INTEGER,
    real_token_reserves INTEGER,
    real_quote_reserves INTEGER,
    token_total_supply INTEGER,
    curve_complete INTEGER,
    creator_address TEXT,
    quote_mint TEXT,
    real_sol_reserves INTEGER,
    virtual_sol_reserves INTEGER,
    curve_progress REAL,
    source TEXT NOT NULL,
    evidence_mode TEXT NOT NULL CHECK(evidence_mode IN ('LIVE_NATIVE','LIVE_REDUNDANT','HISTORICAL_PROXY')),
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_event_timeline_v15 (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    event_type TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    available_timestamp TEXT NOT NULL,
    slot_or_block TEXT,
    transaction_signature TEXT,
    actor TEXT,
    counterparty TEXT,
    side TEXT,
    quote_amount REAL,
    token_amount REAL,
    quote_symbol TEXT,
    creator_linked INTEGER,
    funder TEXT,
    wallet_cluster TEXT,
    jito_tip_lamports INTEGER,
    likely_bundled INTEGER,
    wash_probability REAL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_feature_snapshots_v15 (
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    decision_timestamp TEXT NOT NULL,
    available_timestamp TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    evidence_mode TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    PRIMARY KEY(token_id, decision_timestamp, feature_version)
);

CREATE TABLE IF NOT EXISTS wallet_strategy_profiles_v15 (
    chain TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    stage TEXT NOT NULL,
    objective TEXT NOT NULL,
    regime TEXT NOT NULL,
    copy_delay_seconds INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    sample INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    precision REAL,
    wilson_low REAL,
    wilson_high REAL,
    median_remaining_upside REAL,
    median_drawdown REAL,
    available_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(chain,wallet_address,stage,objective,regime,copy_delay_seconds,available_at)
);

CREATE TABLE IF NOT EXISTS wallet_funding_edges_v15 (
    chain TEXT NOT NULL,
    funded_wallet TEXT NOT NULL,
    funder_wallet TEXT NOT NULL,
    first_funded_at TEXT NOT NULL,
    last_funded_at TEXT NOT NULL,
    amount_native REAL,
    transaction_signature TEXT,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(chain,funded_wallet,funder_wallet,first_funded_at)
);

CREATE TABLE IF NOT EXISTS activity_evidence_v15 (
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    observed_at TEXT NOT NULL,
    raw_buyers INTEGER,
    adjusted_buyers INTEGER,
    raw_volume REAL,
    adjusted_volume REAL,
    raw_net_flow REAL,
    adjusted_net_flow REAL,
    linked_wallet_share REAL,
    bundle_linked_share REAL,
    wash_probability REAL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(token_id, observed_at)
);

CREATE TABLE IF NOT EXISTS social_observations_v15 (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source TEXT NOT NULL,
    unique_mentioners INTEGER,
    mention_count INTEGER,
    source_diversity INTEGER,
    bot_spam_share REAL,
    account_quality REAL,
    engagement REAL,
    official_posts INTEGER,
    investor_posts INTEGER,
    causal_ordering TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS narrative_observations_v15 (
    event_id TEXT PRIMARY KEY REFERENCES canonical_events(event_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    narrative_identity TEXT NOT NULL,
    leader_token_id INTEGER REFERENCES tokens(id),
    copycat_distance REAL,
    launch_density REAL,
    capital_concentration REAL,
    capital_fragmentation REAL,
    velocity REAL,
    acceleration REAL,
    saturation REAL,
    decay REAL,
    revival INTEGER,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_continuity_v15 (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    migration_timestamp TEXT,
    pool_creation_timestamp TEXT,
    pre_migration_json TEXT NOT NULL DEFAULT '{}',
    post_migration_json TEXT NOT NULL DEFAULT '{}',
    liquidity_continuity REAL,
    flow_survival REAL,
    buyer_retention REAL,
    sell_shock REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypothesis_registry_v15 (
    hypothesis_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source_cohort TEXT NOT NULL,
    target TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    effect_size REAL,
    matched_control_effect REAL,
    gameability TEXT NOT NULL,
    feature_requirements_json TEXT NOT NULL,
    data_coverage_json TEXT NOT NULL,
    development_performance_json TEXT NOT NULL,
    validation_performance_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('DISCOVERED','RESEARCH_ONLY','VALIDATION_PENDING','REJECTED','CHALLENGER','APPROVED')),
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_autopsies_v15 (
    autopsy_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    cohort_type TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL,
    root_causes_json TEXT NOT NULL,
    cohort_metrics_json TEXT NOT NULL,
    matched_differences_json TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS challenger_runs_v15 (
    run_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    champion_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    development_window_json TEXT NOT NULL,
    validation_window_json TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    fixed_frequency_json TEXT NOT NULL,
    advancement_state TEXT NOT NULL,
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route=0),
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_observations_v15 (
    observed_at TEXT NOT NULL,
    drift_type TEXT NOT NULL CHECK(drift_type IN ('DATA_DRIFT','CONCEPT_DRIFT','CALIBRATION_DRIFT','PROVIDER_DRIFT')),
    metric TEXT NOT NULL,
    baseline_value REAL,
    current_value REAL,
    distance REAL,
    sample INTEGER NOT NULL,
    sustained_periods INTEGER NOT NULL,
    action TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(observed_at,drift_type,metric)
);

CREATE INDEX IF NOT EXISTS idx_canonical_pending ON canonical_events(processing_status, available_timestamp);
CREATE INDEX IF NOT EXISTS idx_canonical_token_time ON canonical_events(chain, canonical_token, source_timestamp);
CREATE INDEX IF NOT EXISTS idx_canonical_signature ON canonical_events(transaction_signature, event_type);
CREATE INDEX IF NOT EXISTS idx_timeline_token_time ON token_event_timeline_v15(token_id,event_timestamp);
CREATE INDEX IF NOT EXISTS idx_curve_token_time ON curve_observations_v15(token_id,observed_at);
CREATE INDEX IF NOT EXISTS idx_realtime_temperature ON token_realtime_state(monitoring_temperature,last_event_at);
CREATE INDEX IF NOT EXISTS idx_funding_funder_time ON wallet_funding_edges_v15(chain,funder_wallet,last_funded_at);
CREATE INDEX IF NOT EXISTS idx_social_token_time ON social_observations_v15(token_id,observed_at);
CREATE INDEX IF NOT EXISTS idx_narrative_identity_time ON narrative_observations_v15(narrative_identity,observed_at);
CREATE INDEX IF NOT EXISTS idx_hypothesis_status ON hypothesis_registry_v15(status,target,created_at);
CREATE INDEX IF NOT EXISTS idx_drift_metric_time ON drift_observations_v15(metric,observed_at);
