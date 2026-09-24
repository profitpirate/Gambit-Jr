#!/usr/bin/env python3
"""Deterministic, concurrency-safe registry for E4 V12 research experiments."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Self

REGISTRY_VERSION = "e4-v12-experiment-registry-v1"
LEADERBOARD_VERSION = "e4-v12-experiment-leaderboard-v1"
IDENTITY_VERSION = "e4-v12-experiment-identity-v1"

IDENTITY_FIELDS = (
    "thesis_family_identifier",
    "source_code_fingerprint",
    "dataset_source_manifest_fingerprint",
    "evidence_epoch",
    "feature_set_fingerprint",
    "model_family",
    "full_parameters",
    "causal_horizon",
    "candidate_risk_set_policy",
    "chronological_split",
    "bankroll",
    "position_sizing",
    "fee_model",
    "output_guard",
    "latency_assumptions",
    "execution_policy",
    "exit_policy",
)

FAILURE_CLASSES = frozenset(
    {
        "DATA_INTEGRITY_FAILURE",
        "INFRASTRUCTURE_FAILURE",
        "INSUFFICIENT_LABELS",
        "NO_TRAIN_SURVIVOR",
        "VALIDATION_COLLAPSE",
        "HOLDOUT_COLLAPSE",
        "LIVE_COLLAPSE",
        "FALSE_POSITIVE_OVERLOAD",
        "NEGATIVE_EXPECTANCY",
        "INADEQUATE_PROFIT_FACTOR",
        "LATENCY_FAILURE",
        "OUTPUT_DETERIORATION",
        "EXECUTION_FAILURE",
        "EXIT_FAILURE",
        "DUPLICATE_EXPERIMENT",
    }
)
RETRYABLE_FAILURE_CLASSES = frozenset({"INFRASTRUCTURE_FAILURE"})
SCIENTIFIC_FAILURE_CLASSES = (
    FAILURE_CLASSES - RETRYABLE_FAILURE_CLASSES - {"DUPLICATE_EXPERIMENT"}
)
ACTIVE_STATES = frozenset({"DISPATCHED", "QUEUED", "IN_PROGRESS", "WAITING"})
PASSED_STATES = frozenset({"GOLDEN_GATE_PASSED", "UNTOUCHED_LIVE_PASSED"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalise(item) for item in value), key=canonical_json)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        return iso_timestamp(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("experiment identity cannot contain NaN or infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalise(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in IDENTITY_FIELDS if field not in identity]
    extra = sorted(set(identity) - set(IDENTITY_FIELDS))
    if missing or extra:
        raise ValueError(
            f"invalid experiment identity fields: missing={missing}, extra={extra}"
        )
    normalised = _normalise(identity)
    for field in IDENTITY_FIELDS:
        if normalised[field] is None or normalised[field] == "":
            raise ValueError(f"experiment identity field is empty: {field}")
    return normalised


def experiment_id(identity: Mapping[str, Any]) -> str:
    payload = {
        "identity_version": IDENTITY_VERSION,
        "identity": validate_identity(identity),
    }
    return f"e4x-{sha256_value(payload)}"


def empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "records": []}


def validate_registry(document: Mapping[str, Any]) -> None:
    if document.get("version") != REGISTRY_VERSION:
        raise ValueError("unsupported experiment registry version")
    records = document.get("records")
    if not isinstance(records, list):
        raise TypeError("experiment registry records must be a list")
    ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError("experiment registry record must be an object")
        identifier = str(record.get("experiment_id") or "")
        if not identifier:
            raise ValueError("experiment registry record has no experiment_id")
        if identifier in ids:
            raise ValueError(f"duplicate experiment registry record: {identifier}")
        ids.add(identifier)
        if identifier != experiment_id(record.get("identity") or {}):
            raise ValueError(f"experiment identity hash mismatch: {identifier}")
        failure = record.get("failure_classification")
        if failure is not None and failure not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure classification: {failure}")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(
        path,
        (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
    )


class FileLock(AbstractContextManager["FileLock"]):
    """Small cross-platform lock based on atomic file creation."""

    def __init__(self, path: Path, *, timeout_seconds: float = 20.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.acquired = False

    def __enter__(self) -> Self:
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()} created={iso_timestamp()}\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > 300
                    if stale:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring registry lock: {self.path}"
                    )
                time.sleep(0.02)

    def __exit__(self, *_: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


def new_record(
    identity: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalised_identity = validate_identity(identity)
    timestamp = iso_timestamp(now)
    failure = metadata.get("failure_classification")
    if failure is not None and failure not in FAILURE_CLASSES:
        raise ValueError(f"unknown failure classification: {failure}")
    state = str(metadata.get("state") or "REGISTERED")
    retired = bool(metadata.get("retired", failure in SCIENTIFIC_FAILURE_CLASSES))
    retirement_reason = metadata.get("retirement_reason")
    if retired and not retirement_reason and failure:
        retirement_reason = str(failure)
    return {
        "experiment_id": experiment_id(normalised_identity),
        "identity": normalised_identity,
        "thesis_family": normalised_identity["thesis_family_identifier"],
        "created_timestamp": timestamp,
        "updated_timestamp": timestamp,
        "source_commit": metadata.get("source_commit"),
        "dataset_version": metadata.get("dataset_version"),
        "dataset_source_manifest": metadata.get("dataset_source_manifest"),
        "evidence_epoch": normalised_identity["evidence_epoch"],
        "train_metrics": metadata.get("train_metrics"),
        "validation_metrics": metadata.get("validation_metrics"),
        "chronological_holdout_metrics": metadata.get("chronological_holdout_metrics"),
        "live_metrics": metadata.get("live_metrics"),
        "trade_count": metadata.get("trade_count"),
        "wr": metadata.get("wr"),
        "wilson_lower_bound": metadata.get("wilson_lower_bound"),
        "pnl": metadata.get("pnl"),
        "profit_factor": metadata.get("profit_factor"),
        "drawdown": metadata.get("drawdown"),
        "selection_precision": metadata.get("selection_precision"),
        "recall": metadata.get("recall"),
        "latency_results": metadata.get("latency_results"),
        "live_status": metadata.get("live_status"),
        "failure_classification": failure,
        "exact_artifact_paths": list(metadata.get("exact_artifact_paths") or []),
        "retired": retired,
        "reason_for_retirement": retirement_reason,
        "material_change_required_before_rerun": metadata.get(
            "material_change_required_before_rerun"
        ),
        "state": state,
        "workflow": metadata.get("workflow"),
        "ref": metadata.get("ref"),
        "latest_run_id": metadata.get("latest_run_id"),
        "latest_run_url": metadata.get("latest_run_url"),
        "dispatch_attempts": int(metadata.get("dispatch_attempts") or 0),
        "retry_not_before": metadata.get("retry_not_before"),
        "duplicate_suppression_count": 0,
        "suppressed_duplicate_run_ids": [],
        "last_duplicate_failure_classification": None,
    }


class RegistryStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        lock_timeout_seconds: float = 20.0,
    ) -> None:
        self.path = path
        self.clock = clock
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_registry()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        validate_registry(document)
        return document

    def _mutate(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        with FileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds):
            document = self.load()
            result = operation(document)
            document["records"] = sorted(
                document["records"], key=lambda row: str(row["experiment_id"])
            )
            validate_registry(document)
            atomic_write_json(self.path, document)
            return result

    def register(
        self, identity: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        identifier = experiment_id(identity)

        def operation(document: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            for record in document["records"]:
                if record["experiment_id"] == identifier:
                    return dict(record), False
            record = new_record(identity, metadata, now=self.clock())
            document["records"].append(record)
            return dict(record), True

        return self._mutate(operation)

    def update(self, identifier: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        if "experiment_id" in changes or "identity" in changes:
            raise ValueError("experiment identity is immutable")
        failure = changes.get("failure_classification")
        if failure is not None and failure not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure classification: {failure}")

        def operation(document: dict[str, Any]) -> dict[str, Any]:
            for record in document["records"]:
                if record["experiment_id"] != identifier:
                    continue
                record.update(_normalise(changes))
                record["updated_timestamp"] = iso_timestamp(self.clock())
                if failure in SCIENTIFIC_FAILURE_CLASSES:
                    record["retired"] = True
                    record["reason_for_retirement"] = changes.get(
                        "reason_for_retirement", failure
                    )
                elif failure in RETRYABLE_FAILURE_CLASSES:
                    record["retired"] = False
                return dict(record)
            raise KeyError(identifier)

        return self._mutate(operation)

    def suppress_duplicate(
        self, identifier: str, *, run_id: int | None = None
    ) -> dict[str, Any]:
        def operation(document: dict[str, Any]) -> dict[str, Any]:
            for record in document["records"]:
                if record["experiment_id"] != identifier:
                    continue
                seen = [
                    int(value)
                    for value in record.get("suppressed_duplicate_run_ids") or []
                ]
                if run_id is None or run_id not in seen:
                    record["duplicate_suppression_count"] = (
                        int(record.get("duplicate_suppression_count") or 0) + 1
                    )
                    if run_id is not None:
                        seen.append(run_id)
                    record["suppressed_duplicate_run_ids"] = sorted(set(seen))
                    record["last_duplicate_failure_classification"] = (
                        "DUPLICATE_EXPERIMENT"
                    )
                    record["updated_timestamp"] = iso_timestamp(self.clock())
                return dict(record)
            raise KeyError(identifier)

        return self._mutate(operation)


def record_by_id(document: Mapping[str, Any], identifier: str) -> dict[str, Any] | None:
    for record in document.get("records") or []:
        if str(record.get("experiment_id") or "") == identifier:
            return dict(record)
    return None


def retry_decision(
    record: Mapping[str, Any] | None,
    *,
    now: datetime,
    infrastructure_backoff_minutes: int,
    stale_active_minutes: int,
) -> tuple[str, str]:
    if record is None:
        return "ELIGIBLE_NOVEL", "experiment identity has not been tested"
    state = str(record.get("state") or "")
    updated = parse_timestamp(
        record.get("updated_timestamp") or record.get("created_timestamp")
    )
    age = (now - updated).total_seconds() / 60.0 if updated else float("inf")
    if state in ACTIVE_STATES:
        if age >= stale_active_minutes:
            return "ELIGIBLE_STALE_RESTART", "active job exceeded stale limit"
        return "ACTIVE", "identical experiment is already active"
    failure = record.get("failure_classification")
    if failure in RETRYABLE_FAILURE_CLASSES:
        retry_at = parse_timestamp(record.get("retry_not_before"))
        if retry_at is None:
            retry_at = (updated or now) + timedelta(
                minutes=infrastructure_backoff_minutes
            )
        if now >= retry_at:
            return "ELIGIBLE_INFRASTRUCTURE_RETRY", "infrastructure backoff elapsed"
        return (
            "INFRASTRUCTURE_BACKOFF",
            f"infrastructure retry is blocked until {iso_timestamp(retry_at)}",
        )
    if state in PASSED_STATES:
        return "PASSED", "identical experiment already passed its declared gate"
    return (
        "DUPLICATE_SCIENTIFIC",
        "scientific identity is unchanged; a material identity fingerprint must change",
    )


def _number(value: Any, default: float = float("-inf")) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def leaderboard_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_registry(document)
    rows: list[dict[str, Any]] = []
    for record in document["records"]:
        holdout = record.get("chronological_holdout_metrics")
        holdout = holdout if isinstance(holdout, Mapping) else {}
        holdout_wr = holdout.get("wr", holdout.get("win_rate"))
        pnl = holdout.get("pnl", record.get("pnl"))
        profit_factor = holdout.get("profit_factor", record.get("profit_factor"))
        drawdown = holdout.get("drawdown", record.get("drawdown"))
        has_holdout = any(
            value is not None for value in (holdout_wr, pnl, profit_factor, drawdown)
        )
        latency = record.get("latency_results")
        latency = latency if isinstance(latency, Mapping) else {}
        rows.append(
            {
                "rank": 0,
                "experiment_id": record["experiment_id"],
                "thesis_family": record.get("thesis_family"),
                "dataset": record.get("dataset_version"),
                "holdout_wr": holdout_wr,
                "pnl": pnl,
                "profit_factor": profit_factor,
                "drawdown": drawdown,
                "latency_coverage": latency.get("coverage", latency.get("count")),
                "live_status": record.get("live_status"),
                "failure_reason": record.get("failure_classification"),
                "next_required_change": record.get(
                    "material_change_required_before_rerun"
                ),
                "has_chronological_holdout": has_holdout,
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row["has_chronological_holdout"] else 1,
            -_number(row["holdout_wr"]),
            -_number(row["pnl"]),
            -_number(row["profit_factor"]),
            _number(row["drawdown"], float("inf")),
            str(row["experiment_id"]),
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def write_leaderboard(
    document: Mapping[str, Any], json_path: Path, markdown_path: Path
) -> None:
    rows = leaderboard_rows(document)
    atomic_write_json(
        json_path,
        {
            "version": LEADERBOARD_VERSION,
            "ranking_policy": "chronological_holdout_first",
            "rows": rows,
        },
    )
    lines = [
        "# E4 V12 unique-experiment leaderboard",
        "",
        "Train-only results are always ranked below experiments with chronological holdout evidence.",
        "",
        "| Rank | Experiment | Thesis | Dataset | Holdout WR | PnL | PF | Drawdown | Latency | Live | Failure | Next change |",
        "|---:|---|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        values = [
            row["rank"],
            f"`{row['experiment_id']}`",
            row["thesis_family"],
            row["dataset"],
            row["holdout_wr"],
            row["pnl"],
            row["profit_factor"],
            row["drawdown"],
            row["latency_coverage"],
            row["live_status"],
            row["failure_reason"],
            row["next_required_change"],
        ]
        safe = [
            str(value if value is not None else "-").replace("|", "/")
            for value in values
        ]
        lines.append("| " + " | ".join(safe) + " |")
    _atomic_write(markdown_path, ("\n".join(lines) + "\n").encode("utf-8"))
