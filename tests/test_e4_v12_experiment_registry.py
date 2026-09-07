from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts import e4_v12_24x7_watchdog as watchdog
from scripts import e4_v12_experiment_registry as registry

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def identity(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "thesis_family_identifier": "family-a",
        "source_code_fingerprint": "source-1",
        "dataset_source_manifest_fingerprint": "dataset-1",
        "evidence_epoch": "epoch-1",
        "feature_set_fingerprint": "features-1",
        "model_family": "model-a",
        "full_parameters": {"depth": 3, "threshold": 0.7},
        "causal_horizon": {"milliseconds": 500},
        "candidate_risk_set_policy": "visible-and-executable",
        "chronological_split": {"train": 0.6, "validation": 0.2, "holdout": 0.2},
        "bankroll": {"sol": 3.0},
        "position_sizing": {"sol": 0.1},
        "fee_model": {"base_bps": 100},
        "output_guard": {"shortfall_bps": 800},
        "latency_assumptions": {"milliseconds": [0, 1, 2, 5, 10]},
        "execution_policy": "paper-replay",
        "exit_policy": "causal-exit-v1",
    }
    value.update(changes)
    return value


def metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_commit": "abc123",
        "dataset_version": "dataset-v1",
        "state": "COMPLETED_FAILED",
        "failure_classification": "HOLDOUT_COLLAPSE",
        "retired": True,
        "retirement_reason": "holdout collapsed",
        "material_change_required_before_rerun": "new data or thesis",
        "exact_artifact_paths": ["artifact://report.json"],
    }
    value.update(changes)
    return value


def run_row(
    *,
    run_id: int = 7,
    status: str = "completed",
    conclusion: str = "failure",
    age_minutes: int = 5,
) -> dict[str, object]:
    timestamp = registry.iso_timestamp(NOW - timedelta(minutes=age_minutes))
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "head_sha": "head-1",
        "created_at": timestamp,
        "updated_at": timestamp,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


class FakeAPI:
    def __init__(self, target: watchdog.Target, runs: list[dict[str, object]]) -> None:
        self.target = target
        self.runs = runs
        self.cancelled: list[int] = []
        self.dispatched: list[tuple[str, str]] = []
        self.status: dict[str, object] | None = {"thesis_status": "NOT_CONCLUSIVE"}
        workflow_path = f".github/workflows/{target.workflow}"
        self.files: dict[tuple[str, str], str] = {
            (target.ref, workflow_path): "run: python thesis.py --threshold 0.7\n",
        }
        self.tree = {
            workflow_path: "workflow-blob",
            "scripts/thesis.py": "source-blob",
            "src/memecoin_bot/authority.py": "authority-blob",
            watchdog.EVIDENCE_DATA_PATH: "evidence-blob",
            watchdog.EVIDENCE_EPOCH_PATH: "epoch-blob",
        }
        self.path_commit: dict[str, object] | None = {
            "commit": {"committer": {"date": registry.iso_timestamp(NOW)}}
        }

    def branch_head(self, _ref: str) -> str:
        return "head-1"

    def branch_tree(self, _ref: str) -> dict[str, str]:
        return self.tree

    def file_text(self, path: str, ref: str) -> str | None:
        return self.files.get((ref, path))

    def json_file(self, path: str | None, ref: str) -> dict[str, object] | None:
        if path and (text := self.files.get((ref, path))):
            value = json.loads(text)
            return dict(value) if isinstance(value, dict) else None
        return self.status if path else None

    def workflow_runs(self, _workflow: str, _ref: str) -> list[dict[str, object]]:
        return self.runs

    def latest_path_commit(self, _path: str, _ref: str) -> dict[str, object] | None:
        return self.path_commit

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)

    def dispatch(self, workflow: str, ref: str) -> None:
        self.dispatched.append((workflow, ref))


def target(**changes: object) -> watchdog.Target:
    values: dict[str, object] = {
        "workflow": "golden.yml",
        "ref": "research",
        "role": "test-family",
        "priority": 1,
        "success_cooldown_minutes": 60,
        "failure_backoff_minutes": 20,
        "stale_active_minutes": 120,
        "thesis_family": "test-family",
        "model_family": "test-model",
        "feature_contract": "test-features",
        "status_path": "status.json",
    }
    values.update(changes)
    return watchdog.Target(**values)


