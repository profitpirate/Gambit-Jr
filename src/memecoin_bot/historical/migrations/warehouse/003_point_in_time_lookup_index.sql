CREATE INDEX IF NOT EXISTS idx_features_pit_version
ON point_in_time_features(entity_key, feature_version, available_at, observed_at, feature_name);
