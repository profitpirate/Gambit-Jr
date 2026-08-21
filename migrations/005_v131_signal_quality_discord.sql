PRAGMA foreign_keys = ON;

-- Candidate lifecycle: attempts and TTL are authoritative even when no pair/provider data exists.
ALTER TABLE candidates ADD COLUMN last_attempted_at TEXT;
ALTER TABLE candidates ADD COLUMN next_retry_at TEXT;
ALTER TABLE candidates ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN consecutive_missing_pair_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN consecutive_provider_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN pending_since TEXT;
ALTER TABLE candidates ADD COLUMN last_successful_snapshot_at TEXT;
ALTER TABLE candidates ADD COLUMN lifecycle_reason TEXT;
ALTER TABLE candidates ADD COLUMN retry_class TEXT;
ALTER TABLE candidates ADD COLUMN scheduling_lane TEXT NOT NULL DEFAULT 'FRESH';
ALTER TABLE candidates ADD COLUMN previous_reason TEXT;

-- Explicit provider truth. The legacy boolean remains for compatibility/reporting.
ALTER TABLE provider_health ADD COLUMN state TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE provider_health ADD COLUMN disabled_reason TEXT;
UPDATE provider_health SET state=CASE
 WHEN UPPER(COALESCE(last_error,'')) LIKE '%DISABLED%' THEN 'DISABLED'
 WHEN healthy=1 THEN 'HEALTHY'
 WHEN last_success_at IS NOT NULL THEN 'DEGRADED'
 ELSE 'DOWN' END WHERE state='UNKNOWN';

CREATE TABLE IF NOT EXISTS reconciliation_runs (
 id INTEGER PRIMARY KEY,
 reconciliation_key TEXT NOT NULL UNIQUE,
 started_at TEXT NOT NULL,
 completed_at TEXT NOT NULL,
 expired_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guild_settings (
 guild_id TEXT PRIMARY KEY,
 alert_channel_id TEXT,
 alerts_enabled INTEGER NOT NULL DEFAULT 0,
 alert_tier TEXT NOT NULL DEFAULT 'HOT' CHECK(alert_tier IN ('ALL','HOT','PRIORITY','QUALIFIED')),
 created_at TEXT NOT NULL,
 updated_by TEXT,
 updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_deliveries_v131 (
 id INTEGER PRIMARY KEY,
 outbox_id INTEGER NOT NULL REFERENCES outbox(id),
 guild_id TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0,
 remote_message_id TEXT,
 delivered_at TEXT,
 last_error TEXT,
 UNIQUE(outbox_id,guild_id,channel_id)
);

CREATE TABLE IF NOT EXISTS confidence_history (
 id INTEGER PRIMARY KEY,
 candidate_id INTEGER NOT NULL REFERENCES candidates(id),
 recorded_at TEXT NOT NULL,
 normalized_score REAL,
 confidence REAL,
 convergence_score REAL,
 setup_quality TEXT,
 reason TEXT,
 UNIQUE(candidate_id,recorded_at)
);

CREATE TABLE IF NOT EXISTS narrative_events (
 id INTEGER PRIMARY KEY,
 token_id INTEGER NOT NULL REFERENCES tokens(id),
 event_key TEXT NOT NULL UNIQUE,
 detected_at TEXT NOT NULL,
 narrative TEXT NOT NULL,
 freshness TEXT NOT NULL,
 saturation TEXT NOT NULL,
 quality TEXT NOT NULL,
 evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalyst_events (
 id INTEGER PRIMARY KEY,
 token_id INTEGER NOT NULL REFERENCES tokens(id),
 event_key TEXT NOT NULL UNIQUE,
 detected_at TEXT NOT NULL,
 catalyst_type TEXT NOT NULL,
 timing TEXT NOT NULL,
 confidence REAL,
 evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_events (
 id INTEGER PRIMARY KEY,
 event_key TEXT NOT NULL UNIQUE,
 headline TEXT NOT NULL,
 summary TEXT,
 detected_at TEXT NOT NULL,
 published_at TEXT,
 retrieved_at TEXT NOT NULL,
 entity TEXT,
 narrative TEXT,
 confidence REAL,
 provenance TEXT NOT NULL,
 source_url TEXT,
 payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_outcomes (
 radar_event_id INTEGER PRIMARY KEY REFERENCES radar_events(id),
 peak_multiple REAL,
 current_multiple REAL,
 status TEXT NOT NULL DEFAULT 'TRACKING',
 first_2x_at TEXT,
 failed_at TEXT,
 updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_alert_events (
 id INTEGER PRIMARY KEY,
 guild_id TEXT,
 channel_id TEXT NOT NULL,
 requested_by TEXT,
 delivered_at TEXT NOT NULL,
 remote_message_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_candidates_retry_due ON candidates(state,next_retry_at,scheduling_lane);
CREATE INDEX IF NOT EXISTS idx_candidates_attempt_age ON candidates(first_discovered_at,last_attempted_at);
CREATE INDEX IF NOT EXISTS idx_delivery_v131_pending ON alert_deliveries_v131(status,guild_id,channel_id);
CREATE INDEX IF NOT EXISTS idx_confidence_candidate_time ON confidence_history(candidate_id,recorded_at);
CREATE INDEX IF NOT EXISTS idx_narrative_token_time ON narrative_events(token_id,detected_at);
CREATE INDEX IF NOT EXISTS idx_catalyst_token_time ON catalyst_events(token_id,detected_at);
CREATE INDEX IF NOT EXISTS idx_news_narrative_time ON news_events(narrative,published_at);
