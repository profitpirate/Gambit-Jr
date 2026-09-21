"""Classify and reconcile retained E4 execution artifacts for lifetime history."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

PRIMARY_PATTERNS = (
    re.compile(r"^e4-live-market-stress-", re.I),
    re.compile(r"^e4-long-oracle-holdout-", re.I),
    re.compile(r"^e4-v11-forward-batch-", re.I),
    re.compile(r"^e4-v12-forward-\d+$", re.I),
    re.compile(r"^e4-v12-full-wallet-history-", re.I),
    re.compile(r"^e4-v12-v11-forward-", re.I),
    re.compile(r"^e4-exact-300-launch-holdout-", re.I),
    re.compile(r"^e4-v(?:7|8|11)-.*300-launch", re.I),
)


def is_primary(name: str) -> bool:
    return any(pattern.search(name) for pattern in PRIMARY_PATTERNS)


def build(
    inventory: dict[str, Any],
    recoveries: list[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = list(inventory.get("artifacts") or [])
    primary = [row for row in artifacts if is_primary(str(row.get("name") or ""))]
    recovered_ids: set[int] = set()
    for recovery in recoveries:
        for row in recovery.get("manifest") or []:
            archive = Path(str(row.get("archive") or ""))
            try:
                recovered_ids.add(int(archive.stem))
            except ValueError:
                continue

    retained = [row for row in primary if not row.get("expired")]
    expired = [row for row in primary if row.get("expired")]
    missing = [row for row in retained if int(row.get("id") or 0) not in recovered_ids]
    recovered = [row for row in retained if int(row.get("id") or 0) in recovered_ids]

    return {
        "schema_version": "e4-lifetime-source-reconciliation-v1",
        "primary_artifact_count": len(primary),
        "retained_primary_artifacts": len(retained),
        "expired_primary_artifacts": len(expired),
        "recovered_retained_primary_artifacts": len(recovered),
        "missing_retained_primary_artifacts": len(missing),
        "content_reconciliation_complete": not missing and not expired,
        "recovered_compressed_bytes": sum(
            int(row.get("size_in_bytes") or 0) for row in recovered
        ),
        "missing_compressed_bytes": sum(
            int(row.get("size_in_bytes") or 0) for row in missing
        ),
        "missing": [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "size_in_bytes": row.get("size_in_bytes"),
                "expires_at": row.get("expires_at"),
                "workflow_run_id": row.get("workflow_run_id"),
            }
            for row in missing
        ],
        "expired": [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "workflow_run_id": row.get("workflow_run_id"),
            }
            for row in expired
        ],
        "definition": (
            "Primary execution evidence must either be parsed or be explicitly "
            "reconciled to an equivalent committed source before certification."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("research/e4-lifetime-artifact-inventory.json"),
    )
    parser.add_argument(
        "--recovery",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/e4-lifetime-source-reconciliation.json"),
    )
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    recovery_paths = args.recovery or [
        Path("models/e4/e4-lifetime-artifact-recovery.json"),
        Path("models/e4/e4-lifetime-artifact-recovery-tier2.json"),
    ]
    recoveries = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in recovery_paths
        if path.exists()
    ]
    result = build(inventory, recoveries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        key: value
        for key, value in result.items()
        if key not in {"missing", "expired"}
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
