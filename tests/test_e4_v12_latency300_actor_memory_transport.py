from __future__ import annotations

import importlib.util
import json
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


diagnostic = load_module(
    "e4_v12_latency300_actor_memory_transport_tests",
    SCRIPTS / "e4_v12_latency300_actor_memory_transport.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-actor-memory-transport.json").read_text(
            encoding="utf-8"
        )
    )


def test_actor_state_is_updated_only_after_resolution_delay() -> None:
    assert diagnostic.RESOLUTION_DELAY_MS == 60_000
    source = (
        SCRIPTS / "e4_v12_latency300_actor_memory_transport.py"
    ).read_text(encoding="utf-8")
    feature_call = "values = actors.actor_features("
    pending_push = "heapq.heappush("
    assert source.index(feature_call) < source.index(pending_push)


def test_report_binds_exact_latency_labels_and_actor_matrix() -> None:
    result = report()
    identity = result["identity"]
    audit = result["actor_memory_cache_audit"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == diagnostic.source_code_fingerprint(
        ROOT
    )
    assert identity["latency_ms"] == 300
    assert identity["resolution_delay_ms"] == 60_000
    assert identity["exact_actor_memory_matrix_sha256"] == audit["matrix_sha256"]
    assert audit["rows"] == 191_425
    assert audit["features"] == 16
    assert audit["capture_windows"] == 64
    assert audit["raw_capture_runs"] == 48
    assert audit["cached_later_runs"] == 16
    assert audit["future_values_in_features"] is False


def test_diagnostic_never_fits_or_authorises_candidate() -> None:
    result = report()
    assert result["materially_improved_stable_feature_count"] == 0
    assert result["materially_improved_stable_features"] == []
    assert result["candidate_warranted"] is False
    assert result["candidate_fitted"] is False
    assert result["retired_family"] is True
    assert result["failure_classification"] == "VALIDATION_COLLAPSE"
    assert result["development_gate_passed"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0
