from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_latency300_temporal_consensus_transport_tests",
    ROOT / "scripts/e4_v12_latency300_temporal_consensus_transport.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def artifact() -> dict:
    return json.loads(research.OUTPUT.read_text(encoding="utf-8"))


def test_empirical_percentile_uses_only_the_fixed_reference() -> None:
    reference = np.asarray([1.0, 3.0, 2.0, 2.0])
    values = np.asarray([0.0, 1.0, 2.0, 2.5, 4.0])
    assert research.empirical_percentile(reference, values).tolist() == [
        0.0,
        0.25,
        0.75,
        0.75,
        1.0,
    ]


def test_diagnostic_replays_the_exact_300ms_baseline() -> None:
    report = artifact()
    audit = report["selection_audit"]
    assert audit["exact_baseline_replayed"] is True
    assert audit["concurrency_rejections_preserved"] is True
    assert audit["executed_trades"] == 326
    assert audit["wins"] == 199
    assert audit["losses"] == 127
    assert tuple(report["identity"]["training_window_policies"]) == (
        research.TRAIN_WINDOW_POLICIES
    )
    assert report["identity"]["recent_window_lengths"] == [None, 16, 8]


def test_candidate_warrant_requires_every_frozen_transport_floor() -> None:
    report = artifact()
    audit = report["consensus_audit"]
    global_pass = all(
        value >= research.GLOBAL_AUC_FLOOR
        for value in audit["minimum_member_percentile_global_auc_by_fold"]
    )
    residual_pass = all(
        value >= research.RESIDUAL_AUC_FLOOR
        for value in audit["minimum_member_percentile_residual_auc_by_fold"]
    )
    assert audit["global_transport_passed"] is global_pass
    assert audit["residual_transport_passed"] is residual_pass
    assert report["consensus_candidate_warranted"] is (
        global_pass and residual_pass
    )
    assert report["candidate_fitted"] is False
    assert report["development_gate_passed"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
    assert report["production_deployment_authorised"] is False
