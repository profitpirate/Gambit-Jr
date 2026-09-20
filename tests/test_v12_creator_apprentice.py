from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e4_v12_true_latency_replay as replay
import v12_creator_apprentice as subject


def observation(
    mint: str,
    pnl: float | None,
    *,
    creator: str = "creator-x",
    create_ns: int = 100,
    runner_multiple: float = 1.0,
    status: str = "FILLED",
) -> dict:
    return {
        "run_id": f"run-{mint}",
        "mint": mint,
        "creator": creator,
        "create_ns": create_ns,
        "runner": {
            "runner_2x_60s": runner_multiple >= 2.0,
            "runner_3x_60s": runner_multiple >= 3.0,
            "max_multiple_60s": runner_multiple,
        },
        "v12_compatible": {
            "status": status,
            "pnl_sol": pnl,
            "win": bool(pnl is not None and pnl > 0),
        },
        "scout": {
            "status": "FILLED",
            "pnl_sol": pnl,
            "win": bool(pnl is not None and pnl > 0),
        },
    }


def test_first_profitable_unknown_creator_is_shortlisted() -> None:
    result = subject.creator_summary("creator-x", [observation("a", 0.04)])
    assert result["status"] == "SHORTLISTED"
    assert result["wins"] == 1
    assert result["promotion_ready"] is False


def test_second_distinct_winner_confirms_repeat_creator() -> None:
    rows = [
        observation("a", 0.04, create_ns=100),
        observation("b", 0.03, create_ns=200),
    ]
    result = subject.creator_summary("creator-x", rows)
    assert result["status"] == "CONFIRMED_REPEAT_WINNER"
    assert result["distinct_winning_mints"] == ["a", "b"]
    assert result["second_confirmation_mint"] == "b"
    assert result["promotion_ready"] is False


def test_promotion_requires_repeat_wins_and_minimum_sample_quality() -> None:
    rows = [
        observation("a", 0.05, create_ns=100),
        observation("b", 0.04, create_ns=200),
        observation("c", -0.01, create_ns=300),
    ]
    result = subject.creator_summary("creator-x", rows)
    assert result["status"] == "PROMOTION_CANDIDATE"
    assert result["wins"] == 2
    assert result["losses"] == 1
    assert result["win_rate"] >= 2 / 3
    assert result["profit_factor"] >= 1.5
    assert result["promotion_ready"] is True
    assert result["automatic_whitelist_mutation"] is False



def test_profitable_scout_shortlists_even_when_v12_ineligible() -> None:
    row = observation(
        "scout-only",
        0.08,
        status="INELIGIBLE_CREATOR_SEED",
    )
    result = subject.creator_summary("creator-x", [row])
    assert result["status"] == "SHORTLISTED"
    assert result["scout_wins"] == 1
    assert result["v12_compatible_fills"] == 0
    assert result["promotion_ready"] is False


def test_repeat_scout_winners_confirm_but_do_not_bypass_v12_promotion_gate() -> None:
    rows = [
        observation("a", 0.08, create_ns=100, status="INELIGIBLE_CREATOR_SEED"),
        observation("b", 0.06, create_ns=200, status="INELIGIBLE_CREATOR_SEED"),
    ]
    result = subject.creator_summary("creator-x", rows)
    assert result["status"] == "CONFIRMED_REPEAT_WINNER"
    assert result["distinct_scout_winning_mints"] == ["a", "b"]
    assert result["v12_compatible_fills"] == 0
    assert result["promotion_ready"] is False

def test_runner_is_recorded_even_when_not_v12_compatible() -> None:
    row = observation(
        "runner",
        None,
        runner_multiple=3.5,
        status="INELIGIBLE_CREATOR_SEED",
    )
    result = subject.creator_summary("creator-x", [row])
    assert result["status"] == "RUNNER_OBSERVED"
    assert result["runner_2x_count"] == 1
    assert result["runner_3x_count"] == 1
    assert result["v12_compatible_fills"] == 0


def test_rebuild_never_creates_automatic_whitelist_mutation() -> None:
    state = {
        "observations": [
            observation("a", 0.05, create_ns=100),
            observation("b", 0.04, create_ns=200),
            observation("c", -0.01, create_ns=300),
        ]
    }
    subject.rebuild_creator_index(state)
    assert state["promotion_candidates"]
    assert all(
        row["automatic_whitelist_mutation"] is False
        for row in state["promotion_candidates"]
    )


def test_runner_profile_uses_causal_horizons() -> None:
    states = [
        replay.ReserveState(100, 0, 10.0, 1000.0, 1000.0, 0.01, 0.0),
        replay.ReserveState(1_000_000_100, 1, 20.0, 1000.0, 1000.0, 0.02, 0.0),
        replay.ReserveState(5_000_000_100, 2, 30.0, 1000.0, 1000.0, 0.03, 0.0),
        replay.ReserveState(20_000_000_100, 3, 40.0, 1000.0, 1000.0, 0.04, 0.0),
    ]
    run = replay.RunData("r", {}, {}, {"mint": states}, {})
    result = subject.runner_profile(run, "mint", 100)
    assert math.isclose(result["max_multiple_2s"], 2.0)
    assert math.isclose(result["max_multiple_10s"], 3.0)
    assert math.isclose(result["max_multiple_60s"], 4.0)
    assert result["runner_2x_60s"] is True
    assert result["runner_3x_60s"] is True
