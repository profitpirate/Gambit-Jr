-- One authoritative V1.5 runner decision and incremental hot-path state.
CREATE TABLE IF NOT EXISTS runner_decisions_v15 (
    decision_id TEXT PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    candidate_id INTEGER REFERENCES candidates(id),
    trigger_event_id TEXT REFERENCES canonical_events(event_id),
    decision_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    thesis_type TEXT NOT NULL,
    champion TEXT NOT NULL,
    runner_probabilities_json TEXT NOT NULL,
    failure_probability REAL,
    actionability_probability REAL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    uncertainty REAL NOT NULL CHECK(uncertainty BETWEEN 0 AND 1),
    entry_state_json TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    contradicting_evidence_json TEXT NOT NULL,
    critical_unknowns_json TEXT NOT NULL,
    provider_health_json TEXT NOT NULL,
    evidence_freshness_json TEXT NOT NULL,
    latency_json TEXT NOT NULL,
    model_versions_json TEXT NOT NULL,
    controls_json TEXT NOT NULL,
    heuristic_scores_json TEXT NOT NULL,
    tier TEXT NOT NULL,
    route_state TEXT NOT NULL CHECK(route_state IN (
        'HOLD','REJECTED','RESEARCH_SHADOW_CALL','OPERATOR_SHADOW_ALERT','PUBLIC_ALERT'
    )),
    decision_reason TEXT NOT NULL,
    evaluation_universe_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(token_id, decision_at, champion)
);

CREATE TABLE IF NOT EXISTS decision_outcomes_v15 (
    decision_id TEXT PRIMARY KEY REFERENCES runner_decisions_v15(decision_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    decision_at TEXT NOT NULL,
    decision_price REAL,
    decision_market_cap REAL,
    future_peak_price REAL,
    future_peak_market_cap REAL,
    peak_multiple_from_decision REAL,
    time_to_2x_from_decision REAL,
    time_to_5x_from_decision REAL,
    time_to_10x_from_decision REAL,
    time_to_20x_from_decision REAL,
    time_to_50x_from_decision REAL,
    maximum_adverse_excursion REAL,
    maximum_favorable_excursion REAL,
    terminal_failure INTEGER,
    copyability_at_decision INTEGER,
    outcome_mature_at TEXT,
    last_observed_at TEXT,
    outcome_state TEXT NOT NULL DEFAULT 'OPEN',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incremental_feature_state_v15 (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    last_event_id TEXT NOT NULL,
    last_event_timestamp TEXT NOT NULL,
    last_available_timestamp TEXT NOT NULL,
    state_version TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    trade_count INTEGER NOT NULL,
    buy_count INTEGER NOT NULL,
    sell_count INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incremental_actor_state_v15 (
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    actor TEXT NOT NULL,
    buy_count INTEGER NOT NULL DEFAULT 0,
    sell_count INTEGER NOT NULL DEFAULT 0,
    buy_sol REAL NOT NULL DEFAULT 0,
    sell_sol REAL NOT NULL DEFAULT 0,
    post_sell_buy_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(token_id, actor)
);

CREATE TABLE IF NOT EXISTS incremental_actor_window_v15 (
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    actor TEXT NOT NULL,
    window_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(token_id, actor, window_key)
);

CREATE TABLE IF NOT EXISTS decision_model_registry_v15 (
    model_name TEXT NOT NULL,
    target_multiple INTEGER NOT NULL,
    model_kind TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    evaluation_universe_hash TEXT NOT NULL,
    calibration_state TEXT NOT NULL,
    trained_through TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    approved_at TEXT,
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route = 0),
    PRIMARY KEY(model_name, target_multiple, model_version)
);

CREATE INDEX IF NOT EXISTS idx_runner_decisions_token_time
    ON runner_decisions_v15(token_id, decision_at DESC);
CREATE INDEX IF NOT EXISTS idx_runner_decisions_route
    ON runner_decisions_v15(route_state, decision_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_open
    ON decision_outcomes_v15(outcome_state, decision_at);
