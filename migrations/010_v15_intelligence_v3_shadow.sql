-- Research-only V3 comparison ledger. It is deliberately disconnected from signals/outbox.
CREATE TABLE IF NOT EXISTS intelligence_v3_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    decision_timestamp TEXT NOT NULL,
    available_evidence_timestamp TEXT NOT NULL,
    control_decision_json TEXT NOT NULL,
    v2_decision_json TEXT NOT NULL,
    v3_decision_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    latency_json TEXT NOT NULL,
    veto_reasons_json TEXT NOT NULL,
    later_outcome_json TEXT,
    model_version TEXT NOT NULL CHECK(model_version = 'INTELLIGENCE_V3_RESEARCH'),
    public_route INTEGER NOT NULL DEFAULT 0 CHECK(public_route = 0),
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, decision_timestamp, model_version)
);

CREATE INDEX IF NOT EXISTS idx_v3_shadow_candidate_time
ON intelligence_v3_shadow_decisions(candidate_id, decision_timestamp DESC);

CREATE TRIGGER IF NOT EXISTS intelligence_v3_shadow_no_public_route_update
BEFORE UPDATE OF public_route ON intelligence_v3_shadow_decisions
WHEN NEW.public_route != 0
BEGIN
    SELECT RAISE(ABORT, 'Intelligence V3 has no public notifier route');
END;
