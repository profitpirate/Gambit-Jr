from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


research = load_module(
    "e4_v12_latency300_anchor_crowd_transport_tests",
    SCRIPTS / "e4_v12_latency300_anchor_crowd_transport.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-anchor-crowd-transport.json").read_text(
            encoding="utf-8"
        )
    )


def test_interactions_are_fixed_and_finite() -> None:
    micro_matrix = np.zeros((2, len(research.micro.MICRO_FEATURE_NAMES)))
    participant_matrix = np.zeros(
        (2, len(research.participants.PARTICIPANT_FEATURE_NAMES))
    )
    micro_columns = {
        name: index for index, name in enumerate(research.micro.MICRO_FEATURE_NAMES)
    }
    participant_columns = {
        name: index
        for index, name in enumerate(research.participants.PARTICIPANT_FEATURE_NAMES)
    }
    micro_matrix[:, micro_columns["micro_log_price_range_250ms"]] = [0.5, 1.0]
    participant_matrix[
        :, participant_columns["participant_effective_count_250ms"]
    ] = [3.0, 7.0]
    participant_matrix[
        :, participant_columns["participant_log_flow_per_trader_250ms"]
    ] = [0.2, 0.4]
    participant_matrix[
        :, participant_columns["participant_largest_flow_share_250ms"]
    ] = [0.6, 0.7]
    participant_matrix[:, participant_columns["participant_flow_hhi_250ms"]] = [
        0.4,
        0.5,
    ]
    participant_matrix[
        :, participant_columns["participant_normalized_entropy_250ms"]
    ] = [0.8, 0.9]
    matrix = research.interaction_matrix(micro_matrix, participant_matrix)
    assert matrix.shape == (2, len(research.FEATURE_NAMES))
    assert np.all(np.isfinite(matrix))
    assert matrix[0, 0] == 0.5 * np.log1p(3.0)
    assert matrix[1, 5] == 1.0 * np.log1p(7.0) * 0.7
    assert matrix[1, 7] == 1.0 * 0.4 * 0.7


def test_report_identity_and_causality_are_bound() -> None:
    result = report()
    identity = result["identity"]
    audit = result["source_audit"]
    assert result["diagnostic_id"] == f"e4d-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["latency_ms"] == 300
    assert identity["causal_horizon_ms"] == 250
    assert identity["interaction_matrix_sha256"] == audit["interaction_matrix_sha256"]
    assert audit["rows"] == 191_425
    assert audit["windows"] == 64
    assert audit["raw_capture_bytes"] == 11_789_578_939
    assert audit["raw_source_hashes_verified"] is True
    assert audit["future_values_in_features"] is False
    assert audit["active_untouched_live_data_used"] is False


def test_diagnostic_never_fits_or_authorises_a_candidate() -> None:
    result = report()
    assert result["interactions_improving_minimum_later_auc_count"] == 0
    assert result["interactions_improving_minimum_later_auc"] == []
    assert result["candidate_warranted"] is False
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
