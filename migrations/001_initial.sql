PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY,
  chain TEXT NOT NULL,
  token_address TEXT NOT NULL,
  symbol TEXT,
  name TEXT,
  source TEXT NOT NULL,
  first_discovered_at TEXT NOT NULL,
  estimated_created_at TEXT,
  pair_address TEXT,
  deployer TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(chain, token_address)
);

CREATE TABLE IF NOT EXISTS token_snapshots (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  captured_at TEXT NOT NULL,
  source TEXT NOT NULL,
  market_cap_usd REAL,
  price_usd REAL,
  liquidity_usd REAL,
  volume_5m_usd REAL,
  holder_count INTEGER,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  evaluated_at TEXT NOT NULL,
  classification TEXT NOT NULL,
  score REAL NOT NULL,
  confidence REAL NOT NULL,
  hard_rejections_json TEXT NOT NULL,
  component_scores_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  scoring_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  signal_timestamp TEXT NOT NULL,
  signal_price_usd REAL,
  signal_market_cap_usd REAL NOT NULL CHECK(signal_market_cap_usd > 0),
  signal_liquidity_usd REAL,
  signal_holder_count INTEGER,
  signal_volume_5m_usd REAL,
  signal_score REAL NOT NULL,
  signal_class TEXT NOT NULL,
  component_scores_json TEXT NOT NULL,
  developer_state_json TEXT NOT NULL,
  narrative_state_json TEXT NOT NULL,
  social_state_json TEXT NOT NULL,
  onchain_state_json TEXT NOT NULL,
  risk_flags_json TEXT NOT NULL,
  scoring_version TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  current_market_cap_usd REAL,
  current_score REAL,
  max_multiple REAL NOT NULL DEFAULT 1,
  max_drawdown REAL NOT NULL DEFAULT 0,
  ath_market_cap_usd REAL,
  atl_market_cap_usd REAL,
  last_monitored_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(token_id, scoring_version)
);

CREATE TRIGGER IF NOT EXISTS signals_immutable_snapshot
BEFORE UPDATE OF signal_timestamp, signal_price_usd, signal_market_cap_usd,
  signal_liquidity_usd, signal_holder_count, signal_volume_5m_usd,
  signal_score, component_scores_json, developer_state_json,
  narrative_state_json, social_state_json, onchain_state_json,
  risk_flags_json, scoring_version ON signals
BEGIN
  SELECT RAISE(ABORT, 'initial signal snapshot is immutable');
END;

CREATE TABLE IF NOT EXISTS signal_updates (
  id INTEGER PRIMARY KEY,
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  update_timestamp TEXT NOT NULL,
  update_type TEXT NOT NULL,
  previous_score REAL,
  new_score REAL,
  reasons_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
  id INTEGER PRIMARY KEY,
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  multiple REAL NOT NULL,
  hit_at TEXT NOT NULL,
  market_cap_usd REAL NOT NULL,
  seconds_to_hit REAL NOT NULL,
  UNIQUE(signal_id, multiple)
);

CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  remote_message_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS developers (
  id INTEGER PRIMARY KEY,
  primary_wallet TEXT NOT NULL UNIQUE,
  classification TEXT NOT NULL,
  reputation_score REAL,
  evidence_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS developer_wallets (
  developer_id INTEGER NOT NULL REFERENCES developers(id),
  wallet TEXT NOT NULL UNIQUE,
  relationship TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY(developer_id, wallet)
);
CREATE TABLE IF NOT EXISTS developer_tokens (
  developer_id INTEGER NOT NULL REFERENCES developers(id),
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  outcome_json TEXT NOT NULL,
  PRIMARY KEY(developer_id, token_id)
);
CREATE TABLE IF NOT EXISTS wallet_profiles (
  wallet TEXT PRIMARY KEY,
  classification TEXT NOT NULL,
  performance_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS narratives (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS social_snapshots (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  captured_at TEXT NOT NULL,
  source TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY,
  token_id INTEGER NOT NULL REFERENCES tokens(id),
  signal_id INTEGER REFERENCES signals(id),
  detected_at TEXT NOT NULL,
  risk_code TEXT NOT NULL,
  evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health (
  provider TEXT PRIMARY KEY,
  healthy INTEGER NOT NULL,
  consecutive_failures INTEGER NOT NULL,
  last_success_at TEXT,
  last_failure_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_versions (
  version TEXT PRIMARY KEY,
  weights_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_address ON tokens(token_address);
CREATE INDEX IF NOT EXISTS idx_snapshots_token_time ON token_snapshots(token_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_time ON evaluations(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_score ON evaluations(score);
CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(active);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(signal_score);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(signal_timestamp);
CREATE INDEX IF NOT EXISTS idx_milestones_signal ON milestones(signal_id);
CREATE INDEX IF NOT EXISTS idx_developer_wallet ON developer_wallets(wallet);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox(sent_at, created_at);

