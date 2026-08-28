from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .backfill import BackfillEngine
from .providers import JsonlHistoricalProvider, OperationalSnapshotProvider
from .store import ApprovedFeatureStore, HistoricalWarehouse


def _read(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Gambit Jr offline historical pipeline")
    value.add_argument("--warehouse", default="data/historical/warehouse.db")
    value.add_argument("--archive", default="data/archive/historical")
    commands = value.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest-jsonl", help="Checkpoint a provider JSONL export")
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--page-size", type=int, default=500)
    ingest.add_argument("--job-id")

    observed = commands.add_parser(
        "ingest-operational", help="Archive actually observed live market snapshots"
    )
    observed.add_argument("--database", default="data/memecoin.db")
    observed.add_argument("--page-size", type=int, default=500)
    observed.add_argument("--job-id")

    commands.add_parser("coverage", help="Print the machine-readable historical coverage map")

    approve = commands.add_parser("approve-feature", help="Publish a manual feature approval")
    approve.add_argument("--feature-store", default="data/production/approved_features.db")
    approve.add_argument("--approval", required=True)
    return value


async def _run(args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    if args.command == "approve-feature":
        store = ApprovedFeatureStore(args.feature_store)
        try:
            store.approve(_read(args.approval))
            return {"state": "APPROVED", "approval": args.approval}
        finally:
            store.close()

    warehouse = HistoricalWarehouse(args.warehouse, args.archive)
    try:
        if args.command == "coverage":
            return warehouse.coverage_map()
        if args.command == "ingest-jsonl":
            manifest = _read(args.manifest)
            warehouse.register_dataset(manifest)
            provider = JsonlHistoricalProvider(
                args.input,
                manifest["dataset_id"],
                manifest["provider"],
                args.page_size,
            )
        else:
            manifest = {
                "dataset_id": "gambit-jr-observed-market",
                "dataset_version": "v1",
                "provider": "gambit_jr_operational_store",
                "chain": "multi",
                "acquisition_method": "read_only_sqlite_transfer",
                "refresh_method": "checkpointed_incremental_id",
                "timestamp_precision": "provider_snapshot",
                "reliability": "FIRST_PARTY_OBSERVED",
                "history_kind": "TRUE_HISTORICAL",
                "point_in_time_safe": True,
                "estimated_completeness": None,
                "missing_ranges_json": ["before first operational observation"],
                "cost_json": {"monthly_usd": 0},
            }
            warehouse.register_dataset(manifest)
            provider = OperationalSnapshotProvider(args.database, page_size=args.page_size)
        return await BackfillEngine(warehouse).run(provider, job_id=args.job_id)
    finally:
        warehouse.close()


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
