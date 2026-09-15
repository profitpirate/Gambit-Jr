from __future__ import annotations

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
    "e4_v12_online_conformal_holdout_tests",
    SCRIPTS / "e4_v12_online_conformal_holdout.py",
)
registration = load_module(
    "e4_v12_register_online_conformal_capture_tests",
    SCRIPTS / "e4_v12_register_online_conformal_capture.py",
)


def protocol() -> dict:
    return json.loads((ROOT / holdout.PROTOCOL_PATH).read_text(encoding="utf-8"))


def metric(
    *,
    pnl: float = 1.0,
    win_rate: float = 0.70,
    profit_factor: float = 1.6,
) -> dict:
    return {
        "trades": 100,
        "wins": 70,
        "win_rate": win_rate,
        "wilson_95_lower_bound": 0.60,
        "net_pnl_sol": pnl,
        "profit_factor": profit_factor,
        "maximum_drawdown_fraction": 0.10,
        "capture_windows": 8,
        "profitable_capture_windows": 7,
        "largest_winner_contribution": 0.10,
        "quote_coverage": 1.0,
    }


def write_protocol(root: Path, specification: dict) -> None:
    path = root / holdout.PROTOCOL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(specification), encoding="utf-8")


def capture(
    root: Path,
    specification: dict,
    *,
    run_id: str,
    offset: int,
) -> dict:
    events = root / f"{run_id}.jsonl"
    batch = root / f"{run_id}.json"
    events.write_bytes(b"{}\n")
    batch.write_bytes(b"{}\n")
    freeze_ns = int(specification["frozen_candidate"]["frozen_at_epoch_ns"])
    return {
        "run_id": run_id,
        "role": holdout.FINAL_ROLE,
        "workflow_run_started_at_epoch_ns": freeze_ns + offset,
        "capture_start_ns": freeze_ns + offset + 1,
        "capture_end_ns": freeze_ns + offset + 10,
        "launches": 3000,
        "capture_errors": 0,
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "events_path": events.name,
        "events_sha256": holdout.base.sha256_path(events),
        "batch_path": batch.name,
        "batch_sha256": holdout.base.sha256_path(batch),
    }


def manifest(root: Path, specification: dict, captures: list[dict]) -> dict:
    write_protocol(root, specification)
    return {
        "version": holdout.MANIFEST_VERSION,
        "experiment_id": specification["experiment_id"],
        "protocol_sha256_lf": holdout.precision.sha256_lf(
            root / holdout.PROTOCOL_PATH
        ),
        "captures": captures,
        "production_paths_changed": 0,
    }


def test_protocol_is_frozen_and_never_authorises_production() -> None:
    specification = holdout.verify_protocol(ROOT)
    assert specification["experiment_id"].startswith("e4x-")
    contract = specification["final_evidence_contract"]
    assert contract["required_capture_windows"] == 10
    assert contract["required_total_launches"] == 30_000
    assert contract["optional_stopping_allowed"] is False
    assert specification["result_policy"]["final_gate_is_evaluated_once"] is True
    assert specification["result_policy"]["production_deployment_authorised"] is False
    assert specification["result_policy"]["production_paths_changed"] == 0


def test_empty_manifest_is_valid_and_waiting_is_not_a_live_pass() -> None:
    specification = holdout.verify_protocol(ROOT)
    payload = holdout.read_json(ROOT / holdout.DEFAULT_MANIFEST_PATH)
    captures = holdout.validate_manifest(ROOT, payload, specification)
    report = holdout.waiting_report(
        ROOT, specification, ROOT / holdout.DEFAULT_MANIFEST_PATH, captures
    )
    assert captures == []
    assert report["final_evaluation_complete"] is False
    assert report["untouched_holdout_passed"] is False
    assert report["verdict"]["status"] == "WAITING_FOR_10_STRICTLY_LATER_WINDOWS"


def test_manifest_rejects_overlap_pre_freeze_hash_changes_and_extra_windows(
    tmp_path: Path,
) -> None:
    specification = protocol()
    first = capture(tmp_path, specification, run_id="1", offset=100)
    second = capture(tmp_path, specification, run_id="2", offset=105)
    overlapping = manifest(tmp_path, specification, [first, second])
    with pytest.raises(ValueError, match="overlap"):
        holdout.validate_manifest(tmp_path, overlapping, specification)

    prefreeze = deepcopy(first)
    prefreeze["workflow_run_started_at_epoch_ns"] = 0
    payload = manifest(tmp_path, specification, [prefreeze])
    with pytest.raises(ValueError, match="after the freeze"):
        holdout.validate_manifest(tmp_path, payload, specification)

    changed = deepcopy(first)
    changed["events_sha256"] = "0" * 64
    payload = manifest(tmp_path, specification, [changed])
    with pytest.raises(ValueError, match="events hash"):
        holdout.validate_manifest(tmp_path, payload, specification)

    captures = [
        capture(tmp_path, specification, run_id=str(index), offset=index * 20)
        for index in range(1, 12)
    ]
    payload = manifest(tmp_path, specification, captures)
    with pytest.raises(ValueError, match="exceeds"):
        holdout.validate_manifest(tmp_path, payload, specification)


