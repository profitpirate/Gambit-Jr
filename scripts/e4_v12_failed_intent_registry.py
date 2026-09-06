#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MINT_KEYS = (
    "mapped_mint",
    "mint",
    "token_mint",
    "token",
    "address",
)
TIMESTAMP_KEYS = (
    "attempt_ns",
    "received_ns",
    "timestamp_ns",
    "attempt_timestamp_ns",
    "attempt_time_ns",
    "attempt_time",
    "timestamp",
    "block_time",
    "blockTime",
)
SLOT_KEYS = ("attempt_slot", "slot", "block_slot")
TX_INDEX_KEYS = (
    "attempt_transaction_index",
    "transaction_index",
    "transactionIndex",
    "tx_index",
)
SIGNATURE_KEYS = ("attempt_signature", "signature", "tx_signature")

# This immutable run resolved the complete failed-attempt cohort against the
# same 36,000 captured launches used by the matched-control research. The
# artifact is intentionally restored at runtime rather than copied into source
# control, preserving provenance and preventing a stale hand-maintained list.
AUTHORITATIVE_RUN_ID = 33883557623
AUTHORITATIVE_ARTIFACT_NAME = "e4-v12-attempt-mint-forensics-33883557623"
DEFAULT_REPOSITORY = "profitpirate/Gambit-Jr"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def normal_timestamp_ns(value: Any) -> int:
    raw = finite(value)
    if raw <= 0:
        return 0
    if raw < 10_000_000_000:  # epoch seconds
        return int(raw * 1_000_000_000)
    if raw < 10_000_000_000_000:  # epoch milliseconds
        return int(raw * 1_000_000)
    if raw < 10_000_000_000_000_000:  # epoch microseconds
        return int(raw * 1_000)
    return int(raw)


def looks_like_attempt(row: Mapping[str, Any]) -> bool:
    if row.get("mapping_ok") is False:
        return False
    label = " ".join(
        str(row.get(key) or "")
        for key in (
            "label",
            "kind",
            "type",
            "status",
            "failure_type",
            "instruction",
            "buy_instruction",
            "program_error",
            "error",
        )
    ).lower()
    explicit = any(
        key in row
        for key in (
            "mapped_mint",
            "attempt_slot",
            "attempt_transaction_index",
            "failed_attempt",
            "attempt_signature",
        )
    )
    semantic = "failed" in label and any(
        term in label for term in ("buy", "entry", "exactsol", "slippage", "token")
    )
    return bool(explicit or semantic)


def iter_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            if isinstance(item, (Mapping, list, tuple)):
                yield from iter_mappings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_mappings(item)


def normalize_row(row: Mapping[str, Any], source: str) -> dict[str, Any] | None:
    if not looks_like_attempt(row):
        return None
    mint = str(first(row, MINT_KEYS) or "")
    if not mint:
        return None
    timestamp_ns = normal_timestamp_ns(first(row, TIMESTAMP_KEYS))
    slot = integer(first(row, SLOT_KEYS), -1)
    transaction_index = integer(first(row, TX_INDEX_KEYS), -1)
    signature = str(first(row, SIGNATURE_KEYS) or "")
    return {
        "mint": mint,
        "attempt_ns": timestamp_ns,
        "attempt_slot": slot,
        "attempt_transaction_index": transaction_index,
        "signature": signature,
        "error": str(
            row.get("error")
            or row.get("program_error")
            or row.get("failure_type")
            or ""
        ),
        "source": source,
    }


def candidate_json_files(paths: Sequence[Path]) -> list[Path]:
    output: list[Path] = []
    for root in paths:
        if root.is_file() and root.suffix.lower() == ".json":
            output.append(root)
        elif root.is_dir():
            output.extend(root.rglob("*.json"))
    return sorted(set(output))


