CREATE TABLE IF NOT EXISTS feature_approval_evidence_v15 (
    feature_name TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    train_window_json TEXT NOT NULL,
    validation_window_json TEXT NOT NULL,
    test_window_json TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    ablation_json TEXT NOT NULL,
    leakage_state TEXT NOT NULL CHECK(leakage_state='PASS'),
    drift_state TEXT NOT NULL,
    approval_state TEXT NOT NULL CHECK(approval_state='APPROVED'),
    approver TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(feature_name, feature_version),
    FOREIGN KEY(feature_name, feature_version)
        REFERENCES approved_feature_registry(feature_name, feature_version)
);
