"""Merge sharded E4 artifact-recovery outputs into one deduplicated evidence file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v12_e4_lifetime_ledger as ledger


def build(parts: list[dict[str, Any]]) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    archives = 0
    documents = 0
    for part in parts:
        trades.extend(part.get("e4_positions") or [])
        attempts.extend(part.get("outcomes") or [])
        manifest.extend(part.get("manifest") or [])
        archives += int(part.get("artifact_archives_scanned") or 0)
        documents += int(part.get("json_documents_scanned") or 0)

    deduped_trades = ledger.dedupe_trades(trades)
    deduped_attempts = ledger.dedupe_attempts(attempts)
    return {
        "schema_version": "e4-lifetime-artifact-recovery-v1",
        "artifact_archives_scanned": archives,
        "json_documents_scanned": documents,
        "trade_rows_before_dedupe": len(trades),
        "closed_trade_records": len(deduped_trades),
        "attempt_rows_before_dedupe": len(attempts),
        "attempt_records": len(deduped_attempts),
        "manifest": manifest,
        "e4_positions": deduped_trades,
        "outcomes": deduped_attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parts = []
    for path in sorted(args.part_root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == "e4-lifetime-artifact-recovery-v1"
        ):
            parts.append(value)
    if not parts:
        raise SystemExit("no recovery parts found")
    result = build(parts)
    ledger.write(args.output, result)
    print(json.dumps({
        key: value
        for key, value in result.items()
        if key not in {"manifest", "e4_positions", "outcomes"}
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
