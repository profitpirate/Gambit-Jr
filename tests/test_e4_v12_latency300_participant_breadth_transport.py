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
    "e4_v12_latency300_participant_breadth_transport_tests",
    SCRIPTS / "e4_v12_latency300_participant_breadth_transport.py",
)


def report() -> dict:
    return json.loads(
        (
            ROOT / "artifacts/e4-v12-latency300-participant-breadth-transport.json"
        ).read_text(encoding="utf-8")
    )


def point(timestamp_ms: int, kind: str, trader: str, sol: float, price: float):
    return research.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind=kind,
        trader=trader,
        signature=f"{timestamp_ms}-{kind}-{trader}",
        slot=timestamp_ms,
        sol_amount=sol,
        price_sol=price,
        virtual_sol=10.0,
        virtual_tokens=1.0,
        real_tokens=1.0,
        complete=False,
    )


def test_participant_features_are_causal_and_identity_aware() -> None:
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
            point(100, "BUY", "a", 1.0, 1.0),
            point(180, "BUY", "a", 1.0, 1.1),
            point(210, "BUY", "b", 2.0, 1.2),
            point(230, "SELL", "c", 1.0, 1.1),
            point(250, "SELL", "b", 1.0, 1.0),
            point(251, "BUY", "future", 100.0, 2.0),
        ],
    )
    values = research.participant_features(trace)
    assert values is not None
    assert len(values) == len(research.PARTICIPANT_FEATURE_NAMES) == 16
    assert values[:4] == (3.0, 3.0, 2.0, 2.0)
    assert values[4] == 3 / 5
    assert values[5] == 2 / 3
    assert values[6] == 3 / 6
    assert values[7] == (2 / 6) ** 2 + (3 / 6) ** 2 + (1 / 6) ** 2
    expected_entropy = -sum(share * math.log(share) for share in (2 / 6, 3 / 6, 1 / 6))
    assert values[8] == expected_entropy / math.log(3)
    assert values[9] == math.exp(expected_entropy)
    assert values[10] == 1.0
    assert values[11] == 2 / 3
    assert values[12] == 2 / 6
    assert values[13] == math.log1p(2.0)
    assert values[14] == 1.0
    assert values[15] == 1 / 3


def test_report_binds_full_matrix_exact_labels_and_no_live_data() -> None:
    result = report()
    identity = result["identity"]
    audit = result["participant_cache_audit"]
    assert result["diagnostic_id"] == f"e4d-{research.base.stable_hash(identity)}"
    assert identity["source_code_fingerprint"] == research.source_code_fingerprint(ROOT)
    assert identity["latency_ms"] == 300
    assert identity["causal_horizon_ms"] == 250
    assert identity["participant_matrix_sha256"] == audit["matrix_sha256"]
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
