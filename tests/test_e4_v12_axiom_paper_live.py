from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_axiom_paper_live as subject


def model(maximum_evidence_ns: int = 1_000_000_000) -> dict:
    return {
        "schema_version": "test",
        "maximum_evidence_ns": maximum_evidence_ns,
        "selector": {
            "minimum_prior_e4_attempts": 1,
            "minimum_creator_seed_sol": 2.0,
            "minimum_tweet_age_seconds": 0.0,
            "maximum_tweet_age_seconds": 10.0,
            "entry_latency_ms": 5.0,
            "maximum_create_to_entry_price_multiple": 1.5,
            "minimum_entry_output_ratio": 0.65,
        },
        "position_sizing": {
            "starting_bankroll_sol": 3.0,
            "fraction_of_available_cash": 0.10,
            "maximum_concurrent_positions": 2,
        },
        "exit_policy": {
            "stop": 0.70,
            "first_take": 1.15,
            "first_fraction": 0.30,
            "hold_ms": 2_000,
            "trail_retrace": 0.25,
        },
        "execution_costs": {
            "axiom_net_fee_bps_each_side": 95.0,
            "pump_bonding_curve_fee_bps_each_side": 125.0,
            "solana_base_fee_sol_per_transaction": 0.000005,
            "priority_fee_sol_per_transaction": 0.001,
            "mev_bribe_sol_on_buy": 0.001,
            "mev_bribe_sol_on_sell": 0.0,
            "axiom_tier": "Wood",
        },
        "acceptance_gate": {
            "closed_trades": 50,
            "minimum_win_rate": 0.65,
            "minimum_wilson_lower_bound": 0.55,
            "minimum_profit_factor": 1.25,
            "minimum_net_pnl_sol": 0.0,
            "maximum_drawdown_fraction": 0.20,
            "minimum_capture_windows": 1,
        },
        "creator_handles": {"creator": ["known"]},
        "creator_prior_e4_attempts": {"creator": 1},
    }


def event(
    *,
    event_id: int,
    kind: str,
    mint: str,
    received_ns: int,
    signature: str,
    price: float,
    virtual_sol: int,
    virtual_tokens: int,
    trader: str,
    uri: str,
) -> dict:
    raw = {
        "virtual_sol_reserves": virtual_sol,
        "virtual_token_reserves": virtual_tokens,
        "real_token_reserves": 793_100_000_000_000,
    }
    if kind == "CREATE":
        raw.update(
            {
                "creator": "creator",
                "uri": uri,
                "is_mayhem_mode": False,
            }
        )
    return {
        "event_id": event_id,
        "event_index": event_id,
        "kind": kind,
        "mint": mint,
        "received_ns": received_ns,
        "signature": signature,
        "slot": event_id,
        "trader": trader,
        "creator": "creator" if kind == "CREATE" else None,
        "sol_amount": 2.0 if kind == "BUY" and trader == "creator" else 0.1,
        "token_amount": 1_000_000.0,
        "price_sol": price,
        "raw": raw,
    }


def write_pair(tmp_path: Path, count: int = 51) -> tuple[str, Path]:
    batch = tmp_path / "batch.json"
    events = tmp_path / "events.jsonl"
    cache = tmp_path / "cache.json"
    batch.write_text(json.dumps({"capture": {"cohort": []}}), encoding="utf-8")
    rows = []
    metadata = {}
    for index in range(count):
        mint = f"mint-{index}"
        signature = f"sig-{index}"
        create_ns = 2_000_000_000 + index * 3_000_000_000
        uri = f"https://metadata/{mint}"
        status_ms = create_ns // 1_000_000 - 1_000
        status_id = str((status_ms - subject.social.TWITTER_EPOCH_MS) << 22)
        metadata[uri] = {
            "error": "",
            "twitter": f"https://x.com/known/status/{status_id}",
            "handle": "known",
            "status_id": status_id,
        }
        rows.extend(
            [
                event(
                    event_id=index * 4,
                    kind="CREATE",
                    mint=mint,
                    received_ns=create_ns,
                    signature=signature,
                    price=3e-8,
                    virtual_sol=30_000_000_000,
                    virtual_tokens=1_073_000_000_000_000,
                    trader="creator",
                    uri=uri,
                ),
                event(
                    event_id=index * 4 + 1,
                    kind="BUY",
                    mint=mint,
                    received_ns=create_ns,
                    signature=signature,
                    price=3e-8,
                    virtual_sol=32_000_000_000,
                    virtual_tokens=1_006_000_000_000_000,
                    trader="creator",
                    uri=uri,
                ),
                event(
                    event_id=index * 4 + 2,
                    kind="BUY",
                    mint=mint,
                    received_ns=create_ns + 100_000_000,
                    signature=f"public-{index}",
                    price=6e-8,
                    virtual_sol=60_000_000_000,
                    virtual_tokens=500_000_000_000_000,
                    trader="public",
                    uri=uri,
                ),
                event(
                    event_id=index * 4 + 3,
                    kind="SELL",
                    mint=mint,
                    received_ns=create_ns + 300_000_000,
                    signature=f"sell-{index}",
                    price=5e-8,
                    virtual_sol=50_000_000_000,
                    virtual_tokens=600_000_000_000_000,
                    trader="public",
                    uri=uri,
                ),
            ]
        )
    events.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    cache.write_text(json.dumps(metadata), encoding="utf-8")
    return f"run-1={batch}::{events}", cache


