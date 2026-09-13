from __future__ import annotations

import importlib.util
import json
import sys
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
    "e4_v12_register_relative_price_capture_tests",
    SCRIPTS / "e4_v12_register_relative_price_capture.py",
)


def protocol() -> dict:
    return registration.holdout.verify_protocol(ROOT)


def artifact(root: Path, run_id: str, start_ns: int) -> Path:
    directory = root / run_id / "artifacts"
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
    (directory / registration.BATCH_NAME).write_text(
        json.dumps(batch), encoding="utf-8"
    )
    (directory / registration.EVENTS_NAME).write_text(
        "{}\n" * 3000, encoding="utf-8"
    )
    return directory.parent


def test_v11_registration_epoch_requires_timezone() -> None:
    assert registration.epoch_ns("2026-09-13T23:32:47Z") == 1789342367000000000
    with pytest.raises(ValueError, match="timezone"):
        registration.epoch_ns("2026-09-13T23:32:47")


def test_v11_capture_record_validates_exact_read_only_envelope(tmp_path: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(tmp_path, "10", freeze_ns + 2)
    record = registration.capture_record(
        tmp_path,
        directory,
        "10",
        "2026-09-13T23:32:47Z",
        specification,
    )
    assert record["role"] == registration.holdout.FINAL_ROLE
    assert record["launches"] == 3000
    assert record["decoded_events"] == 3000
    assert record["mainnet_transactions_sent"] == 0
    assert record["mainnet_funds_risked_sol"] == 0


def test_v11_registration_rejects_event_count_and_mainnet_activity(
    tmp_path: Path,
) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(tmp_path, "11", freeze_ns + 2)
    events = directory / "artifacts" / registration.EVENTS_NAME
    events.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line count"):
        registration.capture_record(
            tmp_path,
            directory,
            "11",
            "2026-09-13T23:32:47Z",
            specification,
        )
    events.write_text("{}\n" * 3000, encoding="utf-8")
    batch_path = directory / "artifacts" / registration.BATCH_NAME
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch["mainnet_transactions_sent"] = 1
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    with pytest.raises(ValueError, match="sent mainnet"):
        registration.capture_record(
            tmp_path,
            directory,
            "11",
            "2026-09-13T23:32:47Z",
            specification,
        )


def test_v11_atomic_registration_is_idempotent(tmp_path: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(tmp_path, "12", freeze_ns + 2)
    record = registration.capture_record(
        tmp_path,
        directory,
        "12",
        "2026-09-13T23:32:47Z",
        specification,
    )
    manifest_path = tmp_path / "manifest.json"
    first = registration.register(tmp_path, manifest_path, record, specification)
    first_bytes = manifest_path.read_bytes()
    second = registration.register(tmp_path, manifest_path, record, specification)
    assert first == second
    assert manifest_path.read_bytes() == first_bytes
    assert not list(tmp_path.glob(".*.tmp"))


def test_v11_registration_rejects_duplicate_run_with_changed_content(
    tmp_path: Path,
) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    directory = artifact(tmp_path, "13", freeze_ns + 2)
    record = registration.capture_record(
        tmp_path,
        directory,
        "13",
        "2026-09-13T23:32:47Z",
        specification,
    )
    manifest_path = tmp_path / "manifest.json"
    registration.register(tmp_path, manifest_path, record, specification)
    changed = dict(record) | {"events_sha256": "0" * 64}
    with pytest.raises(ValueError, match="different content"):
        registration.register(tmp_path, manifest_path, changed, specification)
