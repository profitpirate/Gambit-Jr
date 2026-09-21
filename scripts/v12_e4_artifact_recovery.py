"""Recover explicit E4 evidence from retained GitHub Actions artifact ZIPs.

ZIPs are parsed in-place: nothing is extracted to the filesystem, path traversal
is impossible, and oversized/raw market payloads are skipped. The parser reuses
the lifetime-ledger admission rules so Gambit replay positions cannot enter the
E4 history by accident.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_lifetime_ledger as ledger

MAX_JSON_BYTES = 64 * 1024 * 1024


def nested_documents(
    payload: dict[str, Any],
    *,
    prefix: str = "root",
    depth: int = 0,
) -> list[tuple[str, dict[str, Any]]]:
    """Expose nested mappings without traversing huge raw-event lists."""
    found = [(prefix, payload)]
    if depth >= 5:
        return found
    for key, value in payload.items():
        if isinstance(value, dict):
            found.extend(
                nested_documents(
                    value,
                    prefix=f"{prefix}.{key}",
                    depth=depth + 1,
                )
            )
    return found


def scan_zip(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    documents = 0
    skipped_oversized = 0
    skipped_invalid = 0
    claims = 0

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".json"):
                continue
            if info.file_size > MAX_JSON_BYTES:
                skipped_oversized += 1
                continue
            try:
                payload = json.loads(archive.read(info))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                skipped_invalid += 1
                continue
            if not isinstance(payload, dict):
                continue
            documents += 1
            base_source = f"artifact://{path.stem}/{info.filename}"
            for nested_path, candidate in nested_documents(payload):
                source = f"{base_source}#{nested_path}"
                found_trades, found_attempts, found_claims = ledger.extract_document(
                    source, candidate
                )
                trades.extend(found_trades)
                attempts.extend(found_attempts)
                claims += len(found_claims)

    return trades, attempts, {
        "archive": path.name,
        "json_documents_scanned": documents,
        "trade_rows_extracted": len(trades),
        "attempt_rows_extracted": len(attempts),
        "aggregate_claims_seen": claims,
        "oversized_json_skipped": skipped_oversized,
        "invalid_json_skipped": skipped_invalid,
    }


def build(zip_root: Path) -> dict[str, Any]:
    raw_trades: list[dict[str, Any]] = []
    raw_attempts: list[dict[str, Any]] = []
    manifest = []
    archives = sorted(zip_root.glob("*.zip"))
    for path in archives:
        trades, attempts, report = scan_zip(path)
        raw_trades.extend(trades)
        raw_attempts.extend(attempts)
        manifest.append(report)

    trades = ledger.dedupe_trades(raw_trades)
    attempts = ledger.dedupe_attempts(raw_attempts)
    return {
        "schema_version": "e4-lifetime-artifact-recovery-v1",
        "artifact_archives_scanned": len(archives),
        "json_documents_scanned": sum(
            row["json_documents_scanned"] for row in manifest
        ),
        "trade_rows_before_dedupe": len(raw_trades),
        "closed_trade_records": len(trades),
        "attempt_rows_before_dedupe": len(raw_attempts),
        "attempt_records": len(attempts),
        "manifest": manifest,
        "e4_positions": trades,
        "outcomes": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("models/e4/e4-lifetime-artifact-recovery.json"),
    )
    args = parser.parse_args()
    result = build(args.zip_root)
    ledger.write(args.output, result)
    print(json.dumps({
        key: value
        for key, value in result.items()
        if key not in {"e4_positions", "outcomes", "manifest"}
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
