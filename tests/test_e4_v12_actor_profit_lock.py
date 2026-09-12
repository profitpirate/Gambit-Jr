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
    "e4_v12_actor_profit_lock_tests",
    SCRIPTS / "e4_v12_actor_profit_lock.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-actor-profit-lock-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_actor_profit_lock_grid_covers_fine_frontier() -> None:
    assert len(research.POLICIES) == 228
    fractions = {policy.partial_fraction for policy in research.POLICIES}
    assert {0.52, 0.55, 0.58, 0.60}.issubset(fractions)


def test_combined_thesis_is_profitable_but_not_robust_enough() -> None:
    result = report()
    winner = result["winner"]
    assert winner["win_rate"] >= 0.65
    assert winner["net_pnl_sol"] > 0
    assert winner["profit_factor"] >= 1.25
    assert result["walk_forward_gate_passed"] is False
    assert result["ready_for_strictly_later_evidence"] is False


def test_raw_actor_cohort_sources_are_verified_and_production_safe() -> None:
    result = report()
    assert result["actor_candidates"] >= 200
    assert all(row["hash_match"] for row in result["source_audit"])
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_paths_changed"] == 0