def load_attempts(paths: Sequence[Path]) -> dict[str, list[dict[str, Any]]]:
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    for path in candidate_json_files(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for mapping in iter_mappings(payload):
            row = normalize_row(mapping, str(path))
            if row is None:
                continue
            key = (
                row["mint"],
                row["attempt_ns"],
                row["attempt_slot"],
                row["attempt_transaction_index"],
                row["signature"],
            )
            if key in seen:
                continue
            seen.add(key)
            by_mint[row["mint"]].append(row)
    for rows in by_mint.values():
        rows.sort(
            key=lambda row: (
                integer(row.get("attempt_ns"), 2**63 - 1) or 2**63 - 1,
                integer(row.get("attempt_slot"), 2**63 - 1),
                integer(row.get("attempt_transaction_index"), 2**31 - 1),
                str(row.get("signature") or ""),
            )
        )
    return dict(by_mint)


def restore_authoritative_attempts() -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Download the immutable failed-attempt mapping when branch scans are empty.

    Research artifacts are not committed to the matched-controls branch, so a
    source-tree-only scan silently produced zero labels. GitHub Actions already
    exposes the authenticated `gh` client and token; use them to restore the
    exact successful artifact by immutable run ID. Outside Actions this remains
    fail-closed and simply returns no rows.
    """
    if os.getenv("E4_FAILED_INTENT_ARTIFACT_RESTORE", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return {}, None
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    gh = shutil.which("gh")
    if not token or not gh:
        return {}, None

    repository = os.getenv("GITHUB_REPOSITORY") or os.getenv("REPO") or DEFAULT_REPOSITORY
    run_id = integer(
        os.getenv("E4_FAILED_INTENT_ARTIFACT_RUN_ID"), AUTHORITATIVE_RUN_ID
    )
    artifact_name = (
        os.getenv("E4_FAILED_INTENT_ARTIFACT_NAME")
        or AUTHORITATIVE_ARTIFACT_NAME
    )
    with tempfile.TemporaryDirectory(prefix="e4-v12-failed-intents-") as temporary:
        destination = Path(temporary)
        command = [
            gh,
            "run",
            "download",
            str(run_id),
            "--repo",
            repository,
            "--name",
            artifact_name,
            "--dir",
            str(destination),
        ]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "GH_TOKEN": token},
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "artifact download failed").strip()
            print(
                json.dumps(
                    {
                        "authoritative_artifact_restored": False,
                        "run_id": run_id,
                        "artifact": artifact_name,
                        "error": message[-1_000:],
                    },
                    sort_keys=True,
                )
            )
            return {}, None
        attempts = load_attempts([destination])
        source = next(
            (
                str(path)
                for path in destination.rglob("*.json")
                if path.is_file()
            ),
            str(destination),
        )
        print(
            json.dumps(
                {
                    "authoritative_artifact_restored": True,
                    "run_id": run_id,
                    "artifact": artifact_name,
                    "mint_count": len(attempts),
                    "attempt_count": sum(len(rows) for rows in attempts.values()),
                },
                sort_keys=True,
            )
        )
        return attempts, source


def estimate_attempt_ns(
    attempt: Mapping[str, Any],
    launch_create_ns: int,
    launch_create_slot: int,
    events: Sequence[Mapping[str, Any]],
) -> int:
    explicit = integer(attempt.get("attempt_ns"))
    if explicit > 0:
        return explicit
    slot = integer(attempt.get("attempt_slot"), launch_create_slot)
    transaction_index = integer(attempt.get("attempt_transaction_index"), -1)
    same_slot = [
        row for row in events if integer(row.get("slot"), -1) == slot
    ]
    if same_slot:
        exact = []
        for row in same_slot:
            raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else {}
            index = integer(
                row.get(
                    "transaction_index",
                    raw.get("transaction_index", raw.get("transactionIndex", -1)),
                ),
                -1,
            )
            if transaction_index >= 0 and index == transaction_index:
                exact.append(integer(row.get("received_ns")))
        values = [value for value in exact if value > 0]
        if values:
            return min(values)
        values = [
            integer(row.get("received_ns"))
            for row in same_slot
            if integer(row.get("received_ns")) > 0
        ]
        if values:
            return min(values)
    slot_delta = max(0, slot - launch_create_slot)
    return launch_create_ns + slot_delta * 400_000_000


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize failed E4 BUY intentions from research JSON"
    )
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    attempts = load_attempts(args.source)
    provenance: dict[str, Any] = {
        "requested_sources": [str(path) for path in args.source],
        "restored_authoritative_artifact": False,
    }
    if not attempts:
        attempts, restored_source = restore_authoritative_attempts()
        provenance["restored_authoritative_artifact"] = bool(attempts)
        provenance["authoritative_run_id"] = AUTHORITATIVE_RUN_ID
        provenance["authoritative_artifact_name"] = AUTHORITATIVE_ARTIFACT_NAME
        provenance["restored_source"] = restored_source

    payload = {
        "version": "e4-v12-failed-intent-registry-v2",
        "mint_count": len(attempts),
        "attempt_count": sum(len(rows) for rows in attempts.values()),
        "attempts_by_mint": attempts,
        "provenance": provenance,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mint_count": payload["mint_count"],
                "attempt_count": payload["attempt_count"],
                "restored_authoritative_artifact": provenance[
                    "restored_authoritative_artifact"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
