from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_latency300_market_flow_transport_tests",
    ROOT / "scripts/e4_v12_latency300_market_flow_transport.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def test_market_flow_is_strictly_causal_and_batches_equal_timestamps() -> None:
    feature_count = len(research.actors.FEATURE_NAMES)
    values = np.zeros((4, feature_count), dtype=np.float64)
    public = research.actors.FEATURE_NAMES.index("public_buy_sol")
    price = research.actors.FEATURE_NAMES.index("price_multiple_from_create")
    seed = research.actors.FEATURE_NAMES.index("creator_seed_sol")
    values[:, public] = [1.0, 2.0, 4.0, 8.0]
    values[:, price] = [1.0, 1.1, 1.2, 1.3]
    values[:, seed] = [1.0, 1.0, 2.0, 2.0]
    decisions = np.asarray([1_000_000_000, 2_000_000_000, 2_000_000_000, 8_000_000_000])

    matrix = research.causal_market_flow_matrix(decisions, values)

    count_5s = research.FLOW_FEATURE_NAMES.index("market_prior_launches_5s")
    public_5s = research.FLOW_FEATURE_NAMES.index("market_prior_public_buy_mean_5s")
    assert matrix[0, count_5s] == 0
    assert matrix[1, count_5s] == 1
    assert matrix[2, count_5s] == 1
    assert matrix[1, public_5s] == matrix[2, public_5s] == 1.0
    assert matrix[3, count_5s] == 0


def test_future_rows_cannot_change_prior_market_flow_features() -> None:
    rng = np.random.default_rng(41)
    values = rng.uniform(size=(8, len(research.actors.FEATURE_NAMES)))
    decisions = np.arange(8, dtype=np.int64) * 1_000_000_000
    short = research.causal_market_flow_matrix(decisions[:5], values[:5])
    full = research.causal_market_flow_matrix(decisions, values)
    np.testing.assert_array_equal(short, full[:5])


def test_committed_market_flow_artifact_is_diagnostic_only() -> None:
    report = json.loads(
        (ROOT / "artifacts/e4-v12-latency300-market-flow-transport.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["candidate_fitted"] is False
    assert report["development_gate_passed"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
    audit = report["flow_feature_audit"]
    assert audit["rows"] == 191_425
    assert audit["features"] == len(research.FLOW_FEATURE_NAMES)
    assert audit["strictly_earlier_decision_snapshots_only"] is True
    assert audit["same_timestamp_rows_excluded_from_one_another"] is True
    assert audit["outcomes_used_as_features"] is False
