from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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


diagnostic = load_module(
    "e4_v12_candidate_latency_breakpoint_tests",
    SCRIPTS / "e4_v12_candidate_latency_breakpoint.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-candidate-latency-breakpoint.json").read_text(
            encoding="utf-8"
        )
    )


def test_frontier_is_bound_to_frozen_candidate_and_exact_traces() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == diagnostic.source_code_fingerprint(
        ROOT
    )
    assert identity["latencies_ms"] == list(diagnostic.LATENCIES_MS)
    assert len(result["frontier"]) == len(diagnostic.LATENCIES_MS)
    assert result["trace_audit"]["selected_traces"] == 317
    assert all(row["quote_coverage"] == 1.0 for row in result["frontier"])


def test_breakpoint_requires_the_complete_declared_development_gate() -> None:
    result = report()
    by_latency = {row["latency_ms"]: row for row in result["frontier"]}
    passing = result["maximum_tested_strict_precision_passing_latency_ms"]
    failing = result["minimum_tested_strict_precision_failing_latency_ms"]
    assert by_latency[passing]["strict_precision_gate_passed"]
    assert not by_latency[failing]["strict_precision_gate_passed"]
    assert all(
        row["strict_precision_gate_passed"]
        == all(row["strict_precision_requirements"].values())
        for row in result["frontier"]
    )


def test_registered_latency_economic_gate_is_reported_separately() -> None:
    result = report()
    assert result["maximum_tested_strict_precision_passing_latency_ms"] == 5
    assert result["minimum_tested_strict_precision_failing_latency_ms"] == 10
    assert result["maximum_tested_registered_economic_passing_latency_ms"] == 50
    assert all(
        row["registered_latency_economic_gate_passed"]
        == all(row["registered_latency_economic_requirements"].values())
        for row in result["frontier"]
    )


def test_diagnostic_cannot_change_candidate_live_or_production() -> None:
    result = report()
    assert result["registered_candidate_latency_contract_changed"] is False
    assert result["candidate_refit"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
