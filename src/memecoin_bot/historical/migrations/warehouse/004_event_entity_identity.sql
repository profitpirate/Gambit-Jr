DROP TRIGGER IF EXISTS immutable_event_update;
DROP INDEX IF EXISTS idx_events_entity_time;

ALTER TABLE normalized_events RENAME TO normalized_events_legacy;

CREATE TABLE normalized_events (
    event_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES raw_evidence(evidence_id),
    dataset_version TEXT NOT NULL,
    entity_key TEXT NOT NULL REFERENCES canonical_entities(entity_key),
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    values_json TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    UNIQUE(evidence_id, event_type, entity_key, observed_at, values_json)
);

INSERT INTO normalized_events
SELECT * FROM normalized_events_legacy;

DROP TABLE normalized_events_legacy;

CREATE INDEX idx_events_entity_time
ON normalized_events(entity_key, available_at, observed_at);

CREATE TRIGGER immutable_event_update BEFORE UPDATE ON normalized_events
BEGIN SELECT RAISE(ABORT, 'normalized historical events are immutable'); END;
