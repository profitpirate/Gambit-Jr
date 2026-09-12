from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

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


holdout = load_module(
    "e4_v12_causal_regime_veto_holdout_tests",
    SCRIPTS / "e4_v12_causal_regime_veto_holdout.py",
)


def protocol():
    return json.loads((ROOT / holdout.PROTOCOL_PATH).read_text(encoding="utf-8"))


def metric(*, pnl: float = 1.0, quote_coverage: float = 1.0):
    return {
        "trades": 100,
        "wins": 70,
        "win_rate": 0.70,
        "wilson_95_lower_bound": 0.60,
        "net_pnl_sol": pnl,
        "profit_factor": 1.6,
        "maximum_drawdown_fraction": 0.10,
        "capture_windows": 5,
        "largest_winner_contribution": 0.10,
        "quote_coverage": quote_coverage,
        "by_capture_window": {
            str(index): {"trades": 20, "wins": 14, "pnl_sol": 0.2}
            for index in range(5)
        },
    }


def write_source(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def capture(
    tmp_path: Path,
    *,
    run_id: str,
    role: str,
    workflow_start_ns: int,
    capture_start_ns: int,
    capture_end_ns: int,
):
    events = tmp_path / f"{run_id}-events.jsonl"
    batch = tmp_path / f"{run_id}-batch.json"
    return {
        "run_id": run_id,
        "role": role,
        "workflow_run_started_at_epoch_ns": workflow_start_ns,
        "capture_start_ns": capture_start_ns,
        "capture_end_ns": capture_end_ns,
        "launches": 3000,
        "decoded_events": 3000,
        "capture_errors": 0,
        "events_path": str(events.relative_to(ROOT)),
        "events_sha256": write_source(events, b"events"),
        "batch_path": str(batch.relative_to(ROOT)),
        "batch_sha256": write_source(batch, b"batch"),
    }


def test_protocol_content_addresses_the_frozen_candidate() -> None:
    specification = protocol()
    frozen = ROOT / specification["frozen_candidate"]["path"]
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == (
        specification["frozen_candidate"]["sha256"]
    )
    assert holdout.verify_protocol(ROOT)["experiment_id"] == (
        specification["experiment_id"]
    )
    assert specification["quarantine"]["may_count_toward_final_gate"] is False
    assert specification["result_policy"]["production_deployment_authorised"] is False


def test_prefreeze_capture_is_quarantine_but_never_final(tmp_path: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    early = capture(
        tmp_path,
        run_id="1",
        role=holdout.QUARANTINE_ROLE,
        workflow_start_ns=freeze_ns - 2,
        capture_start_ns=freeze_ns - 1,
        capture_end_ns=freeze_ns + 1,
    )
    manifest = {
        "version": holdout.MANIFEST_VERSION,
        "experiment_id": specification["experiment_id"],
        "captures": [early],
    }
    assert holdout.validate_manifest(ROOT, manifest, specification)[0]["role"] == (
        holdout.QUARANTINE_ROLE
    )
    manifest["captures"][0]["role"] = holdout.FINAL_ROLE
    with pytest.raises(ValueError, match="did not begin after freeze"):
        holdout.validate_manifest(ROOT, manifest, specification)


def test_final_captures_must_be_hash_valid_and_non_overlapping(tmp_path: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    first = capture(
        tmp_path,
        run_id="2",
        role=holdout.FINAL_ROLE,
        workflow_start_ns=freeze_ns + 1,
        capture_start_ns=freeze_ns + 2,
        capture_end_ns=freeze_ns + 10,
    )
    second = capture(
        tmp_path,
        run_id="3",
        role=holdout.FINAL_ROLE,
        workflow_start_ns=freeze_ns + 11,
        capture_start_ns=freeze_ns + 9,
        capture_end_ns=freeze_ns + 20,
    )
    manifest = {
        "version": holdout.MANIFEST_VERSION,
        "experiment_id": specification["experiment_id"],
        "captures": [first, second],
    }
    with pytest.raises(ValueError, match="overlap"):
        holdout.validate_manifest(ROOT, manifest, specification)
    second["capture_start_ns"] = freeze_ns + 11
    second["events_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="events hash mismatch"):
        holdout.validate_manifest(ROOT, manifest, specification)


def test_golden_gate_requires_all_latency_and_baseline_checks() -> None:
    specification = protocol()
    economics = {str(latency): metric() for latency in (0, 1, 2, 5, 10)}
    passing = holdout.golden_gate(economics, metric(pnl=0.5), specification)
    assert passing["untouched_live_gate_passed"] is True
    assert all(passing["requirements"].values())

    degraded = deepcopy(economics)
    degraded["10"]["quote_coverage"] = 0.99
    failing = holdout.golden_gate(degraded, metric(pnl=0.5), specification)
    assert failing["untouched_live_gate_passed"] is False
    assert failing["failure_classification"] == "LATENCY_FAILURE"
    assert "every_latency_full_quote_coverage" in failing["failed_requirements"]


def test_holdout_protocol_cannot_authorise_production() -> None:
    specification = protocol()
    assert specification["result_policy"]["production_promotion_authorised"] is False
    assert specification["result_policy"]["production_paths_changed"] == 0
    source = (SCRIPTS / "e4_v12_causal_regime_veto_holdout.py").read_text(
        encoding="utf-8"
    )
    assert "src/memecoin_bot" not in source
