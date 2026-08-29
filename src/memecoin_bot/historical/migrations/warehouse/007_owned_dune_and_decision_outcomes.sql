CREATE TABLE IF NOT EXISTS dune_query_registry_v15 (
    query_name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    template_path TEXT NOT NULL,
    expected_columns_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    source_tables_json TEXT NOT NULL,
    minimum_date TEXT NOT NULL,
    compatibility_status TEXT NOT NULL,
    docs_checked_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY(query_name, schema_version)
);

CREATE TABLE IF NOT EXISTS dune_partition_state_v15 (
    query_name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    partition_year INTEGER NOT NULL,
    partition_month INTEGER NOT NULL,
    execution_id TEXT,
    result_offset INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    schema_sha256 TEXT,
    source_coverage_json TEXT NOT NULL DEFAULT '{}',
    quality_state TEXT NOT NULL DEFAULT 'PENDING',
    parquet_path TEXT,
    state TEXT NOT NULL DEFAULT 'PENDING',
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(query_name, schema_version, partition_year, partition_month)
);

CREATE TABLE IF NOT EXISTS historical_tokens_v15 (
    token_id TEXT PRIMARY KEY, chain TEXT NOT NULL, creator TEXT, first_seen_at TEXT NOT NULL,
    available_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_launches_v15 (
    launch_id TEXT PRIMARY KEY, token_id TEXT NOT NULL, launched_at TEXT NOT NULL,
    creator TEXT, transaction_id TEXT, available_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_trades_v15 (
    trade_id TEXT PRIMARY KEY, token_id TEXT NOT NULL, traded_at TEXT NOT NULL, wallet TEXT,
    side TEXT, native_amount REAL, token_amount REAL, price_usd REAL, available_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_landmarks_v15 (
    token_id TEXT NOT NULL, landmark TEXT NOT NULL, observed_at TEXT NOT NULL, value_json TEXT NOT NULL,
    available_at TEXT NOT NULL, PRIMARY KEY(token_id, landmark, observed_at)
);
CREATE TABLE IF NOT EXISTS buyer_landmarks_v15 (
    token_id TEXT NOT NULL, wallet TEXT NOT NULL, landmark TEXT NOT NULL, observed_at TEXT NOT NULL,
    value_json TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY(token_id, wallet, landmark, observed_at)
);
CREATE TABLE IF NOT EXISTS seller_landmarks_v15 (
    token_id TEXT NOT NULL, wallet TEXT NOT NULL, landmark TEXT NOT NULL, observed_at TEXT NOT NULL,
    value_json TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY(token_id, wallet, landmark, observed_at)
);
CREATE TABLE IF NOT EXISTS wallet_token_entries_v15 (
    token_id TEXT NOT NULL, wallet TEXT NOT NULL, first_entry_at TEXT NOT NULL, entry_json TEXT NOT NULL,
    available_at TEXT NOT NULL, PRIMARY KEY(token_id, wallet, first_entry_at)
);
CREATE TABLE IF NOT EXISTS wallet_history_v15 (
    wallet TEXT NOT NULL, as_of TEXT NOT NULL, history_json TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY(wallet, as_of)
);
CREATE TABLE IF NOT EXISTS creator_history_v15 (
    creator TEXT NOT NULL, as_of TEXT NOT NULL, history_json TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY(creator, as_of)
);
CREATE TABLE IF NOT EXISTS migration_events_v15 (
    token_id TEXT NOT NULL, migration_at TEXT NOT NULL, venue TEXT, transaction_id TEXT,
    available_at TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY(token_id, migration_at)
);
CREATE TABLE IF NOT EXISTS market_outcomes_v15 (
    token_id TEXT NOT NULL, horizon_seconds INTEGER NOT NULL, decision_at TEXT NOT NULL,
    outcome_json TEXT NOT NULL, available_at TEXT NOT NULL,
    PRIMARY KEY(token_id, horizon_seconds, decision_at)
);
CREATE TABLE IF NOT EXISTS decision_outcomes_history_v15 (
    decision_id TEXT PRIMARY KEY, token_id TEXT NOT NULL, decision_at TEXT NOT NULL,
    decision_price REAL, decision_market_cap REAL, future_peak_price REAL,
    future_peak_market_cap REAL, peak_multiple_from_decision REAL,
    time_to_2x_from_decision REAL, time_to_5x_from_decision REAL,
    time_to_10x_from_decision REAL, time_to_20x_from_decision REAL,
    time_to_50x_from_decision REAL, maximum_adverse_excursion REAL,
    maximum_favorable_excursion REAL, terminal_failure INTEGER,
    copyability_proxy REAL, available_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS regime_daily_v15 (
    regime_date TEXT PRIMARY KEY, launch_intensity REAL, runner_prevalence REAL,
    median_liquidity REAL, median_dex_volume REAL, sol_trend REAL, market_volatility REAL,
    narrative_concentration REAL, available_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_quality_v15 (
    partition_key TEXT PRIMARY KEY, assessed_at TEXT NOT NULL, row_count INTEGER NOT NULL,
    duplicate_rows INTEGER NOT NULL, missingness_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
    quality_state TEXT NOT NULL, evidence_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dune_partitions_state
    ON dune_partition_state_v15(state, partition_year, partition_month);
