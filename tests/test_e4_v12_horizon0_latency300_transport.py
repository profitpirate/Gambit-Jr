from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "e4_v12_horizon0_latency300_transport_tests",
    ROOT / "scripts/e4_v12_horizon0_latency300_transport.py",
)
assert SPEC and SPEC.loader
research = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(research)


def artifact() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-horizon0-latency300-transport.json").read_text(
            encoding="utf-8"
        )
    )


def test_creator_memory_has_no_creation_time_buyer_history() -> None:
    state = research.actors.ActorState(resolved=4, wins=3, pnl_sol=0.2, severe_losses=1)
    values = research.creator_memory(state)
    assert values[:11] == (0.0,) * 11
    assert values[11] == 4.0
    assert values[12] == 4 / 6
    assert values[13] == 0.05
    assert values[14] == 0.25
    assert values[15] == 1.0


def test_strict_creation_snapshot_excludes_later_equal_timestamp_buy() -> None:
    create = research.base.Point(
        timestamp_ns=100,
        kind="CREATE",
        trader="creator",
        signature="create",
        slot=1,
        sol_amount=0.0,
        price_sol=1.0,
        virtual_sol=30.0,
        virtual_tokens=1_000.0,
        real_tokens=800.0,
        complete=False,
    )
    buy = research.base.Point(
        timestamp_ns=100,
        kind="BUY",
        trader="buyer",
        signature="buy",
        slot=1,
        sol_amount=5.0,
        price_sol=2.0,
        virtual_sol=35.0,
        virtual_tokens=900.0,
        real_tokens=700.0,
        complete=False,
    )
    trace = research.base.Trace(
        run_id="1",
        split="development",
        mint="mint",
        creator="creator",
        create_ns=100,
        create_slot=1,
        create_signature="create",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=True,
        creator_prior_launch_count=0,
        points=[create, buy],
    )
    snapshot = research.strict_creation_snapshot(trace)
    assert snapshot is not None
    assert snapshot[research.base.FEATURE_NAMES.index("public_buy_sol")] == 0.0
    assert snapshot[research.base.FEATURE_NAMES.index("buy_count")] == 0.0
    assert snapshot[research.base.FEATURE_NAMES.index("price_multiple_from_create")] == 1.0


def test_horizon0_transport_is_diagnostic_only_and_causal() -> None:
    report = artifact()
    assert report["identity"]["decision_horizon_ms"] == 0
    assert report["identity"]["latency_ms"] == 300
    assert report["identity"]["resolution_delay_ms"] == 60_300
    assert report["corpus_audit"]["rows"] == 191_425
    assert report["corpus_audit"]["global_creator_counts_use_all_launches"] is True
    assert report["corpus_audit"]["same_timestamp_creator_counts_batched"] is True
    assert report["corpus_audit"]["same_timestamp_market_context_batched"] is True
    assert report["corpus_audit"]["future_values_in_features"] is False
    assert report["candidate_fitted"] is False
    assert report["development_gate_passed"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["active_untouched_live_data_used"] is False
    assert report["production_paths_changed"] == 0
