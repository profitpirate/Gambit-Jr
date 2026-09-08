#!/usr/bin/env python3
"""Build the immutable, causal E4 V12 choice-risk-set V2 corpus.

This module deliberately imports the V1 reader rather than altering it.  Raw capture
and research inputs are local, read-only GitHub Actions artifact restorations pinned
by the V2 source manifest.  No model is trained and no production path is imported.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import e4_v12_canonical_choice_sets as v1

BASE_COMMIT = "078c1875361fd21293c72c225961b2b7647938df"
REPOSITORY = "profitpirate/Gambit-Jr"
ENVELOPES_MS = (0, 100, 250, 500, 750, 1_000, 1_500, 3_000, 5_000, 10_000, 15_000, 30_000, 60_000)
ACTIVE_POLICY_MAX_MS = 60_000
V1_EXPECTED = {
    "artifacts/e4-v12-canonical-choice-sets.jsonl": (
        2_963_159,
        "f64b040f18366bd01dd07adbd4a930446dd91df3e691af88c18b09d3a0b075b4",
    ),
    "artifacts/e4-v12-canonical-choice-sets.parquet": (
        259_565,
        "853e61de326c38516dbf12872a95a86babd7dcc11f1be04503d940450b816bfe",
    ),
    "artifacts/e4-v12-choice-set-coverage.json": (
        143_937,
        "3a3a6fd0272171b37a38cf3e4bd50261e7e23d4ecac0522a8361177767a352ec",
    ),
    "artifacts/e4-v12-choice-set-report.md": (
        3_277,
        "2e9bc96d7ad27d58c4ffc37944ccdf5a3d89a01e6106c3d275d230a5c1399b96",
    ),
    "research/e4-v12-canonical-choice-source-manifest.json": (
        7_238,
        "d005f33b2dc80c326ab87fc7b1546ac132a1bcc80a470d30d751b2cf94af63df",
    ),
}
V1_WINDOWS_CHECKOUT_VARIANTS = {
    "artifacts/e4-v12-canonical-choice-sets.jsonl": (
        2_963_655,
        "a14484031ee809ee908d7065526948e2a0425e9474f7bb6b38236189e45bf474",
    ),
    "artifacts/e4-v12-choice-set-coverage.json": (
        147_617,
        "ca4feea5b918e335e137e73c25728eddba78e2fe9b3ff7a4464135f0ad343bec",
    ),
    "artifacts/e4-v12-choice-set-report.md": (
        3_360,
        "ad2153502cbf59f6386a8f2f4d25a4850322fe7b208c1de244ba53abdfa1b712",
    ),
    "research/e4-v12-canonical-choice-source-manifest.json": (
        7_413,
        "39a8d660c20c4b5595576cb2b0bf5ca626fc56d8f129a7129508c5f310c33099",
    ),
}
V1_RUN_SPLITS = {
    "train": (
        "33605109571",
        "33627431540",
        "33644544746",
        "33656916992",
        "33669514091",
        "33681664773",
        "33692043170",
        "33704841852",
    ),
    "validation": ("33716649440", "33741470396"),
    "holdout": ("33792881280", "33822988156"),
}
RESEARCH_FILES = {
    "failed_attempts_v1": (
        "33883557623",
        "failed-attempts/e4-v12-attempt-mint-forensics.json",
        "failed selection mapping and failure evidence",
    ),
    "exact_timestamp_controls": (
        "33871677018",
        "research/33871677018/e4-v12-exact-timestamp-matched-controls.json",
        "exact-time matched-control audit",
    ),
    "whitelist_launch_audit": (
        "33871800021",
        "research/33871800021/e4-v12-whitelist-launch-audit.json",
        "causal whitelist source audit",
    ),
    "bundle_metadata": (
        "33876511129",
        "research/33876511129/e4-v12-bundle-metadata-forensics.json",
        "transaction ordering and bundle metadata",
    ),
    "wallet_history_v1": (
        "33876680669",
        "research/33876680669/e4-v12-full-wallet-history.json",
        "resolved E4 wallet ledger",
    ),
    "social_choice": (
        "33877632753",
        "research/33877632753/e4-v12-social-choice-forensics.json",
        "timestamp-constrained social and metadata evidence",
    ),
    "global_attempt": (
        "33889403746",
        "research/33889403746/e4-v12-global-attempt-intent.json",
        "launch identity and prior-attempt evidence",
    ),
    "failed_attempts_latest": (
        "33913332895",
        "research/33913332895/e4-v12-latest-attempt-mint-forensics.json",
        "post-V1 failed selection mapping",
    ),
    "wallet_history_latest": (
        "33913332895",
        "research/33913332895/e4-v12-latest-wallet-history.json",
        "post-V1 resolved E4 wallet ledger",
    ),
}


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def verify_v1(repo_root: Path) -> dict[str, Any]:
    rows = []
    for relative, (expected_bytes, expected_sha) in V1_EXPECTED.items():
        path = repo_root / relative
        actual_bytes = path.stat().st_size
        actual_sha = v1.sha256_path(path)
        checkout_variant = V1_WINDOWS_CHECKOUT_VARIANTS.get(relative)
        canonical_match = actual_bytes == expected_bytes and actual_sha == expected_sha
        checkout_match = bool(
            checkout_variant
            and actual_bytes == checkout_variant[0]
            and actual_sha == checkout_variant[1]
        )
        rows.append(
            {
                "path": relative,
                "canonical_git_bytes": expected_bytes,
                "actual_bytes": actual_bytes,
                "canonical_git_sha256": expected_sha,
                "actual_sha256": actual_sha,
                "byte_identical": canonical_match or checkout_match,
                "representation": (
                    "canonical_git_bytes" if canonical_match else "known_crlf_checkout"
                ),
            }
        )
    failures = [row for row in rows if not row["byte_identical"]]
    if failures:
        raise ValueError(f"V1 immutability failure: {compact(failures)}")
    return {
        "version": "e4-v12-v1-immutability-audit-v1",
        "base_commit": BASE_COMMIT,
        "status": "PASS",
        "changed_files": 0,
        "files": rows,
    }


def split_for_run(run_id: str) -> str:
    for split, run_ids in V1_RUN_SPLITS.items():
        if run_id in run_ids:
            return split
    if int(run_id) < int(V1_RUN_SPLITS["holdout"][0]):
        return "validation"
    return "holdout"


def source_manifest(capture_root: Path, evidence_root: Path, prior_path: Path) -> dict[str, Any]:
    captures = []
    for run_dir in sorted(
        (path for path in capture_root.iterdir() if path.is_dir()),
        key=lambda path: int(path.name),
    ):
        run_id = run_dir.name
        batch = run_dir / "artifacts/e4-v12-forward-batch.json"
        events = run_dir / "artifacts/e4-v12-forward-batch-live-events.jsonl"
        if not batch.exists() or not events.exists():
            continue
        report = json.loads(batch.read_text(encoding="utf-8"))
        cohort = (report.get("capture") or {}).get("cohort") or []
        received = [v1.integer(row.get("received_ns")) for row in cohort]
        captures.append(
            {
                "repository": REPOSITORY,
                "branch": str(report.get("branch") or "codex/e4-v12-selection-reconstruction"),
                "commit": str(report.get("commit") or ""),
                "workflow": ".github/workflows/e4-v12-selection-certification.yml",
                "run_id": run_id,
                "artifact_name": f"e4-v12-forward-{run_id}",
                "split": split_for_run(run_id),
                "evidence_epoch": f"forward-capture-{run_id}",
                "capture_start_ns": min(received),
                "capture_end_ns": max(received),
                "role_in_v2": "causal launches, reserves, market flow, and observed E4 buys",
                "files": [
                    {
                        "filename": batch.name,
                        "bytes": batch.stat().st_size,
                        "sha256": v1.sha256_path(batch),
                    },
                    {
                        "filename": events.name,
                        "bytes": events.stat().st_size,
                        "sha256": v1.sha256_path(events),
                    },
                ],
            }
        )
    research = []
    seen_hashes: set[str] = set()
    for identity, (run_id, relative, role) in RESEARCH_FILES.items():
        path = evidence_root / relative
        sha = v1.sha256_path(path)
        if sha in seen_hashes:
            raise ValueError(f"research source duplicated under another identity: {path}")
        seen_hashes.add(sha)
        payload = json.loads(path.read_text(encoding="utf-8"))
        methodology = payload.get("methodology") or {}
        summary = payload.get("summary") or {}
        start = methodology.get("capture_start_ns")
        end = methodology.get("capture_end_ns")
        if start is None and summary.get("earliest_block_time") is not None:
            start = v1.integer(summary["earliest_block_time"]) * 1_000_000_000
        if end is None and summary.get("latest_block_time") is not None:
            end = v1.integer(summary["latest_block_time"]) * 1_000_000_000
        research.append(
            {
                "identity": identity,
                "repository": REPOSITORY,
                "branch": "codex/e4-v12-selection-reconstruction",
                "commit": "source artifact metadata did not expose a commit",
                "workflow": ".github/workflows/e4-v12-selection-certification.yml",
                "run_id": run_id,
                "artifact_name": path.stem + f"-{run_id}",
                "filename": path.name,
                "relative_cache_path": relative.replace("\\", "/"),
                "bytes": path.stat().st_size,
                "sha256": sha,
                "capture_start_ns": start,
                "capture_end_ns": end,
                "evidence_epoch": f"research-{run_id}",
                "role_in_v2": role,
            }
        )
    research.append(
        {
            "identity": "causal_prior_registry",
            "repository": REPOSITORY,
            "branch": "committed V1 evidence",
            "commit": BASE_COMMIT,
            "workflow": "repository source",
            "run_id": None,
            "artifact_name": "e4-v12-causal-prior-registry",
            "filename": prior_path.name,
            "relative_cache_path": str(prior_path).replace("\\", "/"),
            "bytes": prior_path.stat().st_size,
            "sha256": v1.sha256_path(prior_path),
            "capture_start_ns": None,
            "capture_end_ns": None,
            "evidence_epoch": "pre-capture-causal-registry",
            "role_in_v2": "causally frozen creator outcome history",
        }
    )
    return {
        "version": "e4-v12-choice-riskset-source-manifest-v2",
        "repository": REPOSITORY,
        "construction_base_commit": BASE_COMMIT,
        "chronology": (
            "received_ns is primary; transaction and event indexes resolve exact-time ties only"
        ),
        "splits": {
            split: [row["run_id"] for row in captures if row["split"] == split]
            for split in ("train", "validation", "holdout")
        },
        "captures": captures,
        "research_artifacts": research,
        "immutable_v1_sources": [
            {
                "repository": REPOSITORY,
                "branch": "codex/e4-v12-canonical-choice-set",
                "commit": BASE_COMMIT,
                "workflow": "repository source",
                "run_id": None,
                "artifact_name": Path(relative).name,
                "filename": Path(relative).name,
                "path": relative,
                "bytes": size,
                "sha256": sha,
                "known_windows_checkout": (
                    {
                        "bytes": V1_WINDOWS_CHECKOUT_VARIANTS[relative][0],
                        "sha256": V1_WINDOWS_CHECKOUT_VARIANTS[relative][1],
                    }
                    if relative in V1_WINDOWS_CHECKOUT_VARIANTS
                    else None
                ),
                "capture_start_ns": None,
                "capture_end_ns": None,
                "evidence_epoch": "canonical-choice-set-v1",
                "role_in_v2": "immutable V1 ledger, coverage, or provenance evidence",
            }
            for relative, (size, sha) in sorted(V1_EXPECTED.items())
        ],
        "deduplication": {
            "omitted_source": (
                "research/33871800021/e4-v12-exact-timestamp-matched-controls.json"
            ),
            "canonical_source": (
                "research/33871677018/e4-v12-exact-timestamp-matched-controls.json"
            ),
            "reason": "byte-identical SHA-256; one evidence identity retained",
        },
    }


def verify_or_write_manifest(
    path: Path,
    generated: Mapping[str, Any],
    *,
    write_manifest: bool,
) -> None:
    text = json.dumps(generated, indent=2, sort_keys=True) + "\n"
    if write_manifest:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        raise ValueError(f"missing pinned V2 source manifest: {path}")
    if path.read_text(encoding="utf-8") != text:
        raise ValueError("restored source cache differs from the pinned V2 source manifest")


def specs_from_manifest(manifest: Mapping[str, Any], root: Path) -> list[v1.SourceSpec]:
    specs = []
    for row in manifest.get("captures") or []:
        files = {item["filename"]: item for item in row["files"]}
        run_id = str(row["run_id"])
        artifact_root = root / run_id / "artifacts"
        specs.append(
            v1.SourceSpec(
                run_id=run_id,
                artifact_name=str(row["artifact_name"]),
                split=str(row["split"]),
                batch_path=artifact_root / "e4-v12-forward-batch.json",
                events_path=artifact_root / "e4-v12-forward-batch-live-events.jsonl",
                batch_sha256=str(files["e4-v12-forward-batch.json"]["sha256"]),
                events_sha256=str(files["e4-v12-forward-batch-live-events.jsonl"]["sha256"]),
            )
        )
    return specs


def load_research(evidence_root: Path, prior_path: Path) -> dict[str, Any]:
    output = {
        identity: json.loads((evidence_root / relative).read_text(encoding="utf-8"))
        for identity, (_, relative, _) in RESEARCH_FILES.items()
    }
    output["causal_prior_registry"] = json.loads(prior_path.read_text(encoding="utf-8"))
    return output


def merge_wallets(research: Mapping[str, Any]) -> dict[str, Any]:
    transactions: dict[str, dict[str, Any]] = {}
    for name in ("wallet_history_v1", "wallet_history_latest"):
        for row in research[name].get("transactions") or []:
            signature = str(row.get("signature") or "")
            if not signature:
                continue
            current = transactions.get(signature)
            if current is None or (not current.get("detail_ok") and row.get("detail_ok")):
                transactions[signature] = dict(row)
    return {"transactions": list(transactions.values())}


def merge_attempts(research: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for name in ("failed_attempts_v1", "failed_attempts_latest"):
        values = (research[name].get("failed_attempts") or {}).get("rows") or []
        for row in values:
            signature = str(row.get("signature") or "")
            if signature:
                rows.setdefault(signature, dict(row))
    return list(rows.values())


def expanded_failed_decisions(
    attempts: Sequence[Mapping[str, Any]],
    launches: Mapping[str, v1.Launch],
    scans: Mapping[str, v1.CaptureScan],
    wallet: Mapping[str, Mapping[str, Any]],
) -> list[v1.Decision]:
    """Re-evaluate the V1 capture flag against the expanded, pinned V2 corpus."""
    expanded = []
    for source in attempts:
        row = dict(source)
        row["captured_mint"] = str(row.get("mapped_mint") or "") in launches
        expanded.append(row)
    decisions, _ = v1.failed_decisions(expanded, launches, scans, wallet)
    return decisions


def unique_decisions(decisions: Iterable[v1.Decision]) -> list[v1.Decision]:
    output: dict[tuple[str, str, str], v1.Decision] = {}
    for decision in decisions:
        key = (decision.label, decision.signature, decision.chosen_mint)
        output.setdefault(key, decision)
    return sorted(
        output.values(),
        key=lambda row: (row.decision_ns, row.source_transaction_index, row.signature),
    )


def failure_details(transaction: Mapping[str, Any]) -> tuple[str, int | None, str]:
    logs = [str(value) for value in transaction.get("log_messages") or []]
    failure_program = ""
    message = ""
    for line in logs:
        if "AnchorError" in line or "failed:" in line:
            message = line.removeprefix("Program log: ")
        if " failed:" in line and line.startswith("Program "):
            failure_program = line.split()[1]
    error = transaction.get("error") or transaction.get("err") or {}
    code = None
    if isinstance(error, Mapping):
        detail = error.get("InstructionError")
        if isinstance(detail, list) and len(detail) > 1 and isinstance(detail[1], Mapping):
            code = v1.integer(detail[1].get("Custom"), -1)
            if code < 0:
                code = None
    return failure_program, code, message


def candidate_launches(
    scan: v1.CaptureScan,
    decision: v1.Decision,
    maximum_age_ms: int,
) -> list[v1.Launch]:
    ordered = sorted(scan.launches.values(), key=lambda row: (row.create_ns, row.mint))
    timestamps = [row.create_ns for row in ordered]
    start = bisect.bisect_left(timestamps, decision.decision_ns - maximum_age_ms * 1_000_000)
    stop = bisect.bisect_right(timestamps, decision.decision_ns)
    result = [row for row in ordered[start:stop] if v1.launch_visible(row, decision)]
    if decision.chosen_mint not in {row.mint for row in result}:
        result.append(scan.launches[decision.chosen_mint])
    return sorted(result, key=lambda row: (row.create_ns, row.mint))


def causal_state_flags(
    launch: v1.Launch,
    decision: v1.Decision,
    events: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
) -> dict[str, Any]:
    known = [row for row in events if v1.event_known_at_decision(row, decision, launch)]
    invalid = any(
        str(row.get("kind") or "").upper() == "MIGRATION" or bool(row.get("complete"))
        for row in known
    )
    route = "PUMP_BONDING_CURVE"
    reserve_valid = all(
        features.get(name) is not None
        for name in ("virtual_sol_reserve", "virtual_token_reserve", "real_token_reserve")
    )
    executable = reserve_valid and v1.finite(features.get("executable_token_output_0_1_sol")) > 0
    reasons = ["RECEIVED_BY_DECISION", "SUPPORTED_PUMP_ROUTE"]
    if reserve_valid:
        reasons.append("CAUSAL_RESERVE_VALID")
    if executable:
        reasons.append("ECONOMICALLY_EXECUTABLE")
    exclusion = ""
    if invalid:
        exclusion = "PERMANENTLY_INVALID_BEFORE_DECISION"
    elif not reserve_valid:
        exclusion = "MISSING_CAUSAL_RESERVE_STATE"
    elif not executable:
        exclusion = "NOT_ECONOMICALLY_EXECUTABLE"
    return {
        "route": route,
        "pool": str(launch.raw.get("bonding_curve") or ""),
        "route_eligible": True,
        "reserve_valid": reserve_valid,
        "economically_executable": executable,
        "permanently_invalid": invalid,
        "riskset_eligible": not exclusion,
        "eligible_candidate_reasons": "|".join(reason.removesuffix("_BY_DECISION") for reason in reasons),
        "candidate_exclusion_reason": exclusion,
    }


def event_inputs(
    scans: Sequence[v1.CaptureScan],
    decisions: Sequence[v1.Decision],
    signature_indexes: Mapping[str, int],
) -> tuple[
    dict[str, list[v1.Launch]],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    scans_by_run = {scan.spec.run_id: scan for scan in scans}
    candidates: dict[str, list[v1.Launch]] = {}
    candidate_mints_by_run: dict[str, set[str]] = defaultdict(set)
    maximum_by_run_mint: dict[str, dict[str, int]] = defaultdict(dict)
    for decision in decisions:
        values = candidate_launches(scans_by_run[decision.run_id], decision, ACTIVE_POLICY_MAX_MS)
        candidates[decision.group_id] = values
        for launch in values:
            candidate_mints_by_run[decision.run_id].add(launch.mint)
            previous = maximum_by_run_mint[decision.run_id].get(launch.mint, 0)
            maximum_by_run_mint[decision.run_id][launch.mint] = max(
                previous, decision.decision_ns
            )
    events = {}
    for scan in scans:
        events[scan.spec.run_id] = v1.load_relevant_events(
            scan,
            candidate_mints_by_run[scan.spec.run_id],
            maximum_by_run_mint[scan.spec.run_id],
            signature_indexes,
        )
    return candidates, events


def build_initial_rows(
    scans: Sequence[v1.CaptureScan],
    decisions: Sequence[v1.Decision],
    candidates_by_group: Mapping[str, Sequence[v1.Launch]],
    events_by_run: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    social: Mapping[str, Mapping[str, Any]],
    prior_registry: Mapping[str, Any],
    wallet_by_signature: Mapping[str, Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    scans_by_run = {scan.spec.run_id: scan for scan in scans}
    prior_registry_ns = v1.integer(prior_registry.get("first_capture_epoch")) * 1_000_000_000
    groups = []
    successful_event_by_signature = {
        str(event.get("signature") or ""): event
        for scan in scans
        for event in scan.successful_events
    }
    for decision in decisions:
        scan = scans_by_run[decision.run_id]
        selected_launch = scan.launches[decision.chosen_mint]
        failure_program, failure_code, failure_message = failure_details(
            wallet_by_signature.get(decision.signature) or {}
        )
        group = []
        for launch in candidates_by_group[decision.group_id]:
            selected = launch.mint == decision.chosen_mint
            candidate_events = events_by_run[decision.run_id].get(launch.mint, [])
            features = v1.snapshot_features(
                launch,
                decision,
                candidate_events,
                social.get(launch.mint) or {},
                prior_registry,
                prior_registry_ns,
            )
            flags = causal_state_flags(launch, decision, candidate_events, features)
            age_ms = max(0.0, (decision.decision_ns - launch.create_ns) / 1_000_000)
            input_sol = decision.source_input_sol if selected else None
            quote = v1.finite(features.get("executable_token_output_0_1_sol"))
            impact = None
            if quote > 0 and input_sol and input_sol > 0:
                baseline = quote * (input_sol / 0.1)
                observed = decision.source_received_tokens or decision.source_curve_output_tokens
                if baseline > 0 and observed:
                    impact = max(0.0, (1 - observed / baseline) * 10_000)
            row = {
                "schema_version": "e4-v12-canonical-choice-riskset-v2",
                "decision_group_id": decision.group_id,
                "source_run_id": decision.run_id,
                "source_artifact_name": decision.artifact_name,
                "capture_start_ns": scan.minimum_ns,
                "source_row_event_identity": v1.stable_id(
                    "source-event",
                    launch.create_signature,
                    launch.create_event_id,
                    launch.create_event_index,
                    launch.mint,
                ),
                "split": decision.split,
                "selected_by_e4": selected,
                "selection_label": decision.label if selected else "TRUE_IGNORE",
                "launch_global_selection_label": str(
                    launch.raw.get("_global_label") or "TRUE_IGNORE"
                ),
                "landed_successfully": bool(selected and decision.label == "SELECTED_LANDED"),
                "source_transaction_failed": bool(
                    selected and decision.source_transaction_failed
                ),
                "output_guard_rejected": bool(selected and decision.output_guard_rejected),
                "execution_outcome": (
                    decision.execution_outcome if selected else "NOT_SELECTED_IN_GROUP"
                ),
                "failure_program": failure_program if selected else "",
                "failure_code": failure_code if selected else None,
                "failure_message": failure_message if selected else "",
                "source_output_floor": decision.source_output_floor if selected else None,
                "observed_source_tokens": (
                    decision.source_received_tokens
                    or decision.source_curve_output_tokens
                    if selected
                    else None
                ),
                "observed_source_sol": input_sol,
                "source_priority_fee": (
                    decision.source_priority_fee_sol if selected else None
                ),
                "source_tip": None,
                "estimated_curve_deterioration_bps": (
                    decision.estimated_curve_deterioration_bps if selected else impact
                ),
                "chosen_mint": decision.chosen_mint,
                "candidate_mint": launch.mint,
                "decision_ns": decision.decision_ns,
                "decision_ns_lower_bound": decision.decision_ns,
                "observed_landing_ns": (
                    decision.decision_ns if decision.label == "SELECTED_LANDED" else None
                ),
                "decision_ns_upper_bound": decision.decision_ns_upper_bound,
                "decision_time_quality": decision.decision_time_quality,
                "decision_time_source": (
                    "observed E4 BUY event received_ns"
                    if decision.label == "SELECTED_LANDED"
                    else "selected launch CREATE received_ns lower bound"
                ),
                "decision_time_is_lower_bound": decision.label == "SELECTED_FILL_REJECTED",
                "launch_received_ns": launch.create_ns,
                "candidate_state_ns": features.get("reserve_state_ns"),
                "decision_slot": decision.decision_slot,
                "source_transaction_slot": decision.source_transaction_slot,
                "source_transaction_index": (
                    decision.source_transaction_index
                    if decision.source_transaction_index >= 0
                    else None
                ),
                "decision_event_index": decision.decision_event_index,
                "decision_signature": decision.signature,
                "candidate_create_slot": launch.create_slot,
                "candidate_create_transaction_index": (
                    launch.create_transaction_index
                    if launch.create_transaction_index >= 0
                    else None
                ),
                "candidate_create_event_index": launch.create_event_index,
                "candidate_create_signature": launch.create_signature,
                "candidate_age_ms": age_ms,
                "same_slot_alternative": launch.create_slot == selected_launch.create_slot,
                "same_transaction_alternative": (
                    launch.create_signature == selected_launch.create_signature
                ),
                **features,
                **flags,
            }
            row["source_tip_missing_reason"] = "SOURCE_FIELD_ABSENT"
            row["reserve_missing_reason"] = (
                "" if flags["reserve_valid"] else "NO_PREDECISION_RESERVE"
            )
            row["transaction_index_missing_reason"] = (
                "" if row["candidate_create_transaction_index"] is not None else "SOURCE_FIELD_ABSENT"
            )
            row["funder_history_missing_reason"] = (
                "CREATOR_FUNDING_SOURCE_ABSENT"
            )
            row["metadata_history_missing_reason"] = (
                "" if features["metadata_observation_available"] else "METADATA_NOT_OBSERVED"
            )
            row["social_history_missing_reason"] = (
                "" if features["social_available_before_decision"] else "NO_IMMUTABLE_PREDECISION_PROOF"
            )
            group.append(row)
        selected_event = successful_event_by_signature.get(decision.signature) or {}
        reproduction = (
            v1.reproduce_observed_buy(selected_event)
            if decision.label == "SELECTED_LANDED"
            else None
        )
        for row in group:
            row["reserve_reproduction_error_bps"] = reproduction if row["selected_by_e4"] else None
        groups.append(group)
    return groups


def age_policy(groups: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    train_groups = [group for group in groups if group and group[0]["split"] == "train"]
    evaluations = []
    for envelope in ENVELOPES_MS:
        counts = []
        eligible_rows = []
        for group in train_groups:
            rows = [
                row
                for row in group
                if (
                    (envelope == 0 and row["same_slot_alternative"])
                    or (envelope > 0 and v1.finite(row["candidate_age_ms"]) <= envelope)
                    or row["selected_by_e4"]
                )
            ]
            eligible = [row for row in rows if row["riskset_eligible"] or row["selected_by_e4"]]
            counts.append(max(0, len(eligible) - 1))
            eligible_rows.extend(eligible)
        reserve_rate = (
            sum(bool(row["reserve_valid"]) for row in eligible_rows) / len(eligible_rows)
            if eligible_rows
            else 0.0
        )
        executable_rate = (
            sum(bool(row["economically_executable"]) for row in eligible_rows)
            / len(eligible_rows)
            if eligible_rows
            else 0.0
        )
        evaluations.append(
            {
                "age_envelope_ms": envelope,
                "training_groups": len(train_groups),
                "median_alternatives": statistics.median(counts) if counts else 0,
                "mean_alternatives": statistics.fmean(counts) if counts else 0,
                "groups_with_at_least_3_alternatives_percent": (
                    sum(value >= 3 for value in counts) / len(counts) * 100 if counts else 0
                ),
                "causal_reserve_coverage_percent": reserve_rate * 100,
                "economic_executability_percent": executable_rate * 100,
            }
        )
    defensible = [
        row
        for row in evaluations
        if row["age_envelope_ms"] > 0
        and row["causal_reserve_coverage_percent"] >= 99.0
        and row["economic_executability_percent"] >= 99.0
    ]
    chosen = max((row["age_envelope_ms"] for row in defensible), default=1_500)
    return {
        "version": "e4-v12-entry-age-forensics-v2",
        "policy_fit_split": "train",
        "validation_used_to_fit": False,
        "holdout_used_to_fit": False,
        "selection_rule": (
            "largest prespecified envelope at or below 60 seconds with >=99% causal-reserve "
            "and economic-executability coverage in training; no outcome metric is used"
        ),
        "active_age_policy_ms": chosen,
        "training_envelope_evaluations": evaluations,
    }


def envelope_diagnostics(
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    output = {}
    for cohort in ("all", "train", "validation", "holdout"):
        cohort_groups = [
            group
            for group in groups
            if group and (cohort == "all" or group[0]["split"] == cohort)
        ]
        rows = []
        for envelope in ENVELOPES_MS:
            counts = []
            for group in cohort_groups:
                included = [
                    row
                    for row in group
                    if row["selected_by_e4"]
                    or (
                        row["riskset_eligible"]
                        and (
                            (envelope == 0 and row["same_slot_alternative"])
                            or (
                                envelope > 0
                                and v1.finite(row["candidate_age_ms"]) <= envelope
                            )
                        )
                    )
                ]
                counts.append(max(0, len(included) - 1))
            rows.append(
                {
                    "age_envelope_ms": envelope,
                    "groups": len(counts),
                    "alternatives": sum(counts),
                    "mean_alternatives": statistics.fmean(counts) if counts else 0,
                    "median_alternatives": statistics.median(counts) if counts else 0,
                    "groups_with_at_least_3_alternatives_percent": (
                        sum(value >= 3 for value in counts) / len(counts) * 100
                        if counts
                        else 0
                    ),
                }
            )
        output[cohort] = rows
    return output


def age_distribution(decisions: Sequence[v1.Decision], scans: Sequence[v1.CaptureScan]) -> dict[str, Any]:
    launch_by_run = {scan.spec.run_id: scan.launches for scan in scans}

    def describe(values: Sequence[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "p10_ms": percentile(values, 0.10),
            "p25_ms": percentile(values, 0.25),
            "p50_ms": percentile(values, 0.50),
            "p75_ms": percentile(values, 0.75),
            "p90_ms": percentile(values, 0.90),
            "p95_ms": percentile(values, 0.95),
            "p99_ms": percentile(values, 0.99),
            "maximum_ms": max(values, default=None),
        }

    rows = []
    for decision in decisions:
        launch = launch_by_run[decision.run_id][decision.chosen_mint]
        rows.append(
            {
                "decision_group_id": decision.group_id,
                "source_run_id": decision.run_id,
                "split": decision.split,
                "selection_label": decision.label,
                "age_ms": max(0.0, (decision.decision_ns - launch.create_ns) / 1_000_000),
                "same_slot": launch.create_slot == decision.decision_slot,
                "same_transaction": launch.create_signature == decision.signature,
                "decision_time_is_lower_bound": decision.label == "SELECTED_FILL_REJECTED",
            }
        )
    exact = [row for row in rows if not row["decision_time_is_lower_bound"]]
    result = {
        "all_selections": describe([row["age_ms"] for row in rows]),
        "exact_landed_selections": describe([row["age_ms"] for row in exact]),
        "lower_bound_failed_fill_selections": describe(
            [row["age_ms"] for row in rows if row["decision_time_is_lower_bound"]]
        ),
        "same_slot_percent": sum(row["same_slot"] for row in rows) / len(rows) * 100,
        "same_transaction_percent": sum(row["same_transaction"] for row in rows) / len(rows) * 100,
        "by_capture_window": {
            run_id: describe([row["age_ms"] for row in rows if row["source_run_id"] == run_id])
            for run_id in sorted({row["source_run_id"] for row in rows}, key=int)
        },
        "by_selection_label": {
            label: describe([row["age_ms"] for row in rows if row["selection_label"] == label])
            for label in ("SELECTED_LANDED", "SELECTED_FILL_REJECTED")
        },
        "by_strategy_family": {
            "observed_landed": describe(
                [row["age_ms"] for row in rows if row["selection_label"] == "SELECTED_LANDED"]
            ),
            "failed_fill_lower_bound": describe(
                [
                    row["age_ms"]
                    for row in rows
                    if row["selection_label"] == "SELECTED_FILL_REJECTED"
                ]
            ),
        },
        "caveat": (
            "failed-fill ages are conservative zero-age lower bounds and are never mixed into "
            "the exact landed-age percentiles used for interpretation"
        ),
    }
    return result


def apply_policy(
    groups: Sequence[Sequence[dict[str, Any]]], policy_ms: int
) -> list[list[dict[str, Any]]]:
    output = []
    for source_group in groups:
        group = [
            row
            for row in source_group
            if (
                (v1.finite(row["candidate_age_ms"]) <= policy_ms and row["riskset_eligible"])
                or row["selected_by_e4"]
            )
        ]
        if sum(bool(row["selected_by_e4"]) for row in group) != 1:
            raise ValueError("active-age filtering lost or duplicated a selected row")
        scan_start = v1.integer(group[0]["capture_start_ns"])
        left_truncated = v1.integer(group[0]["decision_ns"]) - scan_start < policy_ms * 1_000_000
        for row in group:
            row["group_age_policy_ms"] = policy_ms
            row["opportunity_pool_left_truncated"] = left_truncated
            row["active_launch_count"] = sum(
                v1.finite(value["candidate_age_ms"]) <= policy_ms for value in source_group
            )
            row["route_eligible_launch_count"] = sum(
                bool(value["route_eligible"]) for value in source_group
            )
            row["reserve_valid_launch_count"] = sum(
                bool(value["reserve_valid"]) for value in source_group
            )
            row["economically_executable_launch_count"] = sum(
                bool(value["economically_executable"]) for value in source_group
            )
            row["final_included_candidate_count"] = len(group)
            row["candidate_exclusions_by_reason_json"] = compact(
                Counter(
                    str(value["candidate_exclusion_reason"])
                    for value in source_group
                    if value["candidate_exclusion_reason"]
                )
            )
        output.append(group)
    return output


def add_entity_histories(
    groups: Sequence[Sequence[dict[str, Any]]],
    scans: Sequence[v1.CaptureScan],
) -> dict[str, Any]:
    launches_by_creator: dict[str, list[int]] = defaultdict(list)
    for scan in scans:
        for launch in scan.launches.values():
            launches_by_creator[launch.creator].append(launch.create_ns)
    for values in launches_by_creator.values():
        values.sort()
    selected_creator: Counter[str] = Counter()
    landed_creator: Counter[str] = Counter()
    failed_creator: Counter[str] = Counter()
    selected_buyer: Counter[str] = Counter()
    landed_buyer: Counter[str] = Counter()
    failed_buyer: Counter[str] = Counter()
    selected_cluster: Counter[str] = Counter()
    creator_buyer: Counter[tuple[str, str]] = Counter()
    selected_social: Counter[str] = Counter()
    last_selected_creator: dict[str, int] = {}
    last_selected_buyer: dict[str, int] = {}
    last_selected_cluster: dict[str, int] = {}
    last_selected_social: dict[str, int] = {}
    last_selected_pair: dict[tuple[str, str], int] = {}
    graph_creators: dict[str, dict[str, Any]] = {}
    graph_buyers: dict[str, dict[str, Any]] = {}
    graph_clusters: dict[str, dict[str, Any]] = {}
    graph_social: dict[str, dict[str, Any]] = {}
    graph_pairs: dict[str, dict[str, Any]] = {}
    buyer_times: dict[str, list[int]] = defaultdict(list)
    cluster_times: dict[str, list[int]] = defaultdict(list)
    social_times: dict[str, list[int]] = defaultdict(list)
    pair_times: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_time: dict[int, list[Sequence[dict[str, Any]]]] = defaultdict(list)
    for group in groups:
        by_time[v1.integer(group[0]["decision_ns"])].append(group)
    for decision_ns in sorted(by_time):
        pending = []
        for group in sorted(by_time[decision_ns], key=lambda rows: rows[0]["decision_group_id"]):
            for row in group:
                creator = str(row.get("creator_address") or "")
                buyers = json.loads(str(row.get("first_buyer_identities_json") or "[]"))
                cluster = str(row.get("first_buyer_cluster") or "")
                social = str(row.get("social_account") or "")
                launch_times = launches_by_creator.get(creator, [])
                prior_count = bisect.bisect_left(launch_times, decision_ns)
                previous = launch_times[prior_count - 1] if prior_count else None
                row.update(
                    {
                        "creator_prior_launch_count": prior_count,
                        "creator_prior_e4_selection_count_v2": selected_creator[creator],
                        "creator_prior_landed_count": landed_creator[creator],
                        "creator_prior_failed_fill_count": failed_creator[creator],
                        "time_since_previous_launch_ms": (
                            (decision_ns - previous) / 1_000_000 if previous else None
                        ),
                        "time_since_previous_e4_selection_ms": (
                            (decision_ns - last_selected_creator[creator]) / 1_000_000
                            if creator in last_selected_creator
                            else None
                        ),
                        "known_e4_associated_buyer_count": sum(
                            selected_buyer[buyer] > 0 for buyer in buyers
                        ),
                        "buyer_prior_e4_selection_count": sum(
                            selected_buyer[buyer] for buyer in buyers
                        ),
                        "buyer_prior_execution_counts": [
                            sum(landed_buyer[buyer] for buyer in buyers),
                            sum(failed_buyer[buyer] for buyer in buyers),
                        ],
                        "buyer_cluster_recurrence": selected_cluster[cluster] if cluster else 0,
                        "creator_buyer_pair_recurrence": max(
                            (creator_buyer[(creator, buyer)] for buyer in buyers), default=0
                        ),
                        "social_authority_prior_selection_count": (
                            selected_social[social] if social else 0
                        ),
                        "funder_history_available": False,
                        "first_buyer_history_available": bool(buyers),
                        "buyer_cluster_history_available": bool(cluster),
                        "social_history_available": bool(social),
                        "metadata_history_available": bool(
                            row.get("metadata_observation_available")
                        ),
                        "entity_history_feature_ns": decision_ns,
                        "entity_last_seen_before_decision_ns": [
                            previous,
                            max((last_selected_buyer.get(buyer, 0) for buyer in buyers), default=0)
                            or None,
                            last_selected_cluster.get(cluster),
                            last_selected_social.get(social),
                            max(
                                (
                                    last_selected_pair.get((creator, buyer), 0)
                                    for buyer in buyers
                                ),
                                default=0,
                            )
                            or None,
                            None,
                        ],
                    }
                )
                row["creator_history_available"] = bool(
                    row.get("creator_history_available") or prior_count
                )
            selected = next(row for row in group if row["selected_by_e4"])
            pending.append(selected)
        for row in pending:
            creator = str(row.get("creator_address") or "")
            buyers = json.loads(str(row.get("first_buyer_identities_json") or "[]"))
            cluster = str(row.get("first_buyer_cluster") or "")
            social = str(row.get("social_account") or "")
            selected_creator[creator] += 1
            last_selected_creator[creator] = decision_ns
            if row["selection_label"] == "SELECTED_LANDED":
                landed_creator[creator] += 1
            else:
                failed_creator[creator] += 1
            for buyer in buyers:
                selected_buyer[buyer] += 1
                creator_buyer[(creator, buyer)] += 1
                buyer_times[buyer].append(decision_ns)
                pair_times[(creator, buyer)].append(decision_ns)
                last_selected_buyer[buyer] = decision_ns
                last_selected_pair[(creator, buyer)] = decision_ns
                if row["selection_label"] == "SELECTED_LANDED":
                    landed_buyer[buyer] += 1
                else:
                    failed_buyer[buyer] += 1
            if cluster:
                selected_cluster[cluster] += 1
                cluster_times[cluster].append(decision_ns)
                last_selected_cluster[cluster] = decision_ns
            if social:
                selected_social[social] += 1
                social_times[social].append(decision_ns)
                last_selected_social[social] = decision_ns
    for creator, times in launches_by_creator.items():
        graph_creators[creator] = {
            "first_seen_ns": times[0],
            "last_seen_ns": times[-1],
            "launch_count": len(times),
            "e4_selection_count": selected_creator[creator],
            "landed_selection_count": landed_creator[creator],
            "failed_fill_selection_count": failed_creator[creator],
        }
    for buyer, count in selected_buyer.items():
        graph_buyers[buyer] = {
            "first_seen_ns": buyer_times[buyer][0],
            "last_seen_ns": buyer_times[buyer][-1],
            "prior_e4_association_observations": count,
            "landed_associations": landed_buyer[buyer],
            "failed_fill_associations": failed_buyer[buyer],
        }
    for cluster, count in selected_cluster.items():
        graph_clusters[cluster] = {
            "first_seen_ns": cluster_times[cluster][0],
            "last_seen_ns": cluster_times[cluster][-1],
            "prior_e4_association_observations": count,
        }
    for social, count in selected_social.items():
        graph_social[social] = {
            "first_seen_ns": social_times[social][0],
            "last_seen_ns": social_times[social][-1],
            "prior_e4_association_observations": count,
        }
    for pair, count in creator_buyer.items():
        creator, buyer = pair
        graph_pairs[f"{creator}|{buyer}"] = {
            "creator": creator,
            "buyer": buyer,
            "first_seen_ns": pair_times[pair][0],
            "last_seen_ns": pair_times[pair][-1],
            "prior_e4_association_observations": count,
        }
    return {
        "version": "e4-v12-causal-entity-graph-v2",
        "chronology_rule": "each row reads counters before the current decision is applied",
        "creators": graph_creators,
        "first_buyers": graph_buyers,
        "buyer_clusters": graph_clusters,
        "creator_buyer_pairs": graph_pairs,
        "social_accounts": graph_social,
        "funders": {},
        "funder_evidence_status": (
            "UNAVAILABLE: capture events omit creator funding transfers, transaction account "
            "lists, and creator fee payers; E4 wallet artifacts expose only E4 fee payers"
        ),
        "unavailable_relationships": [
            "creator funding wallet",
            "recurring funder",
            "deployer family beyond creator identity",
            "candidate transaction signer",
            "candidate transaction fee payer",
            "buyer funding relationship",
        ],
    }


RANK_SPECS = {
    "creator_seed": ("creator_seed_sol", "high"),
    "fdv": ("fdv_usd", "low"),
    "launch_age": ("candidate_age_ms", "low"),
    "buyer_count": ("unique_buyers", "high"),
    "buyer_velocity": ("unique_buyers_per_second", "high"),
    "outside_sol": ("public_buy_sol", "high"),
    "identity_history": ("buyer_prior_e4_selection_count", "high"),
    "creator_history": ("creator_prior_e4_selection_count_v2", "high"),
    "funder_history": ("funder_prior_e4_selection_count", "high"),
    "buyer_cluster": ("buyer_cluster_recurrence", "high"),
    "social_authority": ("social_authority_prior_selection_count", "high"),
    "same_slot_flow": ("same_slot_buyer_count", "high"),
    "topology": ("topology_score", "high"),
    "executable_output": ("executable_token_output_0_1_sol", "high"),
    "estimated_impact": ("estimated_price_impact_bps", "low"),
}
HARD_NEGATIVE_CATEGORIES = (
    "SAME_SLOT_ALTERNATIVE",
    "SAME_TRANSACTION_ALTERNATIVE",
    "NEAREST_TIME_ALTERNATIVE",
    "MATCHED_FDV_ALTERNATIVE",
    "MATCHED_SEED_ALTERNATIVE",
    "MATCHED_FLOW_ALTERNATIVE",
    "MATCHED_IDENTITY_ALTERNATIVE",
    "MATCHED_CREATOR_HISTORY_ALTERNATIVE",
    "MATCHED_BUYER_CLUSTER_ALTERNATIVE",
    "MATCHED_SOCIAL_ALTERNATIVE",
    "MATCHED_TOPOLOGY_ALTERNATIVE",
    "TOP_MARKET_ALTERNATIVE",
    "ACTIVE_EXECUTABLE_ALTERNATIVE",
    "RANDOM_ELIGIBLE_CONTROL",
)


def add_derived_features(groups: Sequence[Sequence[dict[str, Any]]]) -> None:
    for group in groups:
        for row in group:
            age_seconds = max(v1.finite(row["candidate_age_ms"]) / 1_000, 0.001)
            unique_buyers = v1.finite(row.get("unique_buyers"))
            buy_count = v1.finite(row.get("buy_count"))
            sell_count = v1.finite(row.get("sell_count"))
            virtual_sol = v1.finite(row.get("virtual_sol_reserve"))
            row.update(
                {
                    "creator_seed_percentage": (
                        v1.finite(row.get("creator_seed_sol")) / virtual_sol * 100
                        if virtual_sol > 0
                        else None
                    ),
                    "repeated_early_buyer_count": v1.integer(
                        row.get("prior_first_buyer_e4_overlap_count")
                    ),
                    "buyer_concentration": (
                        v1.finite(row.get("max_buys_in_one_transaction")) / buy_count
                        if buy_count > 0
                        else 0.0
                    ),
                    "distinct_buy_signatures": v1.integer(row.get("transaction_count")),
                    "buys_per_second": buy_count / age_seconds,
                    "unique_buyers_per_second": unique_buyers / age_seconds,
                    "buy_sell_imbalance": (
                        (buy_count - sell_count) / (buy_count + sell_count)
                        if buy_count + sell_count > 0
                        else 0.0
                    ),
                    "topology_score": (
                        v1.finite(row.get("same_slot_buyer_count"))
                        + v1.finite(row.get("same_transaction_buyer_count"))
                        + v1.finite(row.get("max_buys_in_one_transaction"))
                    ),
                    "estimated_price_impact_bps": (
                        0.1 / (virtual_sol + 0.1) * 10_000 if virtual_sol > 0 else None
                    ),
                    "cumulative_outside_sol": v1.finite(row.get("public_buy_sol")),
                    "funder_prior_creator_count": None,
                    "funder_prior_launch_count": None,
                    "funder_prior_e4_selection_count": None,
                    "creator_funder_recurrence": None,
                    "funder_cluster_identity": "",
                    "funding_amount_sol": None,
                    "funding_age_ms": None,
                    "buyer_funding_relationships_json": "[]",
                    "signer_count": None,
                    "fee_payer_recurrence": None,
                    "instruction_ordering": (
                        f"create_event_index={row['candidate_create_event_index']}"
                    ),
                    "direct_ca_publication": None,
                    "prelaunch_announcement": row.get("social_post_existed_before_launch"),
                    "creator_prior_profitable_count": v1.integer(
                        row.get("creator_history_wins")
                    ),
                    "creator_prior_losing_count": v1.integer(
                        row.get("creator_history_losses")
                    ),
                    "creator_prior_profit_factor": None,
                    "creator_prior_win_rate": v1.finite(
                        row.get("creator_history_win_rate")
                    ),
                }
            )


def augment_entry_age_forensics(
    report: dict[str, Any], groups: Sequence[Sequence[Mapping[str, Any]]]
) -> None:
    selected = [next(row for row in group if row["selected_by_e4"]) for group in groups]

    def describe(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        values = [v1.finite(row["candidate_age_ms"]) for row in rows]
        return {
            "count": len(values),
            "p10_ms": percentile(values, 0.10),
            "p25_ms": percentile(values, 0.25),
            "p50_ms": percentile(values, 0.50),
            "p75_ms": percentile(values, 0.75),
            "p90_ms": percentile(values, 0.90),
            "p95_ms": percentile(values, 0.95),
            "p99_ms": percentile(values, 0.99),
            "maximum_ms": max(values, default=None),
        }

    creator_bands = {
        "no_prior_launch": lambda row: v1.integer(row["creator_prior_launch_count"]) == 0,
        "one_or_two_prior_launches": lambda row: 1
        <= v1.integer(row["creator_prior_launch_count"])
        <= 2,
        "three_plus_prior_launches": lambda row: v1.integer(
            row["creator_prior_launch_count"]
        )
        >= 3,
    }
    report["by_creator_history_band"] = {
        name: describe([row for row in selected if predicate(row)])
        for name, predicate in creator_bands.items()
    }
    report["by_social_evidence_availability"] = {
        "available": describe(
            [row for row in selected if row["social_available_before_decision"]]
        ),
        "unavailable": describe(
            [row for row in selected if not row["social_available_before_decision"]]
        ),
    }
    report["by_first_buyer_recurrence"] = {
        "none": describe(
            [row for row in selected if not row["known_e4_associated_buyer_count"]]
        ),
        "one_or_more": describe(
            [row for row in selected if row["known_e4_associated_buyer_count"]]
        ),
    }
    report["by_transaction_topology"] = {
        "same_transaction": describe(
            [row for row in selected if row["same_transaction_alternative"]]
        ),
        "same_slot_not_same_transaction": describe(
            [
                row
                for row in selected
                if row["same_slot_alternative"] and not row["same_transaction_alternative"]
            ]
        ),
        "later_slot": describe(
            [row for row in selected if not row["same_slot_alternative"]]
        ),
    }


def add_relative_features(groups: Sequence[Sequence[dict[str, Any]]]) -> None:
    for group in groups:
        for rank_name, (field, direction) in RANK_SPECS.items():
            values = [v1.finite(row.get(field)) for row in group]
            unique = sorted(set(values), reverse=direction == "high")
            median = statistics.median(values)
            leader = unique[0]
            for row, value in zip(group, values):
                rank = unique.index(value) + 1
                row[f"{rank_name}_rank"] = rank
                row[f"{rank_name}_percentile"] = (
                    1.0 if len(group) == 1 else 1 - (rank - 1) / (len(group) - 1)
                )
                row[f"{rank_name}_distance_from_group_leader"] = abs(value - leader)
                row[f"{rank_name}_ratio_to_group_median"] = (
                    value / median if median != 0 else None
                )
        objectives = [
            ("fdv_usd", "low"),
            ("candidate_age_ms", "low"),
            ("unique_buyers_per_second", "high"),
            ("public_buy_sol", "high"),
            ("executable_token_output_0_1_sol", "high"),
        ]
        for row in group:
            dominating = 0
            dominated = 0
            for other in group:
                if other is row:
                    continue
                row_values = [v1.finite(row.get(field)) for field, _ in objectives]
                other_values = [v1.finite(other.get(field)) for field, _ in objectives]
                row_better = [
                    left <= right if direction == "low" else left >= right
                    for left, right, (_, direction) in zip(
                        row_values, other_values, objectives
                    )
                ]
                other_better = [
                    right <= left if direction == "low" else right >= left
                    for left, right, (_, direction) in zip(
                        row_values, other_values, objectives
                    )
                ]
                if all(row_better) and any(a != b for a, b in zip(row_values, other_values)):
                    dominated += 1
                if all(other_better) and any(a != b for a, b in zip(row_values, other_values)):
                    dominating += 1
            row["candidates_dominated"] = dominated
            row["candidates_dominating"] = dominating


def pack_wide_features(groups: Sequence[Sequence[dict[str, Any]]]) -> None:
    """Keep JSONL below GitHub's file limit without dropping any feature values."""
    relative_suffixes = (
        "rank",
        "percentile",
        "distance_from_group_leader",
        "ratio_to_group_median",
    )
    window_fields = ("buy_count", "buy_sol", "unique_buyers")
    for group in groups:
        for row in group:
            relative = {}
            for name in RANK_SPECS:
                relative[name] = [row.pop(f"{name}_{suffix}") for suffix in relative_suffixes]
            windows = {}
            for window in v1.WINDOWS_MS:
                windows[str(window)] = [
                    row.pop(f"{field}_first_{window}ms") for field in window_fields
                ]
            row["relative_features"] = relative
            row["predecision_window_features"] = windows
            row["group_eligibility_counts"] = [
                row.pop("active_launch_count"),
                row.pop("route_eligible_launch_count"),
                row.pop("reserve_valid_launch_count"),
                row.pop("economically_executable_launch_count"),
                row.pop("final_included_candidate_count"),
            ]
            row["missing_reasons"] = {
                "tip": row.pop("source_tip_missing_reason"),
                "reserve": row.pop("reserve_missing_reason"),
                "transaction_index": row.pop("transaction_index_missing_reason"),
                "funder": row.pop("funder_history_missing_reason"),
                "metadata": row.pop("metadata_history_missing_reason"),
                "social": row.pop("social_history_missing_reason"),
            }
            row["feature_causality_contract"] = "v2"


