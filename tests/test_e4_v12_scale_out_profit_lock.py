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
    "e4_v12_scale_out_profit_lock_tests",
    SCRIPTS / "e4_v12_scale_out_profit_lock.py",
)


def report():
    return json.loads(
        (ROOT / "artifacts/e4-v12-scale-out-profit-lock-development.json").read_text(
            encoding="utf-8"
        )
    )


def test_sell_quote_has_adverse_size_impact() -> None:
    small = research.sell_quote(10.0, 30.0, 1_000.0) / 10.0
    large = research.sell_quote(100.0, 30.0, 1_000.0) / 100.0
    assert small > large


def test_scale_out_grid_includes_single_and_two_exit_policies() -> None:
    assert len(research.POLICIES) == 180
    assert any(policy.partial_fraction == 1.0 for policy in research.POLICIES)
    assert any(policy.partial_fraction < 1.0 for policy in research.POLICIES)


def test_explicit_nominal_position_preserves_scale_out_quote() -> None:
    decision_ns = research.HORIZON_MS * 1_000_000
    point = research.base.Point(
        timestamp_ns=decision_ns,
        kind="buy",
        trader="buyer",
        signature="signature",
        slot=1,
        sol_amount=1.0,
        price_sol=1e-8,
        virtual_sol=30.0,
        virtual_tokens=3_000_000_000.0,
        real_tokens=1_000_000_000.0,
        complete=False,
    )
    trace = research.base.Trace(
        run_id="1",
        split="test",
        mint="mint",
        creator="creator",
        create_ns=0,
        create_slot=1,
        create_signature="create",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=0,
        points=[point],
    )
    policy = research.POLICIES[0]
    implicit = research.scale_out_outcome(trace, policy)
    explicit = research.scale_out_outcome(
        trace,
        policy,
        position_sol=(
            research.base.STARTING_BANKROLL_SOL * research.base.POSITION_FRACTION
        ),
    )
    assert implicit == explicit


def test_scale_out_rejects_non_positive_explicit_position() -> None:
    decision_ns = research.HORIZON_MS * 1_000_000
    trace = research.base.Trace(
        run_id="1",
        split="test",
        mint="mint",
        creator="creator",
        create_ns=0,
        create_slot=1,
        create_signature="create",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=0,
        points=[
            research.base.Point(
                timestamp_ns=decision_ns,
                kind="buy",
                trader="buyer",
                signature="signature",
                slot=1,
                sol_amount=1.0,
                price_sol=1e-8,
                virtual_sol=30.0,
                virtual_tokens=3_000_000_000.0,
                real_tokens=1_000_000_000.0,
                complete=False,
            )
        ],
    )
    with pytest.raises(ValueError, match="position_sol must be positive"):
        research.scale_out_outcome(trace, research.POLICIES[0], position_sol=0.0)


def test_retrospective_confirmation_is_not_mislabelled_holdout() -> None:
    result = report()
    assert result["secondary_confirmation_gate_passed"] is False
    assert result["ready_for_strictly_later_evidence"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["identity"]["chronological_split"]["untouched_holdout"] == (
        "strictly later evidence still collecting"
    )


def test_scale_out_charges_an_additional_transaction() -> None:
    result = report()
    fee_model = result["identity"]["fee_model"]
    assert fee_model["additional_scale_out_transaction_charged"] is True
    assert fee_model["priority_fee_sol_per_transaction"] == pytest.approx(0.001)


def test_source_evidence_is_hash_verified_and_production_safe() -> None:
    result = report()
    assert all(row["hash_match"] for row in result["source_audit"])
    assert result["production_paths_changed"] == 0
    assert result["production_promotion_authorised"] is False
