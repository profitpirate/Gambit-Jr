from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_axiom_paper_live as paper
import e4_v12_profit_survival_search as base
import v12_prearmed_shadow_suite as subject


def frozen_model() -> dict:
    return {
        "schema_version": "test-100",
        "maximum_evidence_ns": 1,
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
        },
        "acceptance_gate": {
            "closed_trades": 100,
            "minimum_win_rate": 0.65,
            "minimum_wilson_lower_bound": 0.55,
            "minimum_profit_factor": 1.25,
            "minimum_net_pnl_sol": 0.0,
            "maximum_drawdown_fraction": 0.20,
            "minimum_capture_windows": 5,
        },
        "creator_handles": {"creator-a": ["alpha"]},
        "creator_prior_e4_attempts": {"creator-a": 1},
    }


def trade(
    mint: str,
    creator: str,
    pnl: float,
    *,
    entry: float = 0.3,
    output_ratio: float = 0.8,
    chase: float = 1.2,
    age: float = 2.0,
    seed: float = 3.0,
) -> dict:
    return {
        "run_id": "1",
        "mint": mint,
        "creator": creator,
        "pnl_sol": pnl,
        "entry_cost_sol": entry,
        "proceeds_sol": entry + pnl,
        "decision_ns": 100,
        "fill_ns": 105,
        "exit_ns": 1_000,
        "output_ratio": output_ratio,
        "create_to_fill_price_multiple": chase,
        "tweet_age_seconds": age,
        "creator_seed_sol": seed,
    }


def test_creator_concentration_leave_outs() -> None:
    rows = [
        trade("a1", "a", 0.2),
        trade("a2", "a", 0.1),
        trade("b1", "b", -0.05),
        trade("c1", "c", 0.03),
    ]
    result = subject.creator_concentration(rows)
    assert result["creator_count"] == 3
    assert result["top_creator_trade_share"] == 0.5
    assert result["without_top_creator"]["trades"] == 2
    assert result["without_top3_creators"]["trades"] == 0
    assert result["effective_creator_count"] > 2


def test_drift_detects_large_win_rate_drop() -> None:
    baseline = [trade(f"b{i}", "base", 0.05 if i < 7 else -0.02) for i in range(10)]
    current = [trade(f"c{i}", "new", 0.05 if i < 8 else -0.02) for i in range(20)]
    for index, row in enumerate(current):
        row["pnl_sol"] = 0.05 if index % 2 == 0 else -0.02
    result = subject.drift_telemetry(current, baseline)
    assert "WIN_RATE_DOWN_GT_15PP" in result["flags"]
    assert result["creator_js_divergence_bits"] > 0


def test_risk_simulation_is_deterministic() -> None:
    rows = [
        trade(f"m{i}", "a", 0.04 if i % 3 else -0.02)
        for i in range(15)
    ]
    first = subject.risk_of_ruin(
        rows,
        starting_bankroll=3.0,
        position_fraction=0.10,
        model_hash="abc",
        paths=300,
        horizon=40,
    )
    second = subject.risk_of_ruin(
        rows,
        starting_bankroll=3.0,
        position_fraction=0.10,
        model_hash="abc",
        paths=300,
        horizon=40,
    )
    assert first == second
    assert first["status"] == "SIMULATED"
    assert 0 <= first["probability_drawdown_ge_20pct"] <= 1


def test_new_creator_score_rewards_reputation_and_seed_similarity() -> None:
    stats = {"alpha": {"win_rate": 0.8, "expectancy_sol": 0.05}}
    high, high_components = subject.new_creator_score(
        creator_seed_sol=3.0,
        tweet_age_seconds=1.0,
        handle="alpha",
        handle_stats=stats,
        seed_reference=(3.0, 0.5),
        metadata_quality=1.0,
    )
    low, _ = subject.new_creator_score(
        creator_seed_sol=2.0,
        tweet_age_seconds=9.5,
        handle="unknown",
        handle_stats=stats,
        seed_reference=(3.0, 0.5),
        metadata_quality=1.0,
    )
    assert high > low
    assert high_components["handle_reputation"] > 0.5


def test_hard_negative_score_penalises_bad_execution() -> None:
    safe = trade(
        "safe",
        "a",
        0.01,
        output_ratio=0.95,
        chase=1.05,
        age=1.0,
        seed=5.0,
    )
    risky = trade(
        "risky",
        "a",
        -0.01,
        output_ratio=0.66,
        chase=1.49,
        age=9.5,
        seed=2.05,
    )
    regime = {"relative_launch_pressure": 1.8}
    safe_score, _ = subject.hard_negative_score(safe, regime)
    risky_score, _ = subject.hard_negative_score(risky, regime)
    assert risky_score > safe_score
    assert risky_score >= 0.70


def test_absorption_snapshot_distinguishes_buy_support() -> None:
    supported = base.Trace(
        run_id="1",
        split="live",
        mint="mint",
        creator="creator",
        create_ns=0,
        create_slot=1,
        create_signature="sig",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=1,
        points=[
            base.Point(110, "SELL", "s1", "x", 1, 0.2, 0.90, 30, 100, 100, False),
            base.Point(130, "BUY", "b1", "y", 1, 0.5, 0.98, 31, 99, 99, False),
            base.Point(150, "BUY", "b2", "z", 1, 0.5, 1.02, 32, 98, 98, False),
        ],
    )
    weak = base.Trace(
        run_id="1",
        split="live",
        mint="mint2",
        creator="creator",
        create_ns=0,
        create_slot=1,
        create_signature="sig2",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=1,
        points=[
            base.Point(110, "SELL", "s1", "x", 1, 1.0, 0.90, 29, 101, 101, False),
            base.Point(150, "SELL", "s2", "y", 1, 1.0, 0.80, 28, 102, 102, False),
        ],
    )
    a = subject._absorption_snapshot(
        supported, fill_ns=100, horizon_ms=1, fill_price=1.0
    )
    b = subject._absorption_snapshot(
        weak, fill_ns=100, horizon_ms=1, fill_price=1.0
    )
    assert a["score"] > b["score"]


def test_integrity_uses_100_trade_target_and_model_hash() -> None:
    model = frozen_model()
    state = {
        "model_sha256": paper.stable_hash(model),
        "ledger": [trade("m", "a", 0.01)],
        "processed_runs": ["1"],
    }
    result = subject.validate_integrity(state, model, "1")
    assert result["status"] == "PASS"
    assert result["required_closed_trades"] == 100


def test_integrity_rejects_duplicate_trade_identity() -> None:
    model = frozen_model()
    row = trade("m", "a", 0.01)
    state = {
        "model_sha256": paper.stable_hash(model),
        "ledger": [row, dict(row)],
        "processed_runs": ["1"],
    }
    result = subject.validate_integrity(state, model, "1")
    assert result["status"] == "FAIL"
    assert any("DUPLICATE_RUN_MINT_KEYS" in value for value in result["errors"])