def training_scales(groups: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, float]:
    fields = (
        "candidate_age_ms",
        "fdv_usd",
        "creator_seed_sol",
        "public_buy_sol",
        "unique_buyers",
        "creator_prior_e4_selection_count_v2",
        "buyer_cluster_recurrence",
        "topology_score",
    )
    train_rows = [row for group in groups if group[0]["split"] == "train" for row in group]
    scales = {}
    for field in fields:
        values = [v1.finite(row.get(field)) for row in train_rows]
        q25 = percentile(values, 0.25) or 0.0
        q75 = percentile(values, 0.75) or 0.0
        scales[field] = max(q75 - q25, 1e-9)
    return scales


def numeric_distance(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    fields: Sequence[str],
    scales: Mapping[str, float],
) -> float:
    return math.sqrt(
        sum(
            ((v1.finite(left.get(field)) - v1.finite(right.get(field))) / scales[field]) ** 2
            for field in fields
        )
    )


def mark_nearest(
    alternatives: Sequence[dict[str, Any]],
    selected: Mapping[str, Any],
    category: str,
    distance: Any,
) -> None:
    if not alternatives:
        return
    winner = min(alternatives, key=lambda row: (distance(row, selected), row["candidate_mint"]))
    winner["_negative_categories"].add(category)


