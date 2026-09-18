-- Full-bot operational hardening: bounded Discord retry scheduling.
-- The base columns are re-declared so very old/minimal schemas can still be
-- upgraded in place; Store.migrate strips ALTERs for columns already present.
ALTER TABLE outbox ADD COLUMN event_key TEXT;
ALTER TABLE outbox ADD COLUMN event_type TEXT;
ALTER TABLE outbox ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE outbox ADD COLUMN created_at TEXT;
ALTER TABLE outbox ADD COLUMN sent_at TEXT;
ALTER TABLE outbox ADD COLUMN remote_message_id TEXT;
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN last_error TEXT;
ALTER TABLE outbox ADD COLUMN claim_token TEXT;
ALTER TABLE outbox ADD COLUMN claimed_at TEXT;
ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox ADD COLUMN dead_lettered_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_key_hardening
ON outbox(event_key) WHERE event_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_outbox_retry_ready
ON outbox(sent_at,dead_lettered_at,next_attempt_at,claimed_at,id);
