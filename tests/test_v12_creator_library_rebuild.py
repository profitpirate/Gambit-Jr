from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_creator_library_rebuild as subject


def e4(creator: str, wins: int, losses: int, pnl: float) -> dict:
    return {
        "creator": creator,
        "wins": wins,
        "losses": losses,
        "trades": wins + losses,
        "gross_win_rate": wins / max(wins + losses, 1),
        "winning_pnl_sol": pnl,
        "winner_mints": [f"{creator}-w{i}" for i in range(wins)],
        "loser_mints": [f"{creator}-l{i}" for i in range(losses)],
    }


def fresh(
    creator: str,
    *,
    fills: int,
    wins: int,
    losses: int,
    net: float,
    scout_wins: int,
    scout_net: float,
    runners: int,
    promotion: bool = False,
) -> dict:
    return {
        "creator": creator,
        "status": "PROMOTION_CANDIDATE" if promotion else "CONFIRMED_REPEAT_WINNER",
        "promotion_ready": promotion,
        "launches_seen": max(fills, 3),
        "runner_2x_count": runners,
        "runner_3x_count": 0,
        "scout_fills": max(fills, scout_wins),
        "scout_wins": scout_wins,
        "scout_losses": max(0, fills - scout_wins),
        "scout_net_pnl_sol": scout_net,
        "v12_compatible_fills": fills,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / max(fills, 1),
        "net_pnl_sol": net,
        "profit_factor": 3.0,
        "distinct_winning_mints": [f"{creator}-vw{i}" for i in range(wins)],
        "distinct_scout_winning_mints": [f"{creator}-sw{i}" for i in range(scout_wins)],
    }


def test_all_e4_repeat_winners_are_promoted() -> None:
    expectancy = {
        "top_creators": [
            e4("pure", 2, 0, 1.0),
            e4("mixed", 2, 2, 1.0),
            e4("single", 1, 0, 1.0),
        ]
    }
    apprentice = {"creators": {}, "counts": {"unknown_launch_observations": 0}, "observations": []}
    result = subject.build(expectancy, apprentice)
    promoted = {row["creator"] for row in result["promoted"]}
    assert promoted == {"pure", "mixed"}
    assert "single" not in promoted


def test_fresh_promotion_is_added_and_never_auto_buys() -> None:
    expectancy = {"top_creators": []}
    apprentice = {
        "creators": {
            "fresh": fresh(
                "fresh",
                fills=3,
                wins=2,
                losses=1,
                net=0.2,
                scout_wins=2,
                scout_net=0.2,
                runners=2,
                promotion=True,
            )
        },
        "counts": {"unknown_launch_observations": 3},
        "observations": [],
    }
    result = subject.build(expectancy, apprentice)
    assert result["promoted"][0]["creator"] == "fresh"
    assert result["promoted"][0]["auto_buy"] is False
    assert result["promoted"][0]["selection_authority"] == "RECOGNISED_CREATOR_ONLY"


def test_shortlist_is_research_only() -> None:
    expectancy = {"top_creators": []}
    apprentice = {
        "creators": {
            "candidate": fresh(
                "candidate",
                fills=4,
                wins=2,
                losses=2,
                net=0.16,
                scout_wins=3,
                scout_net=0.24,
                runners=3,
            )
        },
        "counts": {"unknown_launch_observations": 3},
        "observations": [],
    }
    result = subject.build(expectancy, apprentice)
    assert not result["promoted"]
    assert result["shortlisted"]
    assert result["shortlisted"][0]["selection_authority"] == "RESEARCH_ONLY"


def test_runner_gap_distinguishes_outcome_from_profitable_entry() -> None:
    apprentice = {
        "creators": {
            "a": {"status": "SHORTLISTED"},
            "b": {"status": "RUNNER_OBSERVED"},
        },
        "observations": [
            {
                "creator": "a",
                "runner": {"runner_2x_60s": True},
                "scout": {"status": "FILLED", "pnl_sol": 0.1},
            },
            {
                "creator": "b",
                "runner": {"runner_2x_60s": True},
                "scout": {"status": "FILLED", "pnl_sol": -0.1},
            },
        ],
    }
    result = subject.runner_gap(apprentice)
    assert result["runner_2x_coin_observations"] == 2
    assert result["unique_runner_2x_creators"] == 2
    assert result["runner_creators_not_shortlisted"] == 1
    assert result["reason_counts_by_creator"]["no_profitable_scout_fill"] == 1


def test_complete_history_rejects_low_confidence_creator_identity() -> None:
    expectancy = {"top_creators": []}
    complete = {
        "completeness": {"union_trades": 450},
        "creators": [
            {
                "creator": "uncertain",
                "wins": 5,
                "losses": 0,
                "trades": 5,
                "winning_pnl_sol": 2.0,
                "minimum_resolution_confidence": 0.50,
                "winner_mints": ["a", "b", "c", "d", "e"],
                "loser_mints": [],
            },
            {
                "creator": "certain",
                "wins": 2,
                "losses": 0,
                "trades": 2,
                "winning_pnl_sol": 0.5,
                "minimum_resolution_confidence": 0.95,
                "winner_mints": ["f", "g"],
                "loser_mints": [],
            },
        ],
    }
    apprentice = {
        "creators": {},
        "counts": {"unknown_launch_observations": 0},
        "observations": [],
    }
    result = subject.build(expectancy, apprentice, complete)
    promoted = {row["creator"] for row in result["promoted"]}
    assert "uncertain" not in promoted
    assert "certain" in promoted


def test_certified_lifetime_ledger_is_authoritative_over_provisional_union() -> None:
    expectancy = {"top_creators": [e4("legacy", 5, 0, 5.0)]}
    complete = {
        "completeness": {"union_trades": 450},
        "creators": [e4("provisional", 4, 0, 4.0)],
    }
    lifetime = {
        "status": "CERTIFIED_COMPLETE",
        "completeness": {"ready_for_nextgen_training": True},
        "counts": {"closed_trade_records": 2345},
        "creators": [e4("lifetime", 3, 0, 3.0)],
    }
    apprentice = {
        "creators": {},
        "counts": {"unknown_launch_observations": 0},
        "observations": [],
    }
    result = subject.build(expectancy, apprentice, complete, lifetime)
    assert result["history_source"] == "E4_LIFETIME_LEDGER_CERTIFIED"
    assert result["activation"]["history_certified"] is True
    assert result["activation"]["lifetime_closed_trades"] == 2345
    assert {row["creator"] for row in result["promoted"]} == {"lifetime"}


def test_uncertified_lifetime_ledger_keeps_library_provisional_and_disabled() -> None:
    expectancy = {"top_creators": []}
    complete = {
        "completeness": {"union_trades": 450},
        "creators": [e4("provisional", 2, 0, 1.0)],
    }
    lifetime = {
        "status": "COLLECTING",
        "completeness": {"ready_for_nextgen_training": False},
        "counts": {"closed_trade_records": 900},
        "creators": [e4("should-not-be-authority", 9, 0, 9.0)],
    }
    apprentice = {
        "creators": {},
        "counts": {"unknown_launch_observations": 0},
        "observations": [],
    }
    result = subject.build(expectancy, apprentice, complete, lifetime)
    assert result["history_source"] == "PROVISIONAL_COMPLETE_ONCHAIN_UNION"
    assert result["activation"]["history_certified"] is False
    assert result["activation"]["enabled_now"] is False
    assert {row["creator"] for row in result["promoted"]} == {"provisional"}
