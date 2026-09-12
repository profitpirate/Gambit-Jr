from __future__ import annotations

import importlib.util
import json
import sys
from itertools import pairwise
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
    "e4_v12_adaptive_exit_search_tests",
    SCRIPTS / "e4_v12_adaptive_exit_search.py",
)


def artifact(name: str):
    return json.loads(
        (ROOT / "artifacts" / f"e4-v12-adaptive-exit-{name}.json").read_text(
            encoding="utf-8"
        )
    )


def point(offset_ms: int, price: float):
    return research.base.Point(
        timestamp_ns=1_000_000_000 + offset_ms * 1_000_000,
        kind="BUY",
        trader="buyer",
        signature=f"sig-{offset_ms}",
        slot=1,
        sol_amount=0.1,
        price_sol=price,
        virtual_sol=30.0 * price,
        virtual_tokens=30_000_000.0,
        real_tokens=20_000_000.0,
        complete=False,
    )


def trace():
    return research.base.Trace(
        run_id="1",
        split="train",
        mint="mint",
        creator="creator",
        create_ns=1_000_000_000,
        create_slot=1,
        create_signature="sig-0",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=0,
        points=[point(0, 1.0), point(250, 1.0), point(300, 1.6), point(400, 1.15)],
    )


def test_adaptive_exit_trails_only_after_causal_peak() -> None:
    policy = research.AdaptivePolicy(0.70, 3.0, 60_000, 1.50, 0.25)
    outcome = research.adaptive_outcome(trace(), 250, policy)
    assert outcome is not None
    assert outcome.exit_reason == "TRAILING_STOP"
    assert outcome.exit_offset_ms == pytest.approx(150.0)


def test_capture_split_has_a_sealed_later_holdout() -> None:
    development = research.capture_specs(ROOT, include_holdout=False)
    complete = research.capture_specs(ROOT, include_holdout=True)
    assert len(development) == 43
    assert len(complete) == 48
    assert all(right.start_ns > left.start_ns for left, right in pairwise(complete))
    assert [spec.split for spec in complete].count("train") == 38
    assert [spec.split for spec in complete].count("validation") == 5
    assert [spec.split for spec in complete].count("holdout") == 5


def test_development_features_are_point_in_time() -> None:
    audit = artifact("data-audit")
    assert audit["holdout_opened"] is False
    assert audit["market_context_uses_prior_snapshots_only"] is True
    assert audit["future_values_in_features"] is False
    assert len(audit["feature_names"]) == len(research.FEATURE_NAMES) == 42
    assert len(audit["runs"]) == 43
    assert all(row["hash_match"] for row in audit["runs"])


def test_frozen_candidate_passes_all_development_gates() -> None:
    search = artifact("development-search")
    frozen = artifact("frozen-candidate")
    metrics = frozen["candidate"]["validation"]
    assert search["frozen_candidate_ready"] is True
    assert research.robust_rank(metrics)[0] == 9
    assert metrics["capture_windows"] == 5
    assert all(
        window["pnl_sol"] > 0 for window in metrics["by_capture_window"].values()
    )
    assert metrics["win_rate"] >= 0.65
    assert metrics["profit_factor"] >= 1.25
    assert frozen["holdout_rows_read_before_freeze"] == 0


def test_experiment_identity_is_deterministic_and_production_safe() -> None:
    frozen = artifact("frozen-candidate")
    identity = frozen["identity"]
    assert frozen["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert len(identity["chronological_split"]["holdout"]) == 5
    assert set(identity["exit_policy"]) == {
        "hold_ms",
        "initial_stop",
        "protect_activate",
        "protection_floor",
        "take",
        "trail_activate",
        "trail_retrace",
    }
    assert frozen["production_paths_changed"] == 0
