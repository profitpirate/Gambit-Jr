CREATE TABLE IF NOT EXISTS research_sources_v15 (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    category TEXT NOT NULL,
    publisher TEXT NOT NULL,
    published_at TEXT,
    examined_at TEXT NOT NULL,
    access_state TEXT NOT NULL CHECK(access_state IN (
        'FULL_TEXT','ABSTRACT_AND_METADATA','METADATA_ONLY','UNAVAILABLE'
    )),
    reliability_state TEXT NOT NULL,
    relevance_state TEXT NOT NULL CHECK(relevance_state IN (
        'HIGH','MEDIUM','LOW','REJECTED'
    )),
    research_method TEXT NOT NULL,
    data_window TEXT NOT NULL,
    population TEXT NOT NULL,
    claim TEXT NOT NULL,
    test_method TEXT NOT NULL,
    result TEXT NOT NULL,
    limitations TEXT NOT NULL,
    license_state TEXT NOT NULL,
    acquisition_state TEXT NOT NULL,
    provenance_hash TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_source_hypotheses_v15 (
    source_id TEXT NOT NULL REFERENCES research_sources_v15(source_id),
    hypothesis TEXT NOT NULL,
    evidence_direction TEXT NOT NULL CHECK(evidence_direction IN (
        'SUPPORTS','CONTRADICTS','MIXED','CONTEXT_ONLY','UNTESTED'
    )),
    PRIMARY KEY(source_id,hypothesis)
);

CREATE INDEX IF NOT EXISTS idx_research_sources_relevance
ON research_sources_v15(relevance_state,category,examined_at);

CREATE INDEX IF NOT EXISTS idx_research_source_hypotheses
ON research_source_hypotheses_v15(hypothesis,evidence_direction);
