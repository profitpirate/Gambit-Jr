-- Full-bot operational hardening: bounded Discord retry scheduling.
ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox ADD COLUMN dead_lettered_at TEXT;

CREATE INDEX IF NOT EXISTS idx_outbox_retry_ready
ON outbox(sent_at,dead_lettered_at,next_attempt_at,claimed_at,id);
