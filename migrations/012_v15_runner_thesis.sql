-- V1.5 runner-first thesis memory. All decisions are shadow-only until a later
-- human-approved model promotion. The immutable call ledger freezes prospective
-- evidence without creating a Discord/outbox route.
CREATE TABLE IF NOT EXISTS runner_analogue_memory_v15 (
    analogue_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL,
    chain TEXT NOT NULL,
    thesis_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    regime TEXT NOT NULL DEFAULT 'UNKNOWN',
    decision_timestamp TEXT NOT NULL,
    outcome_available_at TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    peak_multiple REAL NOT NULL,
    terminal_failure INTEGER NOT NULL CHECK(terminal_failure IN (0,1)),
    actionable_at_decision INTEGER CHECK(actionable_at_decision IN (0,1)),
    entry_market_cap REAL,
    maximum_adverse_excursion REAL,
    time_to_2x_seconds REAL,
    source_dataset TEXT NOT NULL,
    point_in_time_safe INTEGER NOT NULL CHECK(point_in_time_safe IN (0,1)),
    evidence_json TEXT NOT NULL,
    UNIQUE(entity_key,decision_timestamp,feature_version)
);

CREATE TABLE IF NOT EXISTS runner_theses_v15 (
    thesis_id TEXT PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    prior_thesis_id TEXT REFERENCES runner_theses_v15(thesis_id),
    trigger_event_id TEXT REFERENCES canonical_events(event_id),
    decision_timestamp TEXT NOT NULL,
    available_at TEXT NOT NULL,
    thesis_type TEXT NOT NULL,
    formation_reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'DISCOVERED','OBSERVING','THESIS_FORMING','STRENGTHENING','CONFIRMED',
        'CALL_READY','CALLED','WEAKENING','INVALIDATED','ENTRY_NOT_COPYABLE'
    )),
    stage TEXT NOT NULL,
    expected_horizon TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    contradictory_evidence_json TEXT NOT NULL,
    unresolved_risks_json TEXT NOT NULL,
    evidence_freshness_json TEXT NOT NULL,
    runner_probability REAL NOT NULL CHECK(runner_probability BETWEEN 0 AND 1),
    failure_probability REAL NOT NULL CHECK(failure_probability BETWEEN 0 AND 1),
    actionable_probability REAL NOT NULL CHECK(actionable_probability BETWEEN 0 AND 1),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    uncertainty REAL NOT NULL CHECK(uncertainty BETWEEN 0 AND 1),
    analogous_successes_json TEXT NOT NULL,
    analogous_failures_json TEXT NOT NULL,
    invalidation_conditions_json TEXT NOT NULL,
    next_observation_required_json TEXT NOT NULL,
    call_readiness TEXT NOT NULL,
    feature_vector_json TEXT NOT NULL,
    thesis_version TEXT NOT NULL,
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route=0),
    created_at TEXT NOT NULL,
    UNIQUE(token_id,decision_timestamp,thesis_version)
);

CREATE TABLE IF NOT EXISTS runner_thesis_transitions_v15 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    thesis_id TEXT NOT NULL REFERENCES runner_theses_v15(thesis_id),
    prior_thesis_id TEXT REFERENCES runner_theses_v15(thesis_id),
    transitioned_at TEXT NOT NULL,
    prior_state TEXT,
    new_state TEXT NOT NULL,
    runner_probability_delta REAL,
    failure_probability_delta REAL,
    actionability_probability_delta REAL,
    reason_json TEXT NOT NULL,
    UNIQUE(thesis_id,new_state)
);

CREATE TABLE IF NOT EXISTS prospective_shadow_calls_v15 (
    shadow_call_id TEXT PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    thesis_id TEXT NOT NULL REFERENCES runner_theses_v15(thesis_id),
    frozen_at TEXT NOT NULL,
    thesis_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    tier TEXT NOT NULL,
    entry_state TEXT NOT NULL,
    entry_market_cap REAL,
    entry_price REAL,
    runner_probability REAL NOT NULL,
    failure_probability REAL NOT NULL,
    actionable_probability REAL NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    latency_json TEXT NOT NULL,
    model_version TEXT NOT NULL,
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route=0),
    created_at TEXT NOT NULL,
    UNIQUE(token_id,model_version)
);

