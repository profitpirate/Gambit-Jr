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
    "e4_v12_relative_price_capped_actor_holdout_tests",
    SCRIPTS / "e4_v12_relative_price_capped_actor_holdout.py",
)


def protocol() -> dict:
    return json.loads((ROOT / holdout.PROTOCOL_PATH).read_text(encoding="utf-8"))


def metric(*, pnl: float = 1.0, win_rate: float = 0.70, pf: float = 1.6) -> dict:
    return {
        "trades": 100,
        "wins": 70,
        "win_rate": win_rate,
        "wilson_95_lower_bound": 0.60,
        "net_pnl_sol": pnl,
        "profit_factor": pf,
        "maximum_drawdown_fraction": 0.10,
        "capture_windows": 8,
        "largest_winner_contribution": 0.10,
        "quote_coverage": 1.0,
        "by_capture_window": {
            str(index): {"trades": 12, "wins": 9, "pnl_sol": 0.2}
            for index in range(8)
        },
    }


def write_capture_file(root: Path, name: str, content: bytes) -> tuple[str, str]:
    path = root / name
    path.write_bytes(content)
    return name, holdout.base.sha256_path(path)


def capture(
    root: Path,
    *,
    run_id: str,
    workflow_ns: int,
    start_ns: int,
    end_ns: int,
) -> dict:
    events_path, events_sha = write_capture_file(root, f"{run_id}.jsonl", b"{}\n")
    batch_path, batch_sha = write_capture_file(root, f"{run_id}.json", b"{}\n")
    return {
        "run_id": run_id,
        "role": holdout.FINAL_ROLE,
        "workflow_run_started_at_epoch_ns": workflow_ns,
        "capture_start_ns": start_ns,
        "capture_end_ns": end_ns,
        "launches": 3000,
        "capture_errors": 0,
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
        "events_path": events_path,
        "events_sha256": events_sha,
        "batch_path": batch_path,
        "batch_sha256": batch_sha,
    }


def test_v11_protocol_is_frozen_and_never_authorises_production() -> None:
    specification = holdout.verify_protocol(ROOT)
    assert specification["experiment_id"].startswith("e4x-")
    assert specification["final_evidence_contract"]["required_capture_windows"] == 10
    assert specification["final_evidence_contract"]["optional_stopping_allowed"] is False
    assert specification["result_policy"]["production_deployment_authorised"] is False
    assert specification["result_policy"]["production_paths_changed"] == 0


def test_v11_manifest_requires_postfreeze_hash_valid_nonoverlapping_windows(
    tmp_path: Path,
) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    first = capture(
        tmp_path,
        run_id="1",
        workflow_ns=freeze_ns + 1,
        start_ns=freeze_ns + 2,
        end_ns=freeze_ns + 10,
    )
    second = capture(
        tmp_path,
        run_id="2",
        workflow_ns=freeze_ns + 11,
        start_ns=freeze_ns + 9,
        end_ns=freeze_ns + 20,
    )
    manifest = {
        "version": holdout.MANIFEST_VERSION,
        "experiment_id": specification["experiment_id"],
        "captures": [first, second],
    }
    with pytest.raises(ValueError, match="overlap"):
        holdout.validate_manifest(tmp_path, manifest, specification)
    second["capture_start_ns"] = freeze_ns + 11
    second["events_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="events hash mismatch"):
        holdout.validate_manifest(tmp_path, manifest, specification)
    second["events_sha256"] = holdout.base.sha256_path(
        tmp_path / second["events_path"]
    )
    assert len(holdout.validate_manifest(tmp_path, manifest, specification)) == 2


def test_v11_manifest_rejects_prefreeze_and_mainnet_activity(tmp_path: Path) -> None:
    specification = protocol()
    freeze_ns = specification["frozen_candidate"]["frozen_at_epoch_ns"]
    row = capture(
        tmp_path,
        run_id="3",
        workflow_ns=freeze_ns - 1,
        start_ns=freeze_ns + 2,
        end_ns=freeze_ns + 10,
    )
    manifest = {
        "version": holdout.MANIFEST_VERSION,
        "experiment_id": specification["experiment_id"],
        "captures": [row],
    }
    with pytest.raises(ValueError, match="did not begin"):
        holdout.validate_manifest(tmp_path, manifest, specification)
    row["workflow_run_started_at_epoch_ns"] = freeze_ns + 1
    row["mainnet_transactions_sent"] = 1
    with pytest.raises(ValueError, match="sent mainnet"):
        holdout.validate_manifest(tmp_path, manifest, specification)


def test_v11_golden_gate_requires_latency_and_all_three_baseline_improvements() -> None:
    specification = protocol()
    economics = {str(latency): metric() for latency in (0, 1, 2, 5, 10)}
    passing = holdout.golden_gate(
        economics,
        metric(pnl=0.5, win_rate=0.60, pf=1.3),
        specification,
    )
    assert passing["untouched_live_gate_passed"] is True
    assert all(passing["requirements"].values())

    degraded = deepcopy(economics)
    degraded["10"]["quote_coverage"] = 0.99
    failure = holdout.golden_gate(
        degraded,
        metric(pnl=0.5, win_rate=0.60, pf=1.3),
        specification,
    )
    assert failure["untouched_live_gate_passed"] is False
    assert failure["failure_classification"] == "LATENCY_FAILURE"

    baseline = metric(pnl=2.0, win_rate=0.75, pf=2.0)
    failure = holdout.golden_gate(economics, baseline, specification)
    assert failure["untouched_live_gate_passed"] is False
    assert {
        "beats_capped_actor_baseline_pnl",
        "beats_capped_actor_baseline_win_rate",
        "beats_capped_actor_baseline_profit_factor",
    }.issubset(failure["failed_requirements"])


def test_v11_partial_manifest_does_not_reveal_early_economics(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": holdout.MANIFEST_VERSION,
                "experiment_id": protocol()["experiment_id"],
                "captures": [],
            }
        ),
        encoding="utf-8",
    )
    result = holdout.run(ROOT, manifest_path, tmp_path / "unused.pkl")
    assert result["final_capture_windows"] == 0
    assert result["latencies"] == {}
    assert result["same_size_capped_actor_only_baseline"] == {}
    assert result["verdict"]["status"] == "WAITING_FOR_10_STRICTLY_LATER_WINDOWS"
    assert result["production_paths_changed"] == 0
