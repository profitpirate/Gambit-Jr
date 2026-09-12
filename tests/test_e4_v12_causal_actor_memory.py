from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
    "e4_v12_causal_actor_memory_tests",
    SCRIPTS / "e4_v12_causal_actor_memory.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-causal-actor-memory-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_actor_state_uses_resolved_observations_only() -> None:
    state = research.ActorState()
    assert state.resolved == 0
    assert state.bayesian_win_rate == pytest.approx(0.5)
    state.update(0.01)
    state.update(-0.02)
    assert state.resolved == 2
    assert state.wins == 1
    assert state.severe_losses == 1
    assert state.average_pnl == pytest.approx(-0.005)


def test_unknown_actors_have_neutral_point_in_time_features() -> None:
    features = research.actor_features(["buyer"], "creator", {}, {})
    values = dict(zip(research.ACTOR_FEATURE_NAMES, features, strict=True))
    assert values["known_early_buyer_count"] == 0
    assert values["creator_resolved_launches"] == 0
    assert values["creator_bayesian_win_rate"] == pytest.approx(0.5)


def test_real_actor_history_has_measured_coverage() -> None:
    audit = report()["audit"]
    assert audit["captures"] == 48
    assert audit["launches"] == 144_000
    assert audit["rows_with_known_buyer"] > 0
    assert audit["rows_with_known_creator"] > 0
    assert audit["actor_updates_delayed_ms"] == 60_000
    assert audit["future_values_in_features"] is False
    assert all(row["hash_match"] for row in audit["runs"])


def test_actor_ablation_is_not_automatically_approved() -> None:
    result = report()
    assert result["ablation"]["actor_improves_pnl"] is True
    assert result["ablation"]["actor_improves_win_rate"] is False
    assert result["ready_for_strictly_later_evidence"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_paths_changed"] == 0
