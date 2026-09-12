#!/usr/bin/env python3
"""Validate one downloaded live-capture artifact and register it atomically."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import e4_v12_causal_regime_veto_holdout as holdout
import e4_v12_profit_survival_search as base

SCHEMA_VERSION = "e4-v12-holdout-capture-registration-v1"
EXPECTED_COPY_AUDIT_LATENCY_MS = 36.0


def epoch_ns(value: str) -> int:
    normalised = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        raise ValueError("workflow start time must include a timezone")
    return int(parsed.astimezone(UTC).timestamp() * 1_000_000_000)


def count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for count, _ in enumerate(handle, 1):
            pass
    return count


def relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"artifact path escapes repository: {path}")
    return str(resolved.relative_to(root)).replace("\\", "/")


def artifact_file(artifact_dir: Path, name: str) -> Path:
    candidates = (artifact_dir / "artifacts" / name, artifact_dir / name)
    matches = [path.resolve() for path in candidates if path.is_file()]
    if len(matches) != 1:
        raise ValueError(f"expected one {name} under {artifact_dir}, got {len(matches)}")
    return matches[0]


def capture_record(
    root: Path,
    artifact_dir: Path,
    run_id: str,
    workflow_started_at: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    batch_path = artifact_file(artifact_dir, "e4-v12-forward-batch.json")
    events_path = artifact_file(
        artifact_dir,
        "e4-v12-forward-batch-live-events.jsonl",
    )
    copy_audit_path = artifact_file(artifact_dir, "e4-v12-copy-audit.json")
    batch = holdout.read_json(batch_path)
    copy_audit = holdout.read_json(copy_audit_path)
    capture = batch.get("capture")
    if not isinstance(capture, dict):
        raise TypeError("batch capture is not an object")
    cohort = capture.get("cohort")
    if not isinstance(cohort, list):
        raise TypeError("batch cohort is not a list")
    launch_count = int(capture.get("unique_launches", 0))
    required = int(protocol["final_evidence_contract"]["launches_per_window"])
    if launch_count != required or len(cohort) != required:
        raise ValueError("batch does not contain the required 3000 launches")
    mints = [str(row.get("mint", "")) for row in cohort if isinstance(row, dict)]
    if len(mints) != required or "" in mints or len(set(mints)) != required:
        raise ValueError("batch cohort mints are missing or duplicated")
    if not capture.get("target_reached"):
        raise ValueError("capture target was not reached")
    errors = capture.get("errors")
    if not isinstance(errors, list) or errors:
        raise ValueError("capture contains provider/runtime errors")
    decoded_events = int(capture.get("decoded_events", 0))
    if count_lines(events_path) != decoded_events:
        raise ValueError("event file line count does not match the batch")
    if batch.get("hypothesis_only") is not True:
        raise ValueError("capture was not marked hypothesis-only")
    if int(batch.get("mainnet_transactions_sent", -1)) != 0:
        raise ValueError("capture sent mainnet transactions")
    if float(batch.get("mainnet_funds_risked_sol", -1)) != 0:
        raise ValueError("capture risked mainnet funds")
    if str(copy_audit.get("source_run")) != run_id:
        raise ValueError("copy audit source run differs from workflow run")
    if int(copy_audit.get("fresh_launches", 0)) != required:
        raise ValueError("copy audit launch count differs from the batch")
    if float(copy_audit.get("latency_ms", -1)) != EXPECTED_COPY_AUDIT_LATENCY_MS:
        raise ValueError("copy audit was not produced at the frozen 36 ms latency")
    starts = [int(row["received_ns"]) for row in cohort]
    final_progress = capture.get("final_progress")
    if not isinstance(final_progress, dict):
        raise TypeError("capture final progress is not an object")
    capture_end_ns = int(final_progress.get("timestamp_ns", 0))
    capture_start_ns = min(starts)
    if capture_end_ns < max(starts):
        raise ValueError("capture end precedes a launch observation")
    workflow_start_ns = epoch_ns(workflow_started_at)
    freeze_ns = int(protocol["frozen_candidate"]["frozen_at_epoch_ns"])
    role = (
        holdout.FINAL_ROLE
        if workflow_start_ns > freeze_ns and capture_start_ns > freeze_ns
        else holdout.QUARANTINE_ROLE
    )
    return {
        "registration_version": SCHEMA_VERSION,
        "run_id": run_id,
        "role": role,
        "artifact_name": f"e4-v12-forward-{run_id}",
        "workflow_conclusion": "success",
        "workflow_run_started_at": workflow_started_at,
        "workflow_run_started_at_epoch_ns": workflow_start_ns,
        "capture_start_ns": capture_start_ns,
        "capture_end_ns": capture_end_ns,
        "source_commit": str(batch.get("commit", "")),
        "launches": launch_count,
        "decoded_events": decoded_events,
        "capture_errors": len(errors),
        "events_path": relative(root, events_path),
        "events_sha256": base.sha256_path(events_path),
        "batch_path": relative(root, batch_path),
        "batch_sha256": base.sha256_path(batch_path),
        "copy_audit_path": relative(root, copy_audit_path),
        "copy_audit_sha256": base.sha256_path(copy_audit_path),
        "hypothesis_only": True,
        "mainnet_transactions_sent": 0,
        "mainnet_funds_risked_sol": 0,
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialised)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def register(
    root: Path,
    manifest_path: Path,
    record: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    if manifest_path.exists():
        manifest = holdout.read_json(manifest_path)
    else:
        manifest = {
            "version": holdout.MANIFEST_VERSION,
            "experiment_id": protocol["experiment_id"],
            "captures": [],
        }
    captures = [dict(row) for row in manifest.get("captures", [])]
    existing = [row for row in captures if str(row.get("run_id")) == record["run_id"]]
    if existing:
        if existing != [record]:
            raise ValueError("run ID is already registered with different content")
        return manifest
    captures.append(record)
    candidate = dict(manifest) | {"captures": captures}
    validated = holdout.validate_manifest(root, candidate, protocol)
    candidate["captures"] = validated
    atomic_json(manifest_path, candidate)
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow-started-at", required=True)
    parser.add_argument("--manifest", type=Path, default=holdout.DEFAULT_MANIFEST_PATH)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    artifact_dir = (
        args.artifact_dir.resolve()
        if args.artifact_dir.is_absolute()
        else (root / args.artifact_dir).resolve()
    )
    manifest_path = (
        args.manifest.resolve()
        if args.manifest.is_absolute()
        else (root / args.manifest).resolve()
    )
    protocol = holdout.verify_protocol(root)
    record = capture_record(
        root,
        artifact_dir,
        str(args.run_id),
        args.workflow_started_at,
        protocol,
    )
    manifest = register(root, manifest_path, record, protocol)
    print(
        json.dumps(
            {
                "run_id": record["run_id"],
                "role": record["role"],
                "events_sha256": record["events_sha256"],
                "registered_captures": len(manifest["captures"]),
                "manifest_sha256": base.sha256_path(manifest_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
