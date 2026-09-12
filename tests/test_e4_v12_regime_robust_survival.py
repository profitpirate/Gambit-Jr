from __future__ import annotations

import importlib.util
import json
import sys
from itertools import pairwise
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
    "e4_v12_regime_robust_survival_tests",
    SCRIPTS / "e4_v12_regime_robust_survival.py",
)


def artifact(name: str):
    return json.loads(
        (ROOT / "artifacts" / f"e4-v12-regime-robust-{name}.json").read_text(
            encoding="utf-8"
        )
    )


def test_new_evidence_epoch_is_strictly_chronological() -> None:
    specs = research.all_capture_specs(ROOT, include_holdout=True)
    assert len(specs) == 33
    assert all(right.start_ns > left.start_ns for left, right in pairwise(specs))
    assert [spec.split for spec in specs].count("train") == 27
    assert [spec.split for spec in specs].count("validation") == 3
    assert [spec.split for spec in specs].count("holdout") == 3


def test_default_inventory_keeps_later_holdout_sealed() -> None:
    specs = research.all_capture_specs(ROOT, include_holdout=False)
    assert len(specs) == 30
    assert {spec.split for spec in specs} == {"train", "validation"}
    assert specs[-1].run_id == "34159449923"


def test_manifest_has_broad_fixed_evidence_counts() -> None:
    manifest = json.loads((ROOT / research.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["totals"] == {
        "captures": 33,
        "launches": 99_000,
        "sealed_holdout_launches": 9_000,
        "train_launches": 81_000,
        "validation_launches": 9_000,
    }
    assert all(row["capture_errors"] == 0 for row in manifest["new_captures"])


def test_development_audit_never_opened_holdout() -> None:
    audit = artifact("data-audit")
    assert audit["holdout_opened"] is False
    assert set(audit["rows_by_split"]) == {"train", "validation"}
    assert len(audit["runs"]) == 30
    assert all(row["hash_match"] for row in audit["runs"])
    assert audit["future_values_in_features"] is False
    assert audit["production_paths_changed"] == 0


def test_frozen_candidate_passes_every_development_gate() -> None:
    search = artifact("development-search")
    frozen = artifact("frozen-candidate")
    metrics = frozen["candidate"]["validation"]
    assert search["frozen_candidate_ready"] is True
    assert frozen["status"] == "FROZEN_FOR_ONE_TIME_LATER_EPOCH_HOLDOUT"
    assert frozen["holdout_rows_read_before_freeze"] == 0
    assert research.robust_rank(metrics)[0] == 9
    assert metrics["capture_windows"] == 3
    assert metrics["net_pnl_sol"] > 0
    assert metrics["profit_factor"] >= 1.25


def test_experiment_identity_is_deterministic_and_complete() -> None:
    frozen = artifact("frozen-candidate")
    identity = frozen["identity"]
    assert frozen["experiment_id"] == f"e4x-{research.base.stable_hash(identity)}"
    assert identity["thesis_family_identifier"] == research.THESIS_FAMILY
    assert len(identity["chronological_split"]["holdout"]) == 3
    assert identity["fee_model"]["priority_fee_sol"] == 0.001
    assert frozen["production_paths_changed"] == 0


def test_development_replay_ledger_is_frozen() -> None:
    search = artifact("development-search")
    frozen = artifact("frozen-candidate")
    assert search["holdout_rows_read"] == 0
    assert search["winner"]["validation"]["ledger_hash"] == frozen["development_ledger_hash"]
