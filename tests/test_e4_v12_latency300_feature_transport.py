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
    "e4_v12_latency300_feature_transport_tests",
    SCRIPTS / "e4_v12_latency300_feature_transport.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-feature-transport.json"
        ).read_text(encoding="utf-8")
    )


def test_auc_falls_back_to_chance_for_degenerate_inputs() -> None:
    labels = np.asarray([False, True, False, True])
    assert research.auc_or_chance(labels, np.zeros(4)) == 0.5
    assert research.auc_or_chance(np.ones(4), np.arange(4)) == 0.5


def test_identity_binds_exact_labels_and_frozen_epoch_orientation() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["latency_ms"] == 300
    assert identity["descriptive_auc_floor"] == 0.52
    assert len(identity["chronological_epochs"]) == 4
    assert all(len(epoch) == 16 for epoch in identity["chronological_epochs"])
    assert "epoch 0" in identity["orientation_policy"]


def test_feature_transport_counts_and_prevalence_are_preserved() -> None:
    result = report()
    assert result["feature_count"] == 58
    assert result["direction_consistent_features"] == 32
    assert result["all_epoch_auc_52_feature_count"] == 24
    assert result["epochs"][0]["rows"] == 47_571
    assert result["epochs"][-1]["rows"] == 48_000
    assert result["epochs"][0]["positive_rate"] > 0.166
    assert result["epochs"][-1]["positive_rate"] < 0.124
    assert result["features"][0]["feature"] == "real_token_reserve"
    assert result["features"][0]["minimum_later_oriented_auc"] > 0.648
    assert "price_multiple_from_create" in result["all_epoch_auc_52_features"]


def test_diagnostic_does_not_fit_or_authorise_a_candidate() -> None:
    result = report()
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