def evidence(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "dataset_version": "dataset-v1",
        "dataset_source_manifest_fingerprint": "dataset-fingerprint-1",
        "evidence_epoch": "epoch-1",
        "latest_successful_evidence_at": NOW - timedelta(minutes=10),
        "source_manifest": {},
        "source_runs": [],
    }
    values.update(changes)
    return values


class ExperimentIdentityTests(unittest.TestCase):
    def test_identical_experiments_get_identical_ids(self) -> None:
        first = identity(full_parameters={"threshold": 0.7, "depth": 3})
        second = identity(full_parameters={"depth": 3, "threshold": 0.7})
        self.assertEqual(registry.experiment_id(first), registry.experiment_id(second))

    def test_one_parameter_change_creates_new_id(self) -> None:
        first = identity()
        second = identity(full_parameters={"depth": 4, "threshold": 0.7})
        self.assertNotEqual(
            registry.experiment_id(first), registry.experiment_id(second)
        )

    def test_one_dataset_change_creates_new_id(self) -> None:
        first = identity()
        second = identity(dataset_source_manifest_fingerprint="dataset-2")
        self.assertNotEqual(
            registry.experiment_id(first), registry.experiment_id(second)
        )

    def test_committed_evidence_fingerprint_ignores_scheduler_run_ids(self) -> None:
        selected = target(role="fresh-evidence")
        first_api = FakeAPI(selected, [run_row(run_id=1)])
        second_api = FakeAPI(selected, [run_row(run_id=999)])
        first_api.files[(selected.ref, watchdog.EVIDENCE_EPOCH_PATH)] = "epoch-a"
        second_api.files[(selected.ref, watchdog.EVIDENCE_EPOCH_PATH)] = "epoch-a"

        first = watchdog.evidence_snapshot(first_api, selected)
        second = watchdog.evidence_snapshot(second_api, selected)

        self.assertEqual(
            first["dataset_source_manifest_fingerprint"],
            second["dataset_source_manifest_fingerprint"],
        )
        self.assertEqual(first["evidence_epoch"], second["evidence_epoch"])

    def test_only_rolling_risk_sets_change_with_new_evidence(self) -> None:
        frozen = target()
        frozen_api = FakeAPI(frozen, [])
        workflow_path = f".github/workflows/{frozen.workflow}"
        frozen_api.files[(frozen.ref, workflow_path)] = (
            "run: gh run download 33792881280 --name e4-v12-forward-33792881280\n"
        )
        older = evidence(source_runs=[{"run_id": 10, "head_sha": "a"}])
        newer = evidence(source_runs=[{"run_id": 11, "head_sha": "b"}])
        frozen_old = watchdog.experiment_spec(frozen_api, frozen, older)
        frozen_new = watchdog.experiment_spec(frozen_api, frozen, newer)
        self.assertEqual(
            registry.experiment_id(frozen_old.identity),
            registry.experiment_id(frozen_new.identity),
        )

        rolling = target()
        rolling_api = FakeAPI(rolling, [])
        rolling_path = f".github/workflows/{rolling.workflow}"
        rolling_api.files[(rolling.ref, rolling_path)] = (
            "run: gh run list --workflow e4-v12-selection-certification.yml\n"
        )
        rolling_old = watchdog.experiment_spec(rolling_api, rolling, older)
        rolling_new = watchdog.experiment_spec(rolling_api, rolling, newer)
        self.assertNotEqual(
            registry.experiment_id(rolling_old.identity),
            registry.experiment_id(rolling_new.identity),
        )

    def test_all_declared_failure_classes_are_validated(self) -> None:
        self.assertEqual(len(registry.FAILURE_CLASSES), 15)
        with self.assertRaisesRegex(ValueError, "unknown failure"):
            registry.new_record(
                identity(), metadata(failure_classification="UNCLASSIFIED")
            )


