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


research = load_module(
    "e4_v12_causal_dynamic_exit_tests",
    SCRIPTS / "e4_v12_causal_dynamic_exit.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-causal-dynamic-exit-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_dynamic_exit_choices_are_fixed_and_materially_distinct() -> None:
    assert research.SAFE_POLICY.partial_fraction == 1.0
    assert len(research.RUNNER_POLICIES) == 3
    assert all(policy.partial_fraction < 1.0 for policy in research.RUNNER_POLICIES)
    assert len(research.THRESHOLDS) == 5


def test_dynamic_exit_does_not_beat_robust_fixed_runner() -> None:
    result = report()
    assert result["winner"]["candidate"].startswith("runner_only=")
    assert result["winner"]["net_pnl_sol"] > 0
    assert result["winner"]["profit_factor"] >= 1.25
    assert result["walk_forward_gate_passed"] is False
    assert result["ready_for_strictly_later_evidence"] is False


def test_source_and_promotion_guards_remain_closed() -> None:
    result = report()
    assert all(row["hash_match"] for row in result["source_audit"])
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_paths_changed"] == 0
