from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_lifetime_ledger as subject


def build(documents, **kwargs):
    defaults = {
        "minimum_expected_closed_trades": 1,
        "artifact_inventory_complete": True,
        "wallet_identity_inventory_complete": True,
        "source_reconciliation_complete": True,
    }
    defaults.update(kwargs)
    return subject.build(documents, **defaults)


def test_gambit_positions_are_ignored_and_real_e4_rows_are_deduped() -> None:
    forward = {
        "gambit_positions": {
            "fake": {"mint": "fake", "entry_time": 100, "pnl_sol": 99}
        },
        "same_window_e4_positions": {
            "real": {
                "mint": "mint-a",
                "entry_time": 100,
                "exit_time": 102,
                "pnl_sol": 0.5,
            }
        },
    }
    recent = {
        "e4_positions": [
            {
                "mint": "mint-a",
                "entry_time": 102,
                "exit_time": 104,
                "entry_signature": "",
                "pnl_sol": 0.5,
            }
        ]
    }
    result = build([
        ("models/e4/e4-v12-forward-evidence.json", forward, "COMMITTED_REPO"),
        ("research/v12-e4-48h-comparison.json", recent, "COMMITTED_REPO"),
    ])
    assert result["status"] == "CERTIFIED_COMPLETE"
    assert result["counts"]["closed_trade_records"] == 1
    assert result["trades"][0]["mint"] == "mint-a"
    assert all(row["mint"] != "fake" for row in result["trades"])
    assert result["source_manifest"][0]["contains_gambit_positions_ignored"] is True


def test_untimed_legacy_creator_label_enriches_timed_trade_not_double_counts() -> None:
    complete = {
        "trades": [{
            "mint": "mint-a",
            "creator": "creator-a",
            "outcome": "WIN",
            "source": "LEGACY_E4_CORPUS",
        }]
    }
    timed = {
        "e4_positions": [{
            "mint": "mint-a",
            "entry_time": 100,
            "exit_time": 103,
            "pnl_sol": 0.25,
        }]
    }
    result = build([
        ("models/e4/e4-complete-creator-history.json", complete, "COMMITTED_REPO"),
        ("research/v12-e4-48h-comparison.json", timed, "COMMITTED_REPO"),
    ])
    assert result["counts"]["closed_trade_records"] == 1
    assert result["trades"][0]["creator"] == "creator-a"
    assert result["creators"][0]["wins"] == 1


def test_lifetime_certification_fails_closed_when_source_inventory_is_unknown() -> None:
    observed = {
        "wallet": subject.E4_WALLET,
        "evidence": {"closed_memecoin_positions": 221, "swaps": 999},
    }
    result = build(
        [("models/e4/e4-observed-v1.json", observed, "COMMITTED_REPO")],
        minimum_expected_closed_trades=1000,
        artifact_inventory_complete=False,
        wallet_identity_inventory_complete=False,
        source_reconciliation_complete=False,
    )
    assert result["status"] == "COLLECTING"
    assert result["completeness"]["ready_for_nextgen_training"] is False
    assert "historical_actions_artifact_inventory_not_certified_complete" in result["completeness"]["blockers"]
    assert result["aggregate_claims"][0]["closed_positions"] == 221
    assert result["counts"]["closed_trade_records"] == 0


def test_failed_attempts_are_preserved_but_not_counted_as_closed_trades() -> None:
    backfill = {
        "outcomes": [
            {
                "event_identity": "wallet:sig-1",
                "selection_type": "FAILED_ATTEMPT",
                "wallet_signature": "sig-1",
                "outcome": "UNRESOLVED_MINT",
                "timestamp": 100,
            },
            {
                "event_identity": "wallet:sig-2",
                "selection_type": "SUCCESSFUL_BUY",
                "wallet_signature": "sig-2",
                "mint": "mint-b",
                "outcome": "RECOVERED_IN_V2",
                "timestamp": 101,
            },
        ]
    }
    result = build(
        [("artifacts/e4-v12-selection-backfill.json", backfill, "COMMITTED_REPO")],
        minimum_expected_closed_trades=1,
    )
    assert result["counts"]["closed_trade_records"] == 0
    assert result["counts"]["attempt_records"] == 2
    assert result["counts"]["failed_attempts"] == 1
    assert result["counts"]["successful_buy_attempts"] == 1