class RegistryDurabilityTests(unittest.TestCase):
    def test_registry_write_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "registry.json"
            store = registry.RegistryStore(path, clock=lambda: NOW)
            original_replace = os.replace
            with patch.object(
                registry.os, "replace", wraps=original_replace
            ) as replace:
                store.register(identity(), metadata())
            self.assertTrue(replace.called)
            registry.validate_registry(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_concurrent_workflows_cannot_create_duplicate_records(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = registry.RegistryStore(
                Path(directory) / "registry.json", clock=lambda: NOW
            )

            def register_once(_: int) -> bool:
                return store.register(identity(), metadata())[1]

            with ThreadPoolExecutor(max_workers=8) as pool:
                created = list(pool.map(register_once, range(32)))
            document = store.load()
            self.assertEqual(sum(created), 1)
            self.assertEqual(len(document["records"]), 1)

    def test_leaderboard_places_holdout_above_train_only(self) -> None:
        first = registry.new_record(
            identity(thesis_family_identifier="train-only"),
            metadata(
                failure_classification="NO_TRAIN_SURVIVOR",
                train_metrics={"wr": 0.99},
            ),
            now=NOW,
        )
        second = registry.new_record(
            identity(thesis_family_identifier="holdout"),
            metadata(chronological_holdout_metrics={"wr": 0.51, "pnl": 0.1}),
            now=NOW,
        )
        rows = registry.leaderboard_rows(
            {"version": registry.REGISTRY_VERSION, "records": [first, second]}
        )
        self.assertEqual(rows[0]["thesis_family"], "holdout")


class RetryPolicyTests(unittest.TestCase):
    def test_scientific_duplicates_are_not_dispatched(self) -> None:
        selected = target()
        api = FakeAPI(selected, [run_row()])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = registry.RegistryStore(
                Path(directory) / "registry.json", clock=lambda: NOW
            )
            action = watchdog.scientific_action(
                api,
                store,
                selected,
                evidence(latest_successful_evidence_at=NOW - timedelta(minutes=20)),
                now=NOW,
                integrity_passed=True,
                dry_run=False,
            )
            self.assertEqual(action.decision, "retire_duplicate")
            self.assertFalse(action.dispatched)
            self.assertEqual(action.failure_classification, "DUPLICATE_EXPERIMENT")
            self.assertEqual(len(store.load()["records"]), 1)

    def test_infrastructure_failures_may_retry_after_backoff(self) -> None:
        record = registry.new_record(
            identity(),
            metadata(
                state="INFRASTRUCTURE_FAILED",
                failure_classification="INFRASTRUCTURE_FAILURE",
                retired=False,
            ),
            now=NOW - timedelta(minutes=30),
        )
        decision, _ = registry.retry_decision(
            record,
            now=NOW,
            infrastructure_backoff_minutes=20,
            stale_active_minutes=120,
        )
        self.assertEqual(decision, "ELIGIBLE_INFRASTRUCTURE_RETRY")

    def test_new_evidence_makes_affected_experiment_eligible(self) -> None:
        old_identity = identity()
        new_identity = identity(
            dataset_source_manifest_fingerprint="dataset-2", evidence_epoch="epoch-2"
        )
        document = {
            "version": registry.REGISTRY_VERSION,
            "records": [registry.new_record(old_identity, metadata(), now=NOW)],
        }
        new_record = registry.record_by_id(
            document, registry.experiment_id(new_identity)
        )
        decision, _ = registry.retry_decision(
            new_record,
            now=NOW,
            infrastructure_backoff_minutes=20,
            stale_active_minutes=120,
        )
        self.assertEqual(decision, "ELIGIBLE_NOVEL")

    def test_evidence_arriving_after_run_start_is_not_backfilled(self) -> None:
        selected = target()
        completed = run_row(age_minutes=10)
        completed["updated_at"] = registry.iso_timestamp(NOW)
        api = FakeAPI(selected, [completed])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = registry.RegistryStore(
                Path(directory) / "registry.json", clock=lambda: NOW
            )
            action = watchdog.scientific_action(
                api,
                store,
                selected,
                evidence(
                    latest_successful_evidence_at=NOW - timedelta(minutes=5),
                    source_manifest={"files": {"evidence": "new"}},
                ),
                now=NOW,
                integrity_passed=True,
                dry_run=False,
            )
            self.assertEqual(action.decision, "dispatch")
            self.assertEqual(store.load()["records"], [])

    def test_stale_active_jobs_are_restarted(self) -> None:
        record = registry.new_record(
            identity(),
            metadata(state="IN_PROGRESS", failure_classification=None, retired=False),
            now=NOW - timedelta(minutes=121),
        )
        decision, _ = registry.retry_decision(
            record,
            now=NOW,
            infrastructure_backoff_minutes=20,
            stale_active_minutes=120,
        )
        self.assertEqual(decision, "ELIGIBLE_STALE_RESTART")

        selected = target()
        stale_run = run_row(
            run_id=44,
            status="in_progress",
            conclusion="",
            age_minutes=121,
        )
        api = FakeAPI(selected, [stale_run])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = registry.RegistryStore(
                Path(directory) / "registry.json", clock=lambda: NOW
            )
            action = watchdog.scientific_action(
                api,
                store,
                selected,
                evidence(latest_successful_evidence_at=NOW - timedelta(minutes=130)),
                now=NOW,
                integrity_passed=True,
                dry_run=False,
            )
            self.assertEqual(action.decision, "restart")
            self.assertEqual(action.cancelled_run_ids, [44])

    def test_integrity_failure_blocks_novel_scientific_dispatch(self) -> None:
        selected = target()
        api = FakeAPI(selected, [])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = registry.RegistryStore(
                Path(directory) / "registry.json", clock=lambda: NOW
            )
            action = watchdog.scientific_action(
                api,
                store,
                selected,
                evidence(),
                now=NOW,
                integrity_passed=False,
                dry_run=False,
            )
            self.assertEqual(action.decision, "blocked_integrity")
            self.assertEqual(action.failure_classification, "DATA_INTEGRITY_FAILURE")

    def test_integrity_failure_cannot_satisfy_stop_gate(self) -> None:
        self.assertFalse(watchdog.stop_gate({"passed": False}, True))
        self.assertFalse(watchdog.stop_gate({"passed": True}, False))
        self.assertTrue(watchdog.stop_gate({"passed": True}, True))


class SafetyBoundaryTests(unittest.TestCase):
    def test_authoritative_production_v12_is_untouched(self) -> None:
        changed: set[str] = set()
        for base in ("origin/main", "HEAD^"):
            process = subprocess.run(
                ["git", "diff", "--name-only", base, "--"],
                check=False,
                capture_output=True,
                text=True,
            )
            if process.returncode == 0:
                changed.update(process.stdout.splitlines())
        protected = {
            path
            for path in changed
            if path.startswith(("src/memecoin_bot/", "models/e4/", "tools/e4-builder/"))
        }
        self.assertEqual(protected, set())
        self.assertNotEqual(watchdog.RESEARCH_REF, "main")
        self.assertTrue(all(target.ref != "main" for target in watchdog.TARGETS[1:]))

    def test_live_stop_requires_sentinel_and_untouched_live_verdict(self) -> None:
        selected = target()
        api = FakeAPI(selected, [])
        api.files[(watchdog.RESEARCH_REF, watchdog.SENTINEL_PATHS[0])] = (
            "FOUND_LIVE_SUB10MS_GOLDEN_THESIS"
        )
        api.status = None
        passed, _ = watchdog.untouched_live_gate(api)
        self.assertFalse(passed)
        verdict_path = watchdog.LIVE_VERDICT_PATHS[0]
        api.files[(watchdog.RESEARCH_REF, verdict_path)] = json.dumps(
            {
                "status": "FOUND_LIVE_SUB10MS_GOLDEN_THESIS",
                "untouched_live": True,
                "golden_gate_passed": True,
            }
        )
        passed, _ = watchdog.untouched_live_gate(api)
        self.assertTrue(passed)

    def test_fresh_evidence_is_not_a_scientific_experiment(self) -> None:
        self.assertFalse(watchdog.TARGETS[0].scientific)
        self.assertTrue(all(target.scientific for target in watchdog.TARGETS[1:]))


if __name__ == "__main__":
    unittest.main()
