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
    "e4_v12_latency300_microstructure_transport_tests",
    SCRIPTS / "e4_v12_latency300_microstructure_transport.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-microstructure-transport.json"
        ).read_text(encoding="utf-8")
    )


def point(
    timestamp_ms: int, kind: str, trader: str, sol: float, price: float, reserve: float
):
    return research.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind=kind,
        trader=trader,
        signature=f"{timestamp_ms}-{kind}-{trader}",
        slot=timestamp_ms,
        sol_amount=sol,
        price_sol=price,
        virtual_sol=reserve,
        virtual_tokens=1.0,
        real_tokens=1.0,
        complete=False,
    )


def test_microstructure_features_use_only_predecision_public_flow() -> None:
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
            point(100, "BUY", "a", 1.0, 1.0, 10.0),
            point(200, "BUY", "b", 2.0, 1.1, 12.0),
            point(225, "SELL", "c", 1.0, 1.05, 11.0),
            point(250, "BUY", "a", 1.0, 1.2, 13.0),
            point(251, "SELL", "future", 100.0, 0.1, 1.0),
        ],
    )
    values = research.microstructure_features(trace)
    assert values is not None
    assert len(values) == len(research.MICRO_FEATURE_NAMES) == 20
    assert values[0:5] == (4.0, 3.0, 3.0, 2.0, 1.0)
    assert values[5] == 3.0
    assert values[6] == 2.0
    assert values[7] == 2.0
    assert values[8] == 0.6
    assert values[9] == 0.5
    assert values[10] == math.log(1.2)
    assert values[11] == math.log(1.2)
    assert values[12] == math.log(1.2 / 1.1)
    assert values[15] == 25.0
    assert values[16] == 25.0
    assert values[17] == 0.0
    assert values[18] == 3.0
    assert values[19] == math.log1p(13.0 / 4.0)


def test_identity_binds_full_microstructure_matrix_and_exact_labels() -> None:
    result = report()
    identity = result["identity"]
    audit = result["microstructure_cache_audit"]
    assert result["diagnostic_id"] == (
        f"e4d-{research.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == (
        research.source_code_fingerprint(ROOT)
    )
    assert identity["latency_ms"] == 300
    assert identity["causal_horizon_ms"] == 250
    assert identity["microstructure_matrix_sha256"] == audit["matrix_sha256"]
    assert audit["rows"] == 191_425
    assert audit["features"] == 20
    assert audit["raw_capture_runs"] == 48
    assert audit["raw_capture_bytes"] == 11_789_578_939
    assert audit["raw_source_hashes_verified"] is True
    assert audit["cached_later_rows"] == 48_000


def test_new_feature_transport_findings_are_preserved() -> None:
    result = report()
    assert result["feature_count"] == 20
    assert result["direction_consistent_features"] == 19
    assert result["all_epoch_auc_52_feature_count"] == 7
    assert result["features"][0]["feature"] == "micro_log_price_range_250ms"
    assert result["features"][0]["minimum_later_oriented_auc"] > 0.648
    assert "micro_log_return_250ms" in result["all_epoch_auc_52_features"]
    assert "micro_flow_imbalance_250ms" in result["all_epoch_auc_52_features"]


def test_diagnostic_does_not_fit_or_authorise_a_candidate() -> None:
    result = report()
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["microstructure_cache_audit"]["future_values_in_features"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
