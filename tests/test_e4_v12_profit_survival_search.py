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


search = load_module(
    "e4_v12_profit_survival_search_tests",
    SCRIPTS / "e4_v12_profit_survival_search.py",
)


def point(
    offset_ms: int,
    *,
    kind: str = "BUY",
    price: float = 1.0,
    trader: str = "buyer",
    sol: float = 0.1,
):
    return search.Point(
        timestamp_ns=1_000_000_000 + offset_ms * 1_000_000,
        kind=kind,
        trader=trader,
        signature=f"sig-{offset_ms}",
        slot=1,
        sol_amount=sol,
        price_sol=price,
        virtual_sol=30.0 * price,
        virtual_tokens=30_000_000.0,
        real_tokens=20_000_000.0,
        complete=False,
    )


def trace() -> object:
    return search.Trace(
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
        creator_prior_launch_count=2,
        points=[
            point(0, kind="CREATE", price=1.0, trader="creator", sol=0.0),
            point(100, price=1.05),
            point(200, price=1.10),
            point(300, price=1.50),
        ],
    )


def artifact(name: str):
    return json.loads(
        (ROOT / "artifacts" / f"e4-v12-profit-survival-{name}.json").read_text(
            encoding="utf-8"
        )
    )


def test_snapshot_excludes_post_decision_events() -> None:
    row = search.snapshot(trace(), 250)
    assert row is not None
    features = dict(zip(search.FEATURE_NAMES, row))
    assert features["price_multiple_from_create"] == pytest.approx(1.10)
    assert features["prefix_max_price_multiple"] == pytest.approx(1.10)
    assert features["buy_count"] == 2


def test_outcome_begins_after_causal_horizon() -> None:
    outcome = search.trade_outcome(
        trace(), 250, search.Policy(stop=0.7, take=1.2, hold_ms=60_000)
    )
    assert outcome is not None
    assert outcome.exit_reason == "TAKE_PROFIT"
    assert outcome.exit_offset_ms == pytest.approx(50.0)


def test_higher_priority_fee_reduces_net_pnl() -> None:
    outcome = search.Outcome(1.25, 10.0, "TAKE_PROFIT")
    position = search.STARTING_BANKROLL_SOL * search.POSITION_FRACTION
    low = search.net_pnl(outcome, 0.001, position)
    high = search.net_pnl(outcome, 0.02, position)
    assert low > high
    assert low - high == pytest.approx(0.019)


def test_default_capture_inventory_keeps_holdout_sealed() -> None:
    specs = search.capture_specs(ROOT, include_holdout=False)
    assert {row.split for row in specs} == {"train", "validation"}
    assert len(specs) == 12


def test_development_audit_never_read_holdout() -> None:
    audit = artifact("data-audit")
    assert audit["include_holdout"] is False
    assert set(audit["rows_by_split"]) == {"train", "validation"}
    assert audit["future_values_in_features"] is False
    assert audit["production_paths_changed"] == 0


def test_frozen_candidate_clears_development_gate() -> None:
    frozen = artifact("frozen-candidate")
    metrics = frozen["candidate"]["validation"]
    assert frozen["status"] == "FROZEN_FOR_ONE_TIME_HISTORICAL_HOLDOUT"
    assert frozen["holdout_rows_read_before_freeze"] == 0
    assert metrics["trades"] >= 8
    assert metrics["capture_windows"] >= 2
    assert metrics["win_rate"] >= 0.65
    assert metrics["net_pnl_sol"] > 0
    assert metrics["profit_factor"] >= 1.25
    assert metrics["maximum_drawdown_fraction"] <= 0.15


def test_experiment_identity_is_deterministic_and_complete() -> None:
    frozen = artifact("frozen-candidate")
    identity = frozen["identity"]
    assert frozen["experiment_id"] == f"e4x-{search.stable_hash(identity)}"
    assert identity["thesis_family_identifier"] == search.THESIS_FAMILY
    assert identity["chronological_split"]["holdout"] == "sealed until explicit certification"
    assert identity["exit_policy"]["uses_e4_future_sells"] is False


def test_development_replay_is_deterministic() -> None:
    report = artifact("development-search")
    winner = report["winner"]
    assert winner["validation"]["ledger_hash"] == (
        artifact("frozen-candidate")["development_ledger_hash"]
    )
    assert report["holdout_rows_read"] == 0