def test_conservative_concurrency_is_chronological_and_deterministic() -> None:
    rows = [
        {"run_id": "2", "mint": "c", "decision_ns": 100_000_000_000},
        {"run_id": "1", "mint": "b", "decision_ns": 2},
        {"run_id": "1", "mint": "a", "decision_ns": 1},
    ]
    first, rejected = holdout.conservative_candidates(rows)
    second, repeated_rejected = holdout.conservative_candidates(rows)
    assert [row["mint"] for row in first] == ["a", "b", "c"]
    assert first == second
    assert rejected == repeated_rejected == 0


def test_golden_gate_passes_only_complete_untouched_live_economics() -> None:
    specification = protocol()
    economics = {str(delay): metric() for delay in (0, 1, 2, 5, 10)}
    passed = holdout.golden_gate(economics, specification)
    assert passed["untouched_live_gate_passed"] is True
    assert passed["status"] == "UNTOUCHED_LIVE_GATE_PASSED"
    assert passed["production_promotion_authorised"] is False
    assert passed["production_deployment_authorised"] is False

    failed = deepcopy(economics)
    failed["0"]["win_rate"] = 0.649
    verdict = holdout.golden_gate(failed, specification)
    assert verdict["untouched_live_gate_passed"] is False
    assert verdict["failure_classification"] == "FALSE_POSITIVE_OVERLOAD"


def test_registration_is_atomic_idempotent_and_rejects_changed_duplicate(
    tmp_path: Path,
) -> None:
    specification = protocol()
    record = capture(tmp_path, specification, run_id="100", offset=100)
    manifest_path = tmp_path / "manifest.json"
    initial = manifest(tmp_path, specification, [])
    registration.atomic_json(manifest_path, initial)
    first = registration.register(
        tmp_path, manifest_path, record, specification
    )
    assert first["captures"] == [record]
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))
    second = registration.register(
        tmp_path, manifest_path, record, specification
    )
    assert second == first
    changed = dict(record) | {"batch_sha256": "0" * 64}
    with pytest.raises(ValueError, match="different content"):
        registration.register(
            tmp_path, manifest_path, changed, specification
        )


def test_capture_registration_requires_exact_complete_read_only_envelope(
    tmp_path: Path,
) -> None:
    specification = protocol()
    artifact_dir = tmp_path / "download"
    artifact_dir.mkdir()
    cohort = [
        {"mint": f"mint-{index}", "received_ns": 10_000 + index}
        for index in range(3000)
    ]
    batch = {
        "commit": "abc123",
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "capture": {
            "unique_launches": 3000,
            "target_reached": True,
            "errors": [],
            "decoded_events": 2,
            "cohort": cohort,
            "final_progress": {"timestamp_ns": 20_000},
        },
    }
    (artifact_dir / registration.BATCH_NAME).write_text(
        json.dumps(batch), encoding="utf-8"
    )
    (artifact_dir / registration.EVENTS_NAME).write_text(
        "{}\n{}\n", encoding="utf-8"
    )
    record = registration.capture_record(
        tmp_path,
        artifact_dir,
        "123",
        "2026-09-15T12:00:00Z",
        specification,
    )
    assert record["launches"] == 3000
    assert record["decoded_events"] == 2
    assert record["hypothesis_only"] is True
    assert record["mainnet_transactions_sent"] == 0
    assert record["mainnet_funds_risked_sol"] == 0

    batch["capture"]["errors"] = ["provider failure"]
    (artifact_dir / registration.BATCH_NAME).write_text(
        json.dumps(batch), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="errors"):
        registration.capture_record(
            tmp_path,
            artifact_dir,
            "124",
            "2026-09-15T12:00:00Z",
            specification,
        )


def test_forward_workflow_is_bounded_read_only_and_stops_after_ten_windows() -> None:
    workflow = (
        ROOT / ".github/workflows/e4-v12-online-conformal-forward.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "codex/e4-v12-flow-survival-hazard-v12" in workflow
    assert "contents: read" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "registered < required" in workflow
    assert "if: needs.contract.outputs.capture_needed == 'true'" in workflow
    assert "--target-launches 3000" in workflow
    assert "--minimum-launches 3000" in workflow
    assert "--builder-probes 0" in workflow
    assert "--testnet-route-probes 0" in workflow
    assert "--stress-iterations 0" in workflow
    assert "mainnet_transactions_sent" in workflow
    assert "mainnet_funds_risked_sol" in workflow
    assert "actions/upload-artifact@v4" in workflow
