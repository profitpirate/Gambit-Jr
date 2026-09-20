from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
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


registration = load_module(
    "e4_v12_register_holdout_capture_tests",
    SCRIPTS / "e4_v12_register_holdout_capture.py",
)


def protocol():
    return registration.holdout.verify_protocol(ROOT)


@pytest.fixture
def repo_tmp() -> Path:
    parent = ROOT / ".tmp-holdout-registration-pytest"
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="case-", dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            parent.rmdir()
        except OSError:
            pass


def artifact(repo_tmp: Path, run_id: str, start_ns: int) -> Path:
    directory = repo_tmp / run_id / "artifacts"
    directory.mkdir(parents=True)
    cohort = [
        {"mint": f"mint-{index}", "received_ns": start_ns + index}
        for index in range(3000)
    ]
    batch = {
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "commit": "source-commit",
        "capture": {
            "unique_launches": 3000,
            "target_reached": True,
            "decoded_events": 3000,
            "errors": [],
            "cohort": cohort,
            "final_progress": {"timestamp_ns": start_ns + 4000},
        },
    }
    (directory / "e4-v12-forward-batch.json").write_text(
        json.dumps(batch), encoding="utf-8"
    )
    (directory / "e4-v12-forward-batch-live-events.jsonl").write_text(
        "{}\n" * 3000, encoding="utf-8"
    )
    (directory / "e4-v12-copy-audit.json").write_text(
        json.dumps({"source_run": int(run_id), "fresh_launches": 3000, "latency_ms": 36}),
        encoding="utf-8",
    )
    return directory.parent


def test_epoch_ns_requires_timezone() -> None:
    assert registration.epoch_ns("2026-09-12T23:21:48Z") == 1789255308000000000
    with pytest.raises(ValueError, match="timezone"):
        registration.epoch_ns("2026-09-12T23:21:48")


def test_registration_assigns_role_from_both_workflow_and_capture_time(
    repo_tmp: Path,
) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    final_dir = artifact(repo_tmp, "10", freeze_ns + 2)
    final = registration.capture_record(
        ROOT,
        final_dir,
        "10",
        "2026-09-12T23:21:48Z",
        specification,
    )
    assert final["role"] == registration.holdout.FINAL_ROLE
    assert final["launches"] == 3000
    assert final["decoded_events"] == 3000

    quarantine_dir = artifact(repo_tmp, "11", freeze_ns + 5000)
    quarantine = registration.capture_record(
        ROOT,
        quarantine_dir,
        "11",
        "2026-09-12T23:21:46Z",
        specification,
    )
    assert quarantine["role"] == registration.holdout.QUARANTINE_ROLE


def test_registration_rejects_event_count_and_copy_audit_mismatch(
    repo_tmp: Path,
) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(repo_tmp, "12", freeze_ns + 2)
    events = directory / "artifacts/e4-v12-forward-batch-live-events.jsonl"
    events.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line count"):
        registration.capture_record(
            ROOT,
            directory,
            "12",
            "2026-09-12T23:21:48Z",
            specification,
        )
    events.write_text("{}\n" * 3000, encoding="utf-8")
    audit = directory / "artifacts/e4-v12-copy-audit.json"
    audit.write_text(
        json.dumps({"source_run": 12, "fresh_launches": 3000, "latency_ms": 35}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="36 ms"):
        registration.capture_record(
            ROOT,
            directory,
            "12",
            "2026-09-12T23:21:48Z",
            specification,
        )


def test_atomic_registration_is_idempotent(repo_tmp: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(repo_tmp, "13", freeze_ns + 2)
    record = registration.capture_record(
        ROOT,
        directory,
        "13",
        "2026-09-12T23:21:48Z",
        specification,
    )
    manifest_path = repo_tmp / "manifest.json"
    first = registration.register(ROOT, manifest_path, record, specification)
    first_bytes = manifest_path.read_bytes()
    second = registration.register(ROOT, manifest_path, record, specification)
    assert first == second
    assert manifest_path.read_bytes() == first_bytes
    assert not list(repo_tmp.glob(".*.tmp"))


def test_registration_rejects_mainnet_activity(repo_tmp: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(repo_tmp, "14", freeze_ns + 2)
    path = directory / "artifacts/e4-v12-forward-batch.json"
    batch = json.loads(path.read_text(encoding="utf-8"))
    batch["mainnet_transactions_sent"] = 1
    path.write_text(json.dumps(batch), encoding="utf-8")
    with pytest.raises(ValueError, match="sent mainnet"):
        registration.capture_record(
            ROOT,
            directory,
            "14",
            "2026-09-12T23:21:48Z",
            specification,
        )
