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
    "e4_v12_latency300_coordination_transport_tests",
    SCRIPTS / "e4_v12_latency300_coordination_transport.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-coordination-transport.json").read_text(
            encoding="utf-8"
        )
    )


def point(timestamp_ms: int, slot: int, trader: str, signature: str):
    return research.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind="BUY",
        trader=trader,
        signature=signature,
        slot=slot,
        sol_amount=1.0,
        price_sol=1.0 + timestamp_ms / 1000,
        virtual_sol=10.0,
        virtual_tokens=1.0,
        real_tokens=1.0,
        complete=False,
    )


def test_coordination_features_use_only_predecision_public_trades() -> None:
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
            point(10, 1, "a", "s1"),
            point(20, 1, "b", "s2"),
            point(40, 2, "a", "s3"),
            point(70, 4, "c", "s4"),
            point(251, 5, "future", "s5"),
        ],
    )
    values = research.coordination_features(trace)
    assert values is not None
    assert len(values) == len(research.COORDINATION_FEATURE_NAMES) == 16
    assert values[:3] == (3.0, 4.0, 3.0)
    assert values[3] == 0.5
    assert values[4] == 0.375
    expected_entropy = -sum(share * math.log(share) for share in (0.5, 0.25, 0.25))
    assert values[5] == expected_entropy / math.log(3)
    assert values[6] == 0.5
    assert values[7] == 1 / 3
    assert values[8] == 1.0
    assert values[9] == 2.0
    assert values[10] == 10.0
    assert values[11] == 70.0
    assert values[15] == 1 / 3


def test_report_binds_full_matrix_exact_labels_and_no_live_data() -> None:
    result = report()
    identity = result["identity"]
    audit = result["coordination_cache_audit"]
    assert result["diagnostic_id"] == f"e4d-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["latency_ms"] == 300
    assert identity["causal_horizon_ms"] == 250
    assert identity["coordination_matrix_sha256"] == audit["matrix_sha256"]
    assert audit["rows"] == 191_425
    assert audit["features"] == 16
    assert audit["raw_capture_runs"] == 48
    assert audit["raw_capture_bytes"] == 11_789_578_939
    assert audit["raw_source_hashes_verified"] is True
    assert audit["cached_later_rows"] == 48_000
    assert audit["future_values_in_features"] is False
    assert result["active_untouched_live_data_used"] is False


def test_diagnostic_never_fits_or_authorises_a_candidate() -> None:
    result = report()
    assert result["all_epoch_auc_52_feature_count"] == 1
    assert result["candidate_warranted"] is False
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
