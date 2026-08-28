-- V1.5 production cohesion and query hardening. Additive and restart-safe.
ALTER TABLE signals ADD COLUMN v15_signal_tier TEXT NOT NULL DEFAULT 'UNKNOWN';

CREATE INDEX IF NOT EXISTS idx_v15_decisions_candidate_latest
ON v15_decisions(candidate_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_wallet_edge_destination
ON wallet_edges(chain, to_wallet, relationship, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_v15_tier_time
ON signals(v15_signal_tier, signal_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_provider_health_state
ON provider_health(state, updated_at DESC);