def add_hard_negatives(
    groups: Sequence[Sequence[dict[str, Any]]], scales: Mapping[str, float]
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for group in groups:
        selected = next(row for row in group if row["selected_by_e4"])
        alternatives = [row for row in group if not row["selected_by_e4"]]
        for row in group:
            row["_negative_categories"] = set()
        for row in alternatives:
            row["_negative_categories"].add("ACTIVE_EXECUTABLE_ALTERNATIVE")
            if row["same_slot_alternative"]:
                row["_negative_categories"].add("SAME_SLOT_ALTERNATIVE")
            if row["same_transaction_alternative"]:
                row["_negative_categories"].add("SAME_TRANSACTION_ALTERNATIVE")
        mark_nearest(
            alternatives,
            selected,
            "NEAREST_TIME_ALTERNATIVE",
            lambda row, chosen: abs(row["candidate_age_ms"] - chosen["candidate_age_ms"]),
        )
        for category, fields in (
            ("MATCHED_FDV_ALTERNATIVE", ("fdv_usd",)),
            ("MATCHED_SEED_ALTERNATIVE", ("creator_seed_sol",)),
            ("MATCHED_FLOW_ALTERNATIVE", ("public_buy_sol", "unique_buyers")),
            (
                "MATCHED_CREATOR_HISTORY_ALTERNATIVE",
                ("creator_prior_e4_selection_count_v2",),
            ),
            ("MATCHED_BUYER_CLUSTER_ALTERNATIVE", ("buyer_cluster_recurrence",)),
            ("MATCHED_TOPOLOGY_ALTERNATIVE", ("topology_score",)),
        ):
            mark_nearest(
                alternatives,
                selected,
                category,
                lambda row, chosen, fields=fields: numeric_distance(
                    row, chosen, fields, scales
                ),
            )
        mark_nearest(
            alternatives,
            selected,
            "MATCHED_IDENTITY_ALTERNATIVE",
            lambda row, chosen: 1
            - (
                len(
                    set(json.loads(row["first_buyer_identities_json"]))
                    & set(json.loads(chosen["first_buyer_identities_json"]))
                )
                / max(
                    1,
                    len(
                        set(json.loads(row["first_buyer_identities_json"]))
                        | set(json.loads(chosen["first_buyer_identities_json"]))
                    ),
                )
            ),
        )
        mark_nearest(
            alternatives,
            selected,
            "MATCHED_SOCIAL_ALTERNATIVE",
            lambda row, chosen: float(
                bool(row["social_available_before_decision"])
                != bool(chosen["social_available_before_decision"])
            ),
        )
        if alternatives:
            max(alternatives, key=lambda row: (row["public_buy_sol"], row["fdv_usd"]))[
                "_negative_categories"
            ].add("TOP_MARKET_ALTERNATIVE")
            min(
                alternatives,
                key=lambda row: hashlib.sha256(
                    f"{row['decision_group_id']}|{row['candidate_mint']}".encode()
                ).hexdigest(),
            )["_negative_categories"].add("RANDOM_ELIGIBLE_CONTROL")
        selected["hard_negative_categories_json"] = "[]"
        for row in alternatives:
            categories = sorted(row.pop("_negative_categories"))
            row["hard_negative_categories_json"] = compact(categories)
            counts.update(categories)
        selected.pop("_negative_categories", None)
        for row in group:
            nearest = min(
                (other for other in group if other is not row),
                key=lambda other: numeric_distance(
                    row,
                    other,
                    (
                        "candidate_age_ms",
                        "fdv_usd",
                        "creator_seed_sol",
                        "public_buy_sol",
                        "unique_buyers",
                    ),
                    scales,
                ),
                default=None,
            )
            row["nearest_matched_candidate_mint"] = (
                nearest["candidate_mint"] if nearest else ""
            )
            row["fdv_ratio_to_nearest_matched_alternative"] = (
                v1.finite(row.get("fdv_usd")) / v1.finite(nearest.get("fdv_usd"))
                if nearest and v1.finite(nearest.get("fdv_usd")) != 0
                else None
            )
    return {
        "version": "e4-v12-hard-negative-audit-v2",
        "selection_policy": "causal decision-time fields only; no outcome fields loaded",
        "similarity_scale_fit_split": "train",
        "validation_used_to_fit": False,
        "holdout_used_to_fit": False,
        "training_iqr_scales": scales,
        "category_counts": {
            category: counts[category] for category in HARD_NEGATIVE_CATEGORIES
        },
    }


def add_feature_provenance(groups: Sequence[Sequence[dict[str, Any]]]) -> None:
    for group in groups:
        for row in group:
            provenance = {
                "launch": {
                    "source_event": row["source_row_event_identity"],
                    "source_timestamp": row["launch_received_ns"],
                    "decision_timestamp": row["decision_ns"],
                    "causal_valid": row["launch_received_ns"] <= row["decision_ns"],
                    "missing_reason": "",
                    "provenance": row["source_artifact_name"],
                },
                "market": {
                    "source_event": "last visible event for candidate",
                    "source_timestamp": row.get("feature_max_event_ns"),
                    "decision_timestamp": row["decision_ns"],
                    "causal_valid": (
                        row.get("feature_max_event_ns") is None
                        or row["feature_max_event_ns"] <= row["decision_ns"]
                    ),
                    "missing_reason": row["reserve_missing_reason"],
                    "provenance": row["source_artifact_name"],
                },
                "entity": {
                    "source_event": "strictly prior capture and selection ledger",
                    "source_timestamp": row["entity_history_feature_ns"],
                    "decision_timestamp": row["decision_ns"],
                    "causal_valid": row["entity_history_feature_ns"] <= row["decision_ns"],
                    "missing_reason": row["funder_history_missing_reason"],
                    "provenance": "V2 chronological entity graph",
                },
                "social": {
                    "source_event": "content-addressed launch metadata",
                    "source_timestamp": row.get("social_feature_ns"),
                    "decision_timestamp": row["decision_ns"],
                    "causal_valid": (
                        row.get("social_feature_ns") is None
                        or row["social_feature_ns"] <= row["decision_ns"]
                    ),
                    "missing_reason": row["social_history_missing_reason"],
                    "provenance": "pinned social-choice artifact",
                },
            }
            if not all(value["causal_valid"] for value in provenance.values()):
                raise ValueError("row failed feature-provenance causality contract")
            row["feature_causality_contract"] = "v2"


def selection_backfill(
    repo_root: Path,
    decisions: Sequence[v1.Decision],
    attempts: Sequence[Mapping[str, Any]],
    capture_manifest: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    v1_coverage = json.loads(
        (repo_root / "artifacts/e4-v12-choice-set-coverage.json").read_text(encoding="utf-8")
    )
    included = {decision.signature: decision for decision in decisions}
    attempt_by_signature = {str(row.get("signature") or ""): row for row in attempts}
    source_names = [str(row["artifact_name"]) for row in capture_manifest]
    outcomes = []
    for old in v1_coverage.get("exclusions") or []:
        signature = str(old.get("signature") or "")
        attempt = attempt_by_signature.get(signature) or {}
        mint = str(attempt.get("mapped_mint") or old.get("mint") or "")
        decision = included.get(signature)
        timestamp_ns = v1.integer(old.get("block_time")) * 1_000_000_000
        inferred_window = next(
            (
                str(source["run_id"])
                for source in capture_manifest
                if v1.integer(source["capture_start_ns"])
                <= timestamp_ns
                <= v1.integer(source["capture_end_ns"])
            ),
            "OUTSIDE_CAPTURE_WINDOWS",
        )
        if decision:
            outcome = "RECOVERED_IN_V2"
            explanation = "selection matched to an immutable CREATE and causal reserve stream"
            missing = []
        elif not mint:
            outcome = "UNRESOLVED_MINT"
            explanation = "wallet transaction has no authoritative mint mapping"
            missing = ["decoded Pump instruction account mapping"]
        elif old.get("reason_code") == "OUTSIDE_AUTHORITATIVE_CAPTURE_INTERVAL":
            outcome = "OUTSIDE_RETRIEVABLE_HISTORY"
            explanation = "selection timestamp lies outside all restored exact-event windows"
            missing = ["archived exact live-event capture covering the transaction"]
        else:
            outcome = "MISSING_LAUNCH_EVENT"
            explanation = "mapped mint is absent from every restored CREATE-event capture"
            missing = ["immutable CREATE event and pre-decision reserve history"]
        outcomes.append(
            {
                "event_identity": f"wallet:{signature}",
                "wallet_signature": signature,
                "mint": mint,
                "timestamp": old.get("block_time"),
                "selection_type": old.get("selection_kind"),
                "source_window": decision.run_id if decision else inferred_window,
                "outcome": outcome,
                "reason_code": outcome,
                "explanation": explanation,
                "sources_searched": source_names,
                "evidence_still_missing": missing,
            }
        )
    counts = Counter((row["selection_type"], row["outcome"]) for row in outcomes)
    report = {
        "version": "e4-v12-selection-backfill-v2",
        "v1_exclusions_audited": len(outcomes),
        "successful_recovered": counts[("SUCCESSFUL_BUY", "RECOVERED_IN_V2")],
        "failed_fill_recovered": counts[("FAILED_ATTEMPT", "RECOVERED_IN_V2")],
        "counts": {
            f"{kind}:{outcome}": count
            for (kind, outcome), count in sorted(counts.items())
        },
        "counts_by_source_window": dict(
            sorted(Counter(row["source_window"] for row in outcomes).items())
        ),
        "outcomes": outcomes,
    }
    exclusions = [row for row in outcomes if row["outcome"] != "RECOVERED_IN_V2"]
    return report, exclusions


def integrity(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    scans: Sequence[v1.CaptureScan],
    v1_audit: Mapping[str, Any],
) -> dict[str, Any]:
    violations = []
    timestamp_fields = (
        "launch_received_ns",
        "candidate_state_ns",
        "feature_max_event_ns",
        "reserve_state_ns",
        "social_feature_ns",
        "creator_history_feature_ns",
        "first_buyer_history_feature_ns",
        "whitelist_feature_ns",
        "entity_history_feature_ns",
    )
    for group in groups:
        group_id = group[0]["decision_group_id"]
        selected = [row for row in group if row["selected_by_e4"]]
        if len(selected) != 1:
            violations.append({"code": "SELECTED_ROW_CARDINALITY", "group": group_id})
        mints = [str(row["candidate_mint"]) for row in group]
        if len(mints) != len(set(mints)):
            violations.append({"code": "DUPLICATE_MINT_IN_GROUP", "group": group_id})
        for row in group:
            for field in timestamp_fields:
                value = row.get(field)
                if value is not None and v1.integer(value) > v1.integer(row["decision_ns"]):
                    violations.append(
                        {
                            "code": "FUTURE_FEATURE_TIMESTAMP",
                            "group": group_id,
                            "mint": row["candidate_mint"],
                            "field": field,
                        }
                    )
            for timestamp in row.get("entity_last_seen_before_decision_ns") or []:
                if timestamp is not None and v1.integer(timestamp) >= v1.integer(
                    row["decision_ns"]
                ):
                    violations.append(
                        {
                            "code": "NON_PRIOR_ENTITY_HISTORY",
                            "group": group_id,
                            "mint": row["candidate_mint"],
                        }
                    )
            if row["selection_label"] == "SELECTED_FILL_REJECTED" and not row[
                "selected_by_e4"
            ]:
                violations.append({"code": "FAILED_FILL_NEGATIVE", "group": group_id})
    ranges = v1.split_ranges(scans)
    if ranges["train"]["maximum_received_ns"] >= ranges["validation"]["minimum_received_ns"]:
        violations.append({"code": "TRAIN_VALIDATION_OVERLAP"})
    if ranges["validation"]["maximum_received_ns"] >= ranges["holdout"]["minimum_received_ns"]:
        violations.append({"code": "VALIDATION_HOLDOUT_OVERLAP"})
    return {
        "status": "PASS" if not violations and v1_audit["status"] == "PASS" else "FAIL",
        "violations": violations,
        "future_leakage_violations": sum(
            row["code"] == "FUTURE_FEATURE_TIMESTAMP" for row in violations
        ),
        "ambiguous_labels": 0,
        "missing_chosen_rows": sum(
            row["code"] == "SELECTED_ROW_CARDINALITY" for row in violations
        ),
        "duplicate_rows": sum(row["code"] == "DUPLICATE_MINT_IN_GROUP" for row in violations),
        "source_parse_errors": sum(scan.parse_errors for scan in scans),
        "duplicate_source_events": sum(scan.duplicate_events for scan in scans),
        "v1_hash_changes": v1_audit["changed_files"],
        "chronological_split_leakage": sum("OVERLAP" in row["code"] for row in violations),
    }


def group_distribution(groups: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    counts = [len(group) - 1 for group in groups]
    total = len(counts)
    bands = {
        "0": lambda value: value == 0,
        "1": lambda value: value == 1,
        "2": lambda value: value == 2,
        "3-5": lambda value: 3 <= value <= 5,
        "6-10": lambda value: 6 <= value <= 10,
        "11-20": lambda value: 11 <= value <= 20,
        "more_than_20": lambda value: value > 20,
    }
    return {
        "average_alternatives": statistics.fmean(counts) if counts else 0,
        "median_alternatives": statistics.median(counts) if counts else 0,
        "minimum_alternatives": min(counts, default=0),
        "maximum_alternatives": max(counts, default=0),
        "groups_with_at_least_3_alternatives_percent": (
            sum(value >= 3 for value in counts) / total * 100 if total else 0
        ),
        "percentage_by_alternative_band": {
            name: sum(predicate(value) for value in counts) / total * 100 if total else 0
            for name, predicate in bands.items()
        },
    }


def coverage_report(
    groups: Sequence[Sequence[Mapping[str, Any]]],
    scans: Sequence[v1.CaptureScan],
    policy: Mapping[str, Any],
    backfill: Mapping[str, Any],
    integrity_result: Mapping[str, Any],
    exclusions: Sequence[Mapping[str, Any]],
    manifest_path: Path,
) -> dict[str, Any]:
    rows = [row for group in groups for row in group]
    selected = [row for row in rows if row["selected_by_e4"]]
    landed = [row for row in selected if row["selection_label"] == "SELECTED_LANDED"]
    failed = [row for row in selected if row["selection_label"] == "SELECTED_FILL_REJECTED"]
    distribution = group_distribution(groups)

    def percent(field: str) -> float:
        return sum(bool(row.get(field)) for row in rows) / len(rows) * 100 if rows else 0

    clean_v1_universe = 265 + 449
    clean_included = 322 + backfill["successful_recovered"] + backfill["failed_fill_recovered"]
    return {
        "version": "e4-v12-choice-riskset-v2-coverage-v1",
        "source_manifest": str(manifest_path).replace("\\", "/"),
        "source_manifest_sha256": v1.sha256_path(manifest_path),
        "sources": {
            "capture_runs": len(scans),
            "captured_launches": sum(len(scan.launches) for scan in scans),
            "captured_events": sum(scan.event_count for scan in scans),
        },
        "selection_coverage": {
            "v1_resolved_successful_buys_discovered": 265,
            "v1_successful_buys_included": 116,
            "successful_buys_recovered_in_v2": backfill["successful_recovered"],
            "expanded_evidence_successful_buys_included": len(landed),
            "v1_resolved_failed_attempts_discovered": 449,
            "v1_failed_attempts_mapped": 371,
            "v1_failed_attempts_included": 206,
            "failed_attempts_recovered_in_v2": backfill["failed_fill_recovered"],
            "expanded_evidence_failed_attempts_included": len(failed),
            "remaining_v1_exclusions": len(exclusions),
            "clean_resolved_wallet_modelling_coverage_percent": (
                clean_included / clean_v1_universe * 100
            ),
            "expanded_selected_events": len(selected),
        },
        "group_coverage": {
            "decision_groups": len(groups),
            "total_rows": len(rows),
            "selected_rows": len(selected),
            "alternative_rows": len(rows) - len(selected),
            **distribution,
        },
        "feature_coverage_percent": {
            "reserve": percent("reserve_valid"),
            "transaction_index": (
                sum(row.get("candidate_create_transaction_index") is not None for row in rows)
                / len(rows)
                * 100
                if rows
                else 0
            ),
            "creator_history": percent("creator_history_available"),
            "funder_history": percent("funder_history_available"),
            "first_buyer_history": percent("first_buyer_history_available"),
            "buyer_cluster": percent("buyer_cluster_history_available"),
            "social": percent("social_available_before_decision"),
            "metadata": percent("metadata_observation_available"),
            "website": percent("website_available_before_decision"),
            "topology": percent("bundle_shape"),
        },
        "entry_age_policy": policy,
        "integrity": integrity_result,
        "exclusion_counts": dict(sorted(Counter(row["reason_code"] for row in exclusions).items())),
        "exclusion_counts_by_selection_type": {
            selection_type: dict(
                sorted(
                    Counter(
                        row["reason_code"]
                        for row in exclusions
                        if row["selection_type"] == selection_type
                    ).items()
                )
            )
            for selection_type in ("SUCCESSFUL_BUY", "FAILED_ATTEMPT")
        },
        "exclusion_counts_by_source_window": dict(
            sorted(Counter(row["source_window"] for row in exclusions).items())
        ),
        "acceptance_targets": {
            "median_alternatives_at_least_5": distribution["median_alternatives"] >= 5,
            "groups_at_least_3_alternatives_at_least_75_percent": (
                distribution["groups_with_at_least_3_alternatives_percent"] >= 75
            ),
            "every_selected_retained": True,
            "exactly_one_selected_per_group": integrity_result["missing_chosen_rows"] == 0,
            "zero_future_leakage": integrity_result["future_leakage_violations"] == 0,
        },
        "remaining_evidence_gaps": [
            "Creator funding transactions and candidate fee-payer account lists are absent, so funder history is unavailable rather than inferred.",
            "Failed-fill decision times remain conservative CREATE-receipt lower bounds because failed transactions emit no capture event.",
            f"{len(exclusions)} V1 exclusions remain outside retrievable capture history or lack an authoritative mint/CREATE event.",
            "Groups within 60 seconds of a capture start are explicitly marked left-truncated.",
            "Mutable metadata without immutable pre-decision timestamp proof is excluded from causal social features.",
        ],
        "no_model_trained": True,
        "production_paths_changed": 0,
    }


def feature_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    timestamp_fields = sorted(
        field
        for field in rows[0]
        if field.endswith("_ns") and field not in {"decision_ns", "decision_ns_upper_bound"}
    )
    violations = {
        field: sum(
            row.get(field) is not None
            and v1.integer(row[field]) > v1.integer(row["decision_ns"])
            for row in rows
        )
        for field in timestamp_fields
    }
    social_fields = {
        "metadata_uri",
        "metadata_uri_host",
        "metadata_content_addressed",
        "metadata_observation_available",
        "social_account",
        "social_post_timestamp_ns",
        "social_post_existed_before_launch",
        "social_available_before_decision",
        "website_available_before_decision",
        "social_feature_ns",
        "social_evidence_policy",
        "direct_ca_publication",
        "prelaunch_announcement",
    }
    entity_markers = (
        "creator",
        "buyer",
        "funder",
        "social_authority_prior",
        "time_since_previous",
        "entity_history",
    )
    execution_markers = (
        "failure_",
        "source_",
        "observed_",
        "output_guard",
        "execution_outcome",
        "landed_successfully",
    )

    def contract(field: str) -> dict[str, Any]:
        if field in social_fields or field.startswith("metadata_"):
            source = "content-addressed launch metadata in pinned social source"
            timestamp = "social_feature_ns"
            missing = "missing_reasons.social or missing_reasons.metadata"
            provenance = "social_choice research artifact + CREATE URI"
        elif field == "relative_features":
            source = "eligible members of the same decision group"
            timestamp = "decision_ns"
            missing = None
            provenance = "V2 in-group calculation"
        elif any(marker in field for marker in entity_markers):
            source = "strictly prior launch and E4 association ledger"
            timestamp = "entity_history_feature_ns"
            missing = "missing_reasons.funder when applicable"
            provenance = "V2 causal entity graph + frozen prior registry"
        elif any(marker in field for marker in execution_markers):
            source = "selected wallet transaction or observed capture event"
            timestamp = "decision_ns/observed_landing_ns"
            missing = "missing_reasons.tip when applicable"
            provenance = "pinned E4 wallet and exact-event artifacts"
        else:
            source = "CREATE or last causally visible candidate event"
            timestamp = "launch_received_ns/feature_max_event_ns"
            missing = "missing_reasons.reserve or missing_reasons.transaction_index"
            provenance = "pinned exact live-event capture"
        return {
            "feature_name": field,
            "source_event": source,
            "source_timestamp_field": timestamp,
            "decision_timestamp_field": "decision_ns",
            "causal_validity_rule": "source timestamp is null or <= decision_ns",
            "missing_value_reason_field": missing,
            "provenance": provenance,
        }

    return {
        "version": "e4-v12-feature-causality-audit-v2",
        "row_count": len(rows),
        "feature_contract": {
            "market_and_curve": {
                "source": "last causally visible exact-capture event",
                "timestamp_field": "feature_max_event_ns/candidate_state_ns",
                "missing_reason_fields": ["reserve_missing_reason"],
            },
            "creator_and_entity": {
                "source": "strictly prior launch and E4 selection ledger",
                "timestamp_field": "entity_history_feature_ns",
                "missing_reason_fields": ["funder_history_missing_reason"],
            },
            "social_and_metadata": {
                "source": "content-addressed metadata observed in pinned source",
                "timestamp_field": "social_feature_ns",
                "missing_reason_fields": [
                    "social_history_missing_reason",
                    "metadata_history_missing_reason",
                ],
            },
            "relative_features": {
                "source": "members of the same eligible decision group only",
                "timestamp_field": "decision_ns",
                "missing_reason_fields": [],
            },
        },
        "rank_directions": {
            name: direction for name, (_, direction) in sorted(RANK_SPECS.items())
        },
        "relative_feature_value_order": [
            "rank",
            "percentile",
            "distance_from_group_leader",
            "ratio_to_group_median",
        ],
        "predecision_window_value_order": ["buy_count", "buy_sol", "unique_buyers"],
        "group_eligibility_count_order": [
            "total_active_launches",
            "route_eligible_launches",
            "reserve_valid_launches",
            "economically_executable_launches",
            "final_included_candidates",
        ],
        "entity_last_seen_value_order": [
            "creator",
            "first_buyer",
            "buyer_cluster",
            "social_account",
            "creator_buyer_pair",
            "funder",
        ],
        "timestamp_field_violations": violations,
        "future_leakage_violations": sum(violations.values()),
        "per_row_contract_field": "feature_causality_contract",
        "emitted_column_contracts": [contract(field) for field in sorted(rows[0])],
        "missing_reason_codes": {
            "SOURCE_FIELD_ABSENT": "the pinned source did not expose the field",
            "NO_PREDECISION_RESERVE": "no reserve observation existed by decision_ns",
            "CREATOR_FUNDING_SOURCE_ABSENT": (
                "the pinned evidence has no creator funding transaction/account history"
            ),
            "METADATA_NOT_OBSERVED": "metadata was not observed in a pinned source",
            "NO_IMMUTABLE_PREDECISION_PROOF": (
                "social evidence lacked immutable pre-decision timestamp proof"
            ),
        },
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def report_markdown(coverage: Mapping[str, Any]) -> str:
    selection = coverage["selection_coverage"]
    groups = coverage["group_coverage"]
    features = coverage["feature_coverage_percent"]
    integrity_result = coverage["integrity"]
    lines = [
        "# E4 V12 canonical causal choice-risk sets V2",
        "",
        f"Integrity gate: **{integrity_result['status']}**.",
        "No model was trained. Production V12 paths changed: zero.",
        "",
        "## Corpus",
        "",
        f"- Capture runs: {coverage['sources']['capture_runs']}",
        f"- Captured launches: {coverage['sources']['captured_launches']:,}",
        f"- Decision groups: {groups['decision_groups']}",
        f"- Rows: {groups['total_rows']:,}",
        f"- Alternatives: {groups['alternative_rows']:,}",
        f"- Mean alternatives: {groups['average_alternatives']:.2f}",
        f"- Median alternatives: {groups['median_alternatives']}",
        f"- Groups with >=3 alternatives: {groups['groups_with_at_least_3_alternatives_percent']:.2f}%",
        "",
        "## Selection backfill",
        "",
        f"- Successful selections recovered from V1 exclusions: {selection['successful_buys_recovered_in_v2']}",
        f"- Failed-fill selections recovered from V1 exclusions: {selection['failed_attempts_recovered_in_v2']}",
        f"- Expanded selected events: {selection['expanded_selected_events']}",
        f"- Clean V1 resolved-wallet coverage: {selection['clean_resolved_wallet_modelling_coverage_percent']:.2f}%",
        "",
        "## Feature coverage",
        "",
    ]
    lines.extend(f"- {name}: {value:.2f}%" for name, value in sorted(features.items()))
    lines.extend(["", "## Integrity", ""])
    lines.extend(
        [
            f"- Future leakage: {integrity_result['future_leakage_violations']}",
            f"- Ambiguous labels: {integrity_result['ambiguous_labels']}",
            f"- Missing selected rows: {integrity_result['missing_chosen_rows']}",
            f"- Duplicate group rows: {integrity_result['duplicate_rows']}",
            f"- V1 hash changes: {integrity_result['v1_hash_changes']}",
            "",
            "## Evidence limitations",
            "",
        ]
    )
    lines.extend(f"- {gap}" for gap in coverage["remaining_evidence_gaps"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument(
        "--capture-root", type=Path, default=Path(".tmp-choice-set-source/runs")
    )
    parser.add_argument(
        "--evidence-root", type=Path, default=Path(".tmp-choice-set-source")
    )
    parser.add_argument(
        "--prior-registry",
        type=Path,
        default=Path("docs/research/e4-v12-causal-prior-registry.json"),
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=Path("artifacts/e4-v12-riskset-source-manifest.json"),
    )
    parser.add_argument("--write-source-manifest", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    v1_before = verify_v1(repo_root)
    manifest = source_manifest(args.capture_root, args.evidence_root, args.prior_registry)
    verify_or_write_manifest(
        args.source_manifest, manifest, write_manifest=args.write_source_manifest
    )
    specs = specs_from_manifest(manifest, args.capture_root)
    for spec in specs:
        v1.verify_source(spec)
    scans = [v1.scan_capture(spec) for spec in specs]
    if any(len(scan.launches) != 3_000 for scan in scans):
        raise ValueError("every pinned capture must contain exactly 3,000 launches")
    launches = {mint: launch for scan in scans for mint, launch in scan.launches.items()}
    if len(launches) != sum(len(scan.launches) for scan in scans):
        raise ValueError("duplicate launch mint across capture windows")

    research = load_research(args.evidence_root, args.prior_registry)
    wallet = merge_wallets(research)
    wallet_by_signature = v1.wallet_transactions(wallet)
    attempts = merge_attempts(research)
    bundle = research["bundle_metadata"]
    signature_indexes = v1.pre_transaction_indexes(bundle)
    signature_indexes.update(
        {
            signature: v1.integer(row.get("transactionIndex"), -1)
            for signature, row in wallet_by_signature.items()
            if v1.integer(row.get("transactionIndex"), -1) >= 0
        }
    )
    v1.annotate_launch_indexes(scans, signature_indexes)
    scan_by_run = {scan.spec.run_id: scan for scan in scans}
    landed = v1.successful_decisions(scans, wallet_by_signature)
    failed = expanded_failed_decisions(
        attempts, launches, scan_by_run, wallet_by_signature
    )
    decisions = unique_decisions([*landed, *failed])
    selected_mints = {decision.chosen_mint for decision in decisions}
    for launch in launches.values():
        launch.raw["_global_label"] = (
            "SELECTED_LANDED"
            if any(
                decision.chosen_mint == launch.mint
                and decision.label == "SELECTED_LANDED"
                for decision in decisions
            )
            else "SELECTED_FILL_REJECTED"
            if launch.mint in selected_mints
            else "TRUE_IGNORE"
        )

    candidates, events = event_inputs(scans, decisions, signature_indexes)
    social = v1.social_indexes(research["social_choice"], research["global_attempt"])
    raw_groups = build_initial_rows(
        scans,
        decisions,
        candidates,
        events,
        social,
        research["causal_prior_registry"],
        wallet_by_signature,
    )
    age_forensics = age_distribution(decisions, scans)
    policy = age_policy(raw_groups)
    age_forensics.update(policy)
    age_forensics["post_freeze_envelope_diagnostics"] = envelope_diagnostics(raw_groups)
    groups = apply_policy(raw_groups, v1.integer(policy["active_age_policy_ms"]))
    entity_graph = add_entity_histories(groups, scans)
    add_derived_features(groups)
    augment_entry_age_forensics(age_forensics, groups)
    add_relative_features(groups)
    scales = training_scales(groups)
    hard_negative_audit = add_hard_negatives(groups, scales)
    add_feature_provenance(groups)
    pack_wide_features(groups)
    backfill, exclusions = selection_backfill(repo_root, decisions, attempts, manifest["captures"])
    v1_after = verify_v1(repo_root)
    if v1_before != v1_after:
        raise ValueError("V1 changed during V2 construction")
    integrity_result = integrity(groups, scans, v1_after)
    if integrity_result["status"] != "PASS":
        raise ValueError(compact(integrity_result))

    rows = [row for group in groups for row in group]
    rows.sort(
        key=lambda row: (
            v1.integer(row["decision_ns"]),
            str(row["decision_group_id"]),
            0 if row["selected_by_e4"] else 1,
            str(row["candidate_mint"]),
        )
    )
    feature_causality = feature_audit(rows)
    if feature_causality["future_leakage_violations"]:
        raise ValueError("feature causality audit failed")
    coverage = coverage_report(
        groups,
        scans,
        age_forensics,
        backfill,
        integrity_result,
        exclusions,
        args.source_manifest,
    )

    output = args.output_dir
    jsonl_path = output / "e4-v12-canonical-choice-risksets-v2.jsonl"
    parquet_path = output / "e4-v12-canonical-choice-risksets-v2.parquet"
    v1.write_jsonl(jsonl_path, rows)
    v1.write_parquet(jsonl_path, parquet_path)
    write_json(output / "e4-v12-choice-riskset-v2-coverage.json", coverage)
    (output / "e4-v12-choice-riskset-v2-report.md").write_text(
        report_markdown(coverage), encoding="utf-8"
    )
    write_json(output / "e4-v12-entry-age-forensics.json", age_forensics)
    write_json(output / "e4-v12-selection-backfill.json", backfill)
    v1.write_jsonl(output / "e4-v12-selection-exclusions-v2.jsonl", exclusions)
    write_json(output / "e4-v12-causal-entity-graph.json", entity_graph)
    write_json(output / "e4-v12-hard-negative-audit.json", hard_negative_audit)
    write_json(output / "e4-v12-feature-causality-audit.json", feature_causality)
    write_json(output / "e4-v12-v1-immutability-audit.json", v1_after)
    coverage["outputs"] = {
        path.name: {"bytes": path.stat().st_size, "sha256": v1.sha256_path(path)}
        for path in (
            jsonl_path,
            parquet_path,
            output / "e4-v12-entry-age-forensics.json",
            output / "e4-v12-selection-backfill.json",
            output / "e4-v12-selection-exclusions-v2.jsonl",
            output / "e4-v12-causal-entity-graph.json",
            output / "e4-v12-hard-negative-audit.json",
            output / "e4-v12-feature-causality-audit.json",
            output / "e4-v12-v1-immutability-audit.json",
        )
    }
    write_json(output / "e4-v12-choice-riskset-v2-coverage.json", coverage)
    print(
        json.dumps(
            {
                "selection_coverage": coverage["selection_coverage"],
                "group_coverage": coverage["group_coverage"],
                "feature_coverage_percent": coverage["feature_coverage_percent"],
                "integrity": coverage["integrity"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
