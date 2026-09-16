from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

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
    "e4_v12_latency300_point_process_transport_tests",
    SCRIPTS / "e4_v12_latency300_point_process_transport.py",
)


def point(timestamp_ms: int, kind: str, trader: str):
    return research.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind=kind,
        trader=trader,
        signature=f"{timestamp_ms}-{kind}-{trader}",
        slot=timestamp_ms,
        sol_amount=1.0,
        price_sol=1.0 + timestamp_ms / 1_000,
        virtual_sol=10.0,
        virtual_tokens=1.0,
        real_tokens=1.0,
        complete=False,
    )


def test_point_process_features_are_causal_and_deterministic() -> None:
    trace = research.base.Trace(
        run_id="1",
        split="development",
        mint="mint",
        creator="creator",
        create_ns=0,
        create_slot=0,
        create_signature="create",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=False,
        creator_prior_launch_count=0,
        points=[
            point(10, "BUY", "a"),
            point(20, "BUY", "a"),
            point(100, "SELL", "b"),
            point(225, "BUY", "c"),
            point(250, "SELL", "b"),
            point(251, "BUY", "future"),
        ],
    )
    first = research.point_process_features(trace)
    second = research.point_process_features(trace)
    assert first == second
    assert first is not None
    assert len(first) == len(research.FEATURE_NAMES) == 17
    assert all(math.isfinite(value) for value in first)
    assert first[4] == 2 / 5
    assert first[8] == 3 / 4
    assert first[9] == 2 / 5
    assert first[10] == 2 / 5
    assert first[12] == 240 / 250


def test_point_process_excludes_creator_and_bot_flow() -> None:
    trace = research.base.Trace(
        run_id="1",
        split="development",
        mint="mint",
        creator="creator",
        create_ns=0,
        create_slot=0,
        create_signature="create",
        mayhem_mode=False,
        cashback_enabled=False,
        metadata_content_addressed=False,
        creator_prior_launch_count=0,
        points=[
            point(10, "BUY", "creator"),
            point(20, "BUY", research.base.choice_sets.E4_WALLET),
            point(30, "BUY", "public"),
        ],
    )
    values = research.point_process_features(trace)
    assert values is not None
    assert values[10] == 0.0
    assert values[11] == 1.0
    assert values[12] == 0.0


def test_result_is_diagnostic_only_when_present() -> None:
    path = ROOT / research.OUTPUT
    if not path.is_file():
        return
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["diagnostic_id"] == (
        f"e4d-{research.base.stable_hash(result['identity'])}"
    )
    assert result["identity"]["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    audit = result["point_process_cache_audit"]
    assert audit["rows"] == 191_425
    assert audit["features"] == len(research.FEATURE_NAMES) == 17
    assert audit["raw_capture_runs"] == 48
    assert audit["raw_capture_bytes"] == 11_789_578_939
    assert audit["raw_source_hashes_verified"] is True
    assert audit["cached_later_rows"] == 48_000
    assert result["selection_audit"]["executed_trades"] == 326
    assert result["selection_audit"]["wins"] == 199
    assert result["global_stable_features"] == [
        "point_log_buy_arrival_cv",
        "point_log_arrival_cv_250ms",
        "point_first_last_span_share",
    ]
    assert result["residual_stable_features"] == []
    assert result["jointly_stable_features"] == []
    assert result["point_process_candidate_warranted"] is False
    assert result["retired_diagnostic_family"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["production_paths_changed"] == 0
    assert result["production_deployment_authorised"] is False
