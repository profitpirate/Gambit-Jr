PRAGMA foreign_keys = ON;

ALTER TABLE tokens ADD COLUMN original_description TEXT;
ALTER TABLE tokens ADD COLUMN translated_name TEXT;
ALTER TABLE tokens ADD COLUMN romanized_name TEXT;
ALTER TABLE tokens ADD COLUMN translated_description TEXT;

ALTER TABLE candidates ADD COLUMN radar_score REAL;
ALTER TABLE candidates ADD COLUMN radar_reason TEXT;
ALTER TABLE candidates ADD COLUMN radar_triggered_at TEXT;
ALTER TABLE candidates ADD COLUMN radar_market_cap_usd REAL;
ALTER TABLE candidates ADD COLUMN radar_price_usd REAL;
ALTER TABLE candidates ADD COLUMN radar_liquidity_usd REAL;

ALTER TABLE signals ADD COLUMN radar_timestamp TEXT;
ALTER TABLE signals ADD COLUMN radar_market_cap_usd REAL;
ALTER TABLE signals ADD COLUMN radar_to_signal_seconds REAL;
ALTER TABLE signals ADD COLUMN radar_to_signal_multiple REAL;
ALTER TABLE signals ADD COLUMN discovery_to_signal_seconds REAL;

CREATE TABLE IF NOT EXISTS discovery_sources (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  source TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(token_id, source)
);

CREATE TABLE IF NOT EXISTS radar_events (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  event_level TEXT NOT NULL,
  triggered_at TEXT NOT NULL,
  radar_score REAL NOT NULL,
  reason_json TEXT NOT NULL,
  market_cap_usd REAL,
  price_usd REAL,
  liquidity_usd REAL,
  snapshot_count INTEGER NOT NULL,
  UNIQUE(candidate_id, event_level)
);

CREATE TABLE IF NOT EXISTS token_outcomes (
  token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
  discovery_market_cap_usd REAL,
  peak_market_cap_usd REAL,
  max_multiple_from_discovery REAL,
  radar_occurred INTEGER NOT NULL DEFAULT 0,
  signal_id INTEGER REFERENCES signals(id),
  final_lifecycle_state TEXT,
  non_signal_reason TEXT,
  first_market_at TEXT,
  last_observed_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcome_milestones (
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  multiple REAL NOT NULL,
  hit_at TEXT NOT NULL,
  market_cap_usd REAL NOT NULL,
  radar_before_hit INTEGER NOT NULL,
  signal_before_hit INTEGER NOT NULL,
  PRIMARY KEY(token_id, multiple)
);

CREATE INDEX IF NOT EXISTS idx_discovery_sources_token ON discovery_sources(token_id, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_radar_events_candidate ON radar_events(candidate_id, triggered_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_multiple ON token_outcomes(max_multiple_from_discovery DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_observed ON token_outcomes(last_observed_at);

DROP TRIGGER IF EXISTS signals_immutable_snapshot;
CREATE TRIGGER signals_immutable_snapshot
BEFORE UPDATE OF signal_timestamp, signal_price_usd, signal_market_cap_usd,
  signal_liquidity_usd, signal_holder_count, signal_volume_5m_usd,
  signal_score, signal_class, component_scores_json, developer_state_json,
  narrative_state_json, social_state_json, onchain_state_json,
  risk_flags_json, scoring_version, normalized_score, confidence,
  candidate_history_json, radar_timestamp, radar_market_cap_usd,
  radar_to_signal_seconds, radar_to_signal_multiple, discovery_to_signal_seconds ON signals
BEGIN
  SELECT RAISE(ABORT, 'initial signal snapshot is immutable');
END;
