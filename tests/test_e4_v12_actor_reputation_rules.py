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
    "e4_v12_actor_reputation_rules_tests",
    SCRIPTS / "e4_v12_actor_reputation_rules.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-actor-reputation-rules-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_rule_grid_is_bounded_and_interpretable() -> None:
    assert len(research.BUYER_RULES) == 432
    assert len(research.CREATOR_RULES) == 120
    assert all(rule.history_max_min >= 2 for rule in research.BUYER_RULES)
    assert all(rule.resolved_min >= 1 for rule in research.CREATOR_RULES)


def test_actor_rule_finds_expectancy_but_not_required_precision() -> None:
    result = report()
    winner = result["winner"]
    assert winner["net_pnl_sol"] > 0
    assert winner["profit_factor"] >= 1.25
    assert winner["positive_folds"] == 4
    assert winner["positive_capture_windows"] >= 16
    assert winner["win_rate"] < 0.65
    assert result["walk_forward_gate_passed"] is False
    assert result["ready_for_strictly_later_evidence"] is False


def test_no_consumed_result_is_mislabelled_as_holdout_or_live() -> None:
    result = report()
    assert result["identity"]["chronological_split"]["future_holdout"] == (
        "strictly later evidence still collecting"
    )
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_paths_changed"] == 0
