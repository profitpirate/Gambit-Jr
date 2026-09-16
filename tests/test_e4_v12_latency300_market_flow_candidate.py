from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_latency300_market_flow_candidate_tests",
    ROOT / "scripts/e4_v12_latency300_market_flow_candidate.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def artifact() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-market-flow-candidate.json").read_text(
            encoding="utf-8"
        )
    )


def test_candidate_consumes_exact_frozen_transport_features() -> None:
    discovery = json.loads(research.DISCOVERY_PATH.read_text(encoding="utf-8"))
    assert tuple(discovery["stable_features"]) == research.STABLE_FLOW_FEATURE_NAMES
    assert len(research.STABLE_FLOW_FEATURE_NAMES) == 7


def test_candidate_is_chronological_and_production_is_untouched() -> None:
    report = artifact()
    for fold in report["identity"]["chronological_split"]:
        fit = [int(value) for value in fold["fit"]]
        calibration = [int(value) for value in fold["calibration"]]
        test = [int(value) for value in fold["test"]]
        assert max(fit) < min(calibration) < min(test)
        assert set(fit).isdisjoint(calibration)
        assert set(calibration).isdisjoint(test)
    assert report["anti_lookahead"]["market_flow_uses_strictly_earlier_decision_snapshots"] is True
    assert report["anti_lookahead"]["equal_timestamp_launches_are_batched"] is True
    assert report["anti_lookahead"]["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
    assert report["production_deployment_authorised"] is False


def test_candidate_verdict_matches_every_frozen_requirement() -> None:
    report = artifact()
    passed = all(report["requirements"].values())
    assert report["development_gate_passed"] is passed
    assert report["retired"] is (not passed)
    assert report["ready_for_new_strictly_later_holdout"] is passed
    assert report["untouched_holdout_passed"] is False
    assert report["live_confirmation_authorised"] is False
