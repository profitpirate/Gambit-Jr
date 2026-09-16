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
    "e4_v12_latency300_early_horizon_frontier_tests",
    SCRIPTS / "e4_v12_latency300_early_horizon_frontier.py",
)


def report() -> dict:
    return json.loads(
        (ROOT / "artifacts/e4-v12-latency300-early-horizon-frontier.json").read_text(
            encoding="utf-8"
        )
    )


def point(timestamp_ms: int, virtual_sol: float, virtual_tokens: float):
    return diagnostic.base.Point(
        timestamp_ns=timestamp_ms * 1_000_000,
        kind="BUY",
        trader=f"trader-{timestamp_ms}",
        signature=f"signature-{timestamp_ms}",
        slot=timestamp_ms,
        sol_amount=1.0,
        price_sol=virtual_sol / virtual_tokens,
        virtual_sol=virtual_sol,
        virtual_tokens=virtual_tokens,
        real_tokens=virtual_tokens,
        complete=False,
    )


def test_horizons_apply_same_latency_at_different_launch_relative_fills() -> None:
    trace = diagnostic.base.Trace(
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
            point(0, 30.0, 1_000.0),
            point(300, 36.0, 850.0),
            point(550, 45.0, 700.0),
            point(1_000, 50.0, 650.0),
            point(60_300, 55.0, 600.0),
            point(60_550, 55.0, 600.0),
        ],
    )
    values = diagnostic.evaluate_trace(trace)
    assert set(values) == set(diagnostic.DECISION_HORIZONS_MS)
    assert diagnostic.LATENCY_MS == 300
    assert values[0] is not None
    assert values[250] is not None
    assert values[0] > values[250]


def test_report_is_bound_to_exact_development_population() -> None:
    result = report()
    identity = result["identity"]
    assert result["diagnostic_id"] == (
        f"e4d-{diagnostic.base.stable_hash(identity)}"
    )
    assert identity["source_code_fingerprint"] == diagnostic.source_code_fingerprint(
        ROOT
    )
    assert identity["latency_ms"] == 300
    assert identity["reference_horizon_ms"] == 250
    assert identity["decision_horizons_ms"] == list(
        diagnostic.DECISION_HORIZONS_MS
    )
    assert result["rows"] == 191_425
    assert result["windows"] == 64
    assert result["raw_capture_runs"] == 48
    assert result["cached_later_runs"] == 16
    assert result["raw_source_hashes_verified"] is True
    assert all(row["population_rows"] == result["rows"] for row in result["frontier"])


def test_diagnostic_never_fits_or_authorises_a_candidate() -> None:
    result = report()
    assert result["candidate_fitted"] is False
    assert result["active_untouched_live_data_used"] is False
    assert result["untouched_holdout_passed"] is False
    assert result["live_confirmation_authorised"] is False
    assert result["production_promotion_authorised"] is False
    assert result["production_deployment_authorised"] is False
    assert result["production_paths_changed"] == 0


def test_run_audits_are_reduced_in_canonical_order() -> None:
    source = (
        SCRIPTS / "e4_v12_latency300_early_horizon_frontier.py"
    ).read_text(encoding="utf-8")
    sort = 'audits.sort(key=lambda row: int(row["run_id"]))'
    reduction = "for audit in audits:"
    assert source.index(sort) < source.index(reduction)
