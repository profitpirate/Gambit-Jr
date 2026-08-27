-- Restored V1.4 hardening baseline for V1.5. Additive and restart compatible.
ALTER TABLE outbox ADD COLUMN claim_token TEXT;
ALTER TABLE outbox ADD COLUMN claimed_at TEXT;

CREATE TABLE IF NOT EXISTS launch_source_cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_launch_event_chain_tx_log_token
ON launch_events(chain, transaction_id, token_address)
WHERE transaction_id IS NOT NULL AND transaction_id != '';

CREATE INDEX IF NOT EXISTS idx_outbox_claimed_at ON outbox(claimed_at, id);
