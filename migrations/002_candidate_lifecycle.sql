PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL UNIQUE REFERENCES tokens(id),
  state TEXT NOT NULL,
  reason TEXT,
  first_discovered_at TEXT NOT NULL,
  first_evaluated_at TEXT,
  last_evaluated_at TEXT,
  last_monitored_at TEXT,
  initial_market_cap_usd REAL,
  current_market_cap_usd REAL,
  initial_liquidity_usd REAL,
  current_liquidity_usd REAL,
  initial_price_usd REAL,
  current_price_usd REAL,
  initial_volume_5m_usd REAL,
  current_volume_5m_usd REAL,
  current_buys_5m INTEGER,
  current_sells_5m INTEGER,
  snapshot_count INTEGER NOT NULL DEFAULT 0,
  raw_points REAL,
  normalized_score REAL,
  confidence REAL,
  classification TEXT,
  hard_rejections_json TEXT NOT NULL DEFAULT '[]',
  waiting_reasons_json TEXT NOT NULL DEFAULT '[]',
  unknown_fields_json TEXT NOT NULL DEFAULT '[]',
  scoring_version TEXT NOT NULL,
  signal_id INTEGER REFERENCES signals(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expired_at TEXT
);

CREATE TABLE IF NOT EXISTS candidate_transitions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT,
  score REAL,
  confidence REAL,
  created_at TEXT NOT NULL
);

ALTER TABLE signals ADD COLUMN normalized_score REAL;
ALTER TABLE signals ADD COLUMN confidence REAL;
ALTER TABLE signals ADD COLUMN candidate_history_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE signals ADD COLUMN current_signal_class TEXT;
ALTER TABLE evaluations ADD COLUMN normalized_score REAL;
ALTER TABLE evaluations ADD COLUMN available_weight REAL;

DROP TRIGGER IF EXISTS signals_immutable_snapshot;
CREATE TRIGGER signals_immutable_snapshot
BEFORE UPDATE OF signal_timestamp, signal_price_usd, signal_market_cap_usd,
  signal_liquidity_usd, signal_holder_count, signal_volume_5m_usd,
  signal_score, signal_class, component_scores_json, developer_state_json,
  narrative_state_json, social_state_json, onchain_state_json,
  risk_flags_json, scoring_version, normalized_score, confidence,
  candidate_history_json ON signals
BEGIN
  SELECT RAISE(ABORT, 'initial signal snapshot is immutable');
END;

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_token ON candidates(token_id);
CREATE INDEX IF NOT EXISTS idx_candidates_active ON candidates(state, last_monitored_at);
CREATE INDEX IF NOT EXISTS idx_candidates_score ON candidates(normalized_score DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_transitions_candidate ON candidate_transitions(candidate_id, created_at);