def test_cost_model_includes_axiom_pump_priority_bribe_and_base() -> None:
    costs = subject.load_costs(model())
    assert costs.variable_rate == 0.022
    assert costs.buy_fixed == 0.002005
    assert costs.sell_fixed == 0.001005
    assert subject.curve_input_for_budget(0.3, costs) < 0.3


def test_frozen_model_change_is_rejected(tmp_path: Path) -> None:
    original = model()
    state = subject.empty_state(original, subject.stable_hash(original))
    changed = model()
    changed["selector"]["minimum_creator_seed_sol"] = 1.0
    model_path = tmp_path / "model.json"
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.md"
    model_path.write_text(json.dumps(changed), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pair, cache = write_pair(tmp_path, 1)
    args = Namespace(
        pair=[pair],
        model=model_path,
        state=state_path,
        report=report_path,
        metadata_cache=[cache],
    )
    try:
        import asyncio

        asyncio.run(subject.run(args))
    except ValueError as exc:
        assert "fingerprint changed" in str(exc)
    else:
        raise AssertionError("changed model was accepted")


def test_forward_ledger_stops_at_first_50_closed_trades(tmp_path: Path) -> None:
    frozen = model()
    model_path = tmp_path / "model.json"
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.md"
    model_path.write_text(json.dumps(frozen), encoding="utf-8")
    pair, cache = write_pair(tmp_path)
    args = Namespace(
        pair=[pair],
        model=model_path,
        state=state_path,
        report=report_path,
        metadata_cache=[cache],
    )
    import asyncio

    result = asyncio.run(subject.run(args))
    assert result["completion"] == {
        "required_closed_trades": 50,
        "reached": True,
        "remaining": 0,
    }
    assert len(result["ledger"]) == 50
    assert result["audit_counts"]["post_completion_candidates_ignored"] == 1
    assert result["metrics"]["total_axiom_fees_sol"] > 0
    assert result["metrics"]["total_pump_fees_sol"] > 0
    assert result["production_paths_changed"] == 0
    assert "first 50 chronological" in report_path.read_text(encoding="utf-8")


def test_nonfuture_capture_is_rejected(tmp_path: Path) -> None:
    frozen = model(maximum_evidence_ns=10**30)
    model_path = tmp_path / "model.json"
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.md"
    model_path.write_text(json.dumps(frozen), encoding="utf-8")
    pair, cache = write_pair(tmp_path, 1)
    args = Namespace(
        pair=[pair],
        model=model_path,
        state=state_path,
        report=report_path,
        metadata_cache=[cache],
    )
    import asyncio

    try:
        asyncio.run(subject.run(args))
    except ValueError as exc:
        assert "strictly after" in str(exc)
    else:
        raise AssertionError("nonfuture capture was accepted")


def test_quiet_curve_exit_is_timestamped_at_deadline_not_before_fill() -> None:
    frozen = model()
    run = subject.replay.RunData(
        run_id="quiet-run",
        batch={},
        events_by_mint={"quiet-mint": []},
        reserves_by_mint={
            "quiet-mint": [
                subject.replay.ReserveState(
                    received_ns=100,
                    sequence=0,
                    virtual_sol=30.0,
                    virtual_tokens=1_000_000_000.0,
                    real_tokens=800_000_000.0,
                    price_sol=3e-8,
                    fdv_usd=0.0,
                )
            ]
        },
        e4_positions={},
    )
    trace = subject.base.Trace(
        run_id="quiet-run",
        split="live",
        mint="quiet-mint",
        creator="creator",
        create_ns=100,
        create_slot=1,
        create_signature="sig",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=1,
        points=[
            subject.base.Point(
                timestamp_ns=100,
                kind="BUY",
                trader="creator",
                signature="sig",
                slot=1,
                sol_amount=2.0,
                price_sol=3e-8,
                virtual_sol=30.0,
                virtual_tokens=1_000_000_000.0,
                real_tokens=800_000_000.0,
                complete=False,
            )
        ],
    )
    candidate = subject.Candidate(
        run_id="quiet-run",
        mint="quiet-mint",
        creator="creator",
        handle="known",
        status_id="1",
        tweet_age_seconds=1.0,
        create_ns=100,
        decision_ns=100,
        decision_sequence=0,
        creator_seed_sol=2.0,
    )
    trade, rejection = subject.simulate_trade(
        run,
        trace,
        candidate,
        0.3,
        frozen,
        subject.load_costs(frozen),
        subject.load_policy(frozen),
    )
    assert rejection is None
    assert trade is not None
    assert trade["exit_ns"] == trade["fill_ns"] + frozen["exit_policy"]["hold_ms"] * 1_000_000
    assert trade["exit_ns"] >= trade["fill_ns"]
