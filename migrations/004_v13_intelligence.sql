PRAGMA foreign_keys = ON;

ALTER TABLE radar_events ADD COLUMN immutable_payload_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE radar_events ADD COLUMN priority TEXT NOT NULL DEFAULT 'STANDARD';
ALTER TABLE radar_events ADD COLUMN software_version TEXT NOT NULL DEFAULT '1.3.0';
ALTER TABLE radar_events ADD COLUMN radar_version TEXT NOT NULL DEFAULT 'v1.3-radar';
ALTER TABLE radar_events ADD COLUMN config_fingerprint TEXT;

CREATE TABLE IF NOT EXISTS provider_evidence (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), field_name TEXT NOT NULL,
 value_json TEXT NOT NULL, provider TEXT NOT NULL, retrieved_at TEXT NOT NULL, age_seconds REAL,
 confidence TEXT NOT NULL, conflict_state TEXT NOT NULL DEFAULT 'NONE', raw_json TEXT NOT NULL,
 UNIQUE(token_id,field_name,provider,retrieved_at)
);
CREATE TABLE IF NOT EXISTS gmgn_snapshots (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), captured_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, unavailable_json TEXT NOT NULL, UNIQUE(token_id,captured_at)
);
CREATE TABLE IF NOT EXISTS wallet_intelligence (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), captured_at TEXT NOT NULL,
 smart_money_state TEXT NOT NULL, buyer_diversity TEXT NOT NULL, activity_quality TEXT NOT NULL,
 payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS priority_transitions (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), from_priority TEXT,
 to_priority TEXT NOT NULL, reason_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(token_id,to_priority)
);
CREATE TABLE IF NOT EXISTS social_metadata_history (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), captured_at TEXT NOT NULL,
 first_discovery INTEGER NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intelligence_events (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), event_key TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL, detected_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_deliveries (
 id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL REFERENCES outbox(id), channel_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, remote_message_id TEXT,
 delivered_at TEXT, last_error TEXT, UNIQUE(outbox_id,channel_id)
);
CREATE TABLE IF NOT EXISTS paper_simulations (
 id INTEGER PRIMARY KEY, radar_event_id INTEGER NOT NULL REFERENCES radar_events(id), notional_usd REAL NOT NULL,
 fee_bps REAL NOT NULL, slippage_json TEXT NOT NULL, current_value_usd REAL, peak_value_usd REAL,
 status TEXT NOT NULL, software_version TEXT NOT NULL, config_fingerprint TEXT,
 UNIQUE(radar_event_id,notional_usd)
);
CREATE TABLE IF NOT EXISTS latency_metrics (
 id INTEGER PRIMARY KEY, token_id INTEGER NOT NULL REFERENCES tokens(id), metric TEXT NOT NULL,
 started_at TEXT NOT NULL, ended_at TEXT NOT NULL, seconds REAL NOT NULL, UNIQUE(token_id,metric)
);
CREATE TABLE IF NOT EXISTS outcomes_v13 (
 token_id INTEGER PRIMARY KEY REFERENCES tokens(id), final_state TEXT, peak_multiple REAL,
 survived INTEGER, probable_rug INTEGER NOT NULL DEFAULT 0, feature_snapshot_json TEXT NOT NULL DEFAULT '{}',
 finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS config_fingerprints (
 fingerprint TEXT PRIMARY KEY, software_version TEXT NOT NULL, scoring_version TEXT NOT NULL,
 radar_version TEXT NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS radar_events_immutable
BEFORE UPDATE OF triggered_at,radar_score,reason_json,market_cap_usd,price_usd,liquidity_usd,
 snapshot_count,immutable_payload_json,software_version,radar_version,config_fingerprint ON radar_events
BEGIN SELECT RAISE(ABORT, 'original radar snapshot is immutable'); END;

CREATE INDEX IF NOT EXISTS idx_provider_evidence_token_field ON provider_evidence(token_id,field_name,retrieved_at);
CREATE INDEX IF NOT EXISTS idx_gmgn_token_time ON gmgn_snapshots(token_id,captured_at);
CREATE INDEX IF NOT EXISTS idx_wallet_intelligence_token_time ON wallet_intelligence(token_id,captured_at);
CREATE INDEX IF NOT EXISTS idx_intelligence_event_token_time ON intelligence_events(token_id,detected_at);
CREATE INDEX IF NOT EXISTS idx_delivery_pending ON alert_deliveries(status,channel_id);
