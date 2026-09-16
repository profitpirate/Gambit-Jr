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
    "e4_v12_latency300_price_path_transport_tests",
    SCRIPTS / "e4_v12_latency300_price_path_transport.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-price-path-transport.json").read_text(
            encoding="utf-8"
        )
    )


def point(timestamp_ms: int, price: float):
    return research.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind="BUY",
        trader=f"trader-{timestamp_ms}",
        signature=f"signature-{timestamp_ms}",
        slot=timestamp_ms,
        sol_amount=1.0,
        price_sol=price,
        virtual_sol=10.0,
        virtual_tokens=1.0,
        real_tokens=1.0,
        complete=False,
    )


def test_price_path_features_are_causal_and_ignore_future_points() -> None:
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
            point(0, 1.0),
            point(100, 2.0),
            point(150, 1.5),
            point(250, 3.0),
            point(251, 0.01),
        ],
    )
    values = research.price_path_features(trace)
    assert values is not None
    assert len(values) == len(research.PRICE_PATH_FEATURE_NAMES) == 16
    steps = (math.log(2.0), math.log(1.5 / 2.0), math.log(2.0))
    total_variation = sum(abs(step) for step in steps)
    assert math.isclose(values[0], math.log1p(total_variation))
    assert math.isclose(values[1], math.log(3.0) / total_variation)
    assert values[2] == 0.0
    assert math.isclose(values[3], math.log(2.0 / 1.5))
    assert values[4] == 1.0
    assert values[5] == 2 / 3
    assert values[6] == 2 / 3
    assert values[7] == 1.0
    assert values[8] == 0.0
    assert math.isclose(values[9], math.log(3.0))
    assert math.isclose(values[10], math.log(1.5 / 2.0))
    assert math.isclose(values[11], math.log(2.0))


def test_report_binds_full_matrix_exact_labels_and_no_live_data() -> None:
    result = report()
    identity = result["identity"]
    audit = result["price_path_cache_audit"]
    assert result["diagnostic_id"] == f"e4d-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["latency_ms"] == 300
    assert identity["causal_horizon_ms"] == 250
    assert identity["price_path_matrix_sha256"] == audit["matrix_sha256"]
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
    assert result["candidate_fitted"] is False
    assert result["development_gate_passed"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