CREATE TABLE IF NOT EXISTS prospective_shadow_outcomes_v15 (
    shadow_call_id TEXT PRIMARY KEY REFERENCES prospective_shadow_calls_v15(shadow_call_id),
    outcome_available_at TEXT NOT NULL,
    peak_multiple REAL,
    reached_2x INTEGER CHECK(reached_2x IN (0,1)),
    reached_5x INTEGER CHECK(reached_5x IN (0,1)),
    reached_10x INTEGER CHECK(reached_10x IN (0,1)),
    reached_20x INTEGER CHECK(reached_20x IN (0,1)),
    reached_50x INTEGER CHECK(reached_50x IN (0,1)),
    maximum_adverse_excursion REAL,
    terminal_failure INTEGER CHECK(terminal_failure IN (0,1)),
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runner_reflections_v15 (
    reflection_id TEXT PRIMARY KEY,
    shadow_call_id TEXT NOT NULL UNIQUE REFERENCES prospective_shadow_outcomes_v15(shadow_call_id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    thesis_type TEXT NOT NULL,
    predicted_runner_probability REAL NOT NULL,
    predicted_failure_probability REAL NOT NULL,
    predicted_actionable_probability REAL NOT NULL,
    realized_2x INTEGER NOT NULL CHECK(realized_2x IN (0,1)),
    realized_peak_multiple REAL NOT NULL,
    brier_2x REAL NOT NULL,
    error_class TEXT NOT NULL,
    false_positive INTEGER NOT NULL CHECK(false_positive IN (0,1)),
    root_cause_json TEXT NOT NULL,
    counterfactual_json TEXT NOT NULL,
    outcome_available_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runner_theses_token_time
ON runner_theses_v15(token_id,decision_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_runner_theses_state_time
ON runner_theses_v15(state,decision_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_analogue_pit_lookup
ON runner_analogue_memory_v15(chain,stage,outcome_available_at,thesis_type);

CREATE INDEX IF NOT EXISTS idx_shadow_calls_time
ON prospective_shadow_calls_v15(frozen_at,thesis_type,stage);

CREATE INDEX IF NOT EXISTS idx_runner_reflections_outcome
ON runner_reflections_v15(outcome_available_at,thesis_type,error_class);

CREATE TRIGGER IF NOT EXISTS runner_thesis_no_public_route
BEFORE UPDATE OF public_route ON runner_theses_v15
WHEN NEW.public_route != 0
BEGIN SELECT RAISE(ABORT, 'runner thesis has no public route'); END;

CREATE TRIGGER IF NOT EXISTS shadow_call_no_public_route
BEFORE UPDATE OF public_route ON prospective_shadow_calls_v15
WHEN NEW.public_route != 0
BEGIN SELECT RAISE(ABORT, 'prospective shadow call has no public route'); END;

CREATE TRIGGER IF NOT EXISTS immutable_shadow_call_update
BEFORE UPDATE ON prospective_shadow_calls_v15
BEGIN SELECT RAISE(ABORT, 'prospective shadow calls are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_shadow_call_delete
BEFORE DELETE ON prospective_shadow_calls_v15
BEGIN SELECT RAISE(ABORT, 'prospective shadow calls are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_shadow_outcome_update
BEFORE UPDATE ON prospective_shadow_outcomes_v15
BEGIN SELECT RAISE(ABORT, 'prospective shadow outcomes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_shadow_outcome_delete
BEFORE DELETE ON prospective_shadow_outcomes_v15
BEGIN SELECT RAISE(ABORT, 'prospective shadow outcomes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_runner_reflection_update
BEFORE UPDATE ON runner_reflections_v15
BEGIN SELECT RAISE(ABORT, 'runner reflections are immutable'); END;

CREATE TRIGGER IF NOT EXISTS immutable_runner_reflection_delete
BEFORE DELETE ON runner_reflections_v15
BEGIN SELECT RAISE(ABORT, 'runner reflections are immutable'); END;
