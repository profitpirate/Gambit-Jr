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
    "e4_v12_latency300_scaleout_oracle_tests",
    SCRIPTS / "e4_v12_latency300_scaleout_oracle.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-scaleout-oracle.json").read_text(
            encoding="utf-8"
        )
    )


def test_oracle_is_bound_to_frozen_risk_set_and_scaleout_grid() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == diagnostic.source_code_fingerprint(
        ROOT
    )
    assert identity["latency_ms"] == 300
    assert identity["policy_grid"] == [policy.key for policy in diagnostic.POLICIES]
    assert result["trace_audit"]["selected_traces"] == 317
    assert all(fold["policies_evaluated"] == 16 for fold in result["folds"])


def test_no_scaleout_policy_repairs_final_chronological_fold() -> None:
    result = report()
    assert [fold["feasible_policies"] for fold in result["folds"]] == [12, 15, 6, 0]
    best = result["final_fold_oracle_best"]
    assert best["win_rate"] == 0.525
    assert best["net_pnl_sol"] < -0.37
    assert best["profit_factor"] < 0.39
    assert best["fold_gate_passed"] is False
    assert result["candidate_warranted"] is False
    assert result["retired_family"] is True
    assert result["failure_classification"] == "EXIT_FAILURE"


def test_oracle_does_not_consume_live_or_authorise_production() -> None:
    result = report()
    assert result["candidate_frozen"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
