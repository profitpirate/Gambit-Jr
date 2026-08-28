from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backfill import BackfillEngine
from .evidence_research import run_public_evidence_research, storage_projection, write_report
from .finalization import (
    measure_local_latency,
    normalize_ranked_pool_ohlcv,
    normalize_regime_dataset,
    run_real_research,
    write_completion_report,
)
from .providers import (
    BinanceKlineProvider,
    BirdeyeOhlcvProvider,
    DuneQueryProvider,
    GeckoTerminalOhlcvProvider,
    GeckoTerminalPoolProvider,
    JsonlHistoricalProvider,
    OperationalHistoryProvider,
    OperationalSnapshotProvider,
)
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

    operational = commands.add_parser(
        "ingest-operational-all",
        help="Archive all eligible Jr operational evidence without user/Discord configuration",
    )
    operational.add_argument("--database", default="data/memecoin.db")
    operational.add_argument("--page-size", type=int, default=500)
    operational.add_argument("--job-id")

    public = commands.add_parser(
        "finalize-public",
        help="Acquire and research the credential-free public evidence currently obtainable",
    )
    public.add_argument("--start", default="2021-01-01T00:00:00+00:00")
    public.add_argument("--end", default=datetime.now(UTC).isoformat())
    public.add_argument("--new-pool-pages", type=int, default=10)
    public.add_argument("--ranked-pool-pages", type=int, default=2)
    public.add_argument("--pool-history-count", type=int, default=20)
    public.add_argument("--report", default="outputs/v15-finalization-evidence.json")
    public.add_argument("--code-version", default="working-tree")

    evidence = commands.add_parser(
        "research-public-corpora",
        help="Verify and research checksum-pinned public launch corpora",
    )
    evidence.add_argument("--data-root", required=True)
    evidence.add_argument("--report", default="outputs/v15-real-evidence.json")
    evidence.add_argument("--code-version", default="working-tree")

    dune = commands.add_parser("ingest-dune", help="Ingest a reviewed Dune query result")
    dune.add_argument("--query-id", type=int, required=True)
    dune.add_argument("--chain", required=True)
    dune.add_argument("--entity-field", required=True)
    dune.add_argument("--observed-at-field", required=True)
    dune.add_argument("--available-at-field")
    dune.add_argument("--job-id")

    birdeye = commands.add_parser("ingest-birdeye-ohlcv", help="Ingest Birdeye token OHLCV")
    birdeye.add_argument("--addresses", required=True, help="JSON array file; never a private key")
    birdeye.add_argument("--start", required=True)
    birdeye.add_argument("--end", required=True)
    birdeye.add_argument("--chain", default="solana")
    birdeye.add_argument("--candle-type", default="1D")
    birdeye.add_argument("--job-id")

    commands.add_parser("coverage", help="Print the machine-readable historical coverage map")
    commands.add_parser("operator-status", help="Print offline research/operator status")

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
            return warehouse.coverage_manifest()
        if args.command == "operator-status":
            return warehouse.operator_status()
        if args.command == "finalize-public":
            return await _finalize_public(warehouse, args)
        if args.command == "research-public-corpora":
            report = run_public_evidence_research(
                args.data_root, warehouse=warehouse, code_version=args.code_version
            )
            report["storage_projection"] = storage_projection(report)
            write_report(report, args.report)
            return report
        if args.command == "ingest-dune":
            provider = DuneQueryProvider(
                args.query_id,
                os.getenv("DUNE_API_KEY"),
                chain=args.chain,
                entity_field=args.entity_field,
                observed_at_field=args.observed_at_field,
                available_at_field=args.available_at_field,
            )
            warehouse.register_dataset(
                _manifest(
                    provider.dataset_id,
                    f"dune-query-{args.query_id}-v1",
                    provider.name,
                    args.chain,
                    history_kind="TRUE_HISTORICAL",
                    precision="query-defined",
                    missing=["coverage is determined by the reviewed Dune SQL query"],
                    completeness=None,
                )
            )
            return await BackfillEngine(warehouse).run(provider, job_id=args.job_id)
        if args.command == "ingest-birdeye-ohlcv":
            addresses = json.loads(Path(args.addresses).read_text(encoding="utf-8"))
            if not isinstance(addresses, list) or not all(
                isinstance(address, str) for address in addresses
            ):
                raise ValueError("Birdeye addresses file must contain a JSON string array")
            provider = BirdeyeOhlcvProvider(
                addresses,
                args.start,
                args.end,
                os.getenv("BIRDEYE_API_KEY"),
                chain=args.chain,
                candle_type=args.candle_type,
            )
            warehouse.register_dataset(
                _manifest(
                    provider.dataset_id,
                    f"{args.start[:10]}_{args.end[:10]}_{args.candle_type}_v1",
                    provider.name,
                    args.chain,
                    history_kind="TRUE_HISTORICAL",
                    precision=f"{args.candle_type} candle close",
                    missing=["only explicitly supplied token addresses"],
                    completeness=None,
                )
            )
            return await BackfillEngine(warehouse).run(provider, job_id=args.job_id)
        if args.command == "ingest-jsonl":
            manifest = _read(args.manifest)
            warehouse.register_dataset(manifest)
            provider = JsonlHistoricalProvider(
                args.input,
                manifest["dataset_id"],
                manifest["provider"],
                args.page_size,
            )
        elif args.command == "ingest-operational":
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
        else:
            manifest = {
                "dataset_id": "gambit-jr-operational-evidence",
                "dataset_version": "v1.5-operational-transfer-v1",
                "provider": "gambit_jr_operational_store",
                "chain": "multi",
                "acquisition_method": "read_only_allowlisted_sqlite_transfer",
                "refresh_method": "checkpointed_table_rowid",
                "timestamp_precision": "original operational timestamp",
                "reliability": "FIRST_PARTY_OBSERVED",
                "history_kind": "TRUE_HISTORICAL",
                "point_in_time_safe": True,
                "estimated_completeness": None,
                "missing_ranges_json": ["before first operational observation"],
                "cost_json": {"monthly_usd": 0},
            }
            warehouse.register_dataset(manifest)
            provider = OperationalHistoryProvider(args.database, page_size=args.page_size)
        return await BackfillEngine(warehouse).run(provider, job_id=args.job_id)
    finally:
        warehouse.close()


def _manifest(
    dataset_id: str,
    version: str,
    provider: str,
    chain: str,
    *,
    history_kind: str,
    precision: str,
    missing: list[str],
    completeness: float | None,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "dataset_version": version,
        "provider": provider,
        "chain": chain,
        "acquisition_method": "credential_free_public_api",
        "refresh_method": "checkpointed_cursor",
        "timestamp_precision": precision,
        "reliability": "PUBLIC_PROVIDER",
        "history_kind": history_kind,
        "point_in_time_safe": True,
        "estimated_completeness": completeness,
        "missing_ranges_json": missing,
        "rate_limit_json": {"handled": True, "bounded_retries": True},
        "cost_json": {"class": "FREE_PUBLIC_ENDPOINT", "monthly_usd": 0},
    }


async def _finalize_public(
    warehouse: HistoricalWarehouse, args: argparse.Namespace
) -> dict[str, Any]:
    acquisition: dict[str, Any] = {}
    normalization: dict[str, Any] = {}
    engine = BackfillEngine(warehouse, max_retries=5, maximum_rate_limit_sleep_seconds=60)
    for symbol in ("BTCUSDT", "SOLUSDT", "BNBUSDT"):
        provider = BinanceKlineProvider(symbol, args.start, args.end)
        warehouse.register_dataset(
            _manifest(
                provider.dataset_id,
                f"{args.start[:10]}_{args.end[:10]}_1d_v1",
                provider.name,
                "market",
                history_kind="TRUE_HISTORICAL",
                precision="daily candle close",
                missing=[],
                completeness=1.0,
            )
        )
        acquisition[provider.dataset_id] = await engine.run(provider)
        normalization[provider.dataset_id] = normalize_regime_dataset(
            warehouse, provider.dataset_id, f"binance-{symbol.lower()}-regime-v1"
        )

    new_pools = GeckoTerminalPoolProvider(
        "solana", endpoint="new_pools", maximum_pages=args.new_pool_pages
    )
    warehouse.register_dataset(
        _manifest(
            new_pools.dataset_id,
            "acquisition-time-snapshots-v1",
            new_pools.name,
            "solana",
            history_kind="TRUE_HISTORICAL",
            precision="acquisition timestamp",
            missing=["all launches outside the free latest-ten-page rolling window"],
            completeness=None,
        )
    )
    acquisition[new_pools.dataset_id] = await engine.run(new_pools)
    warehouse.assess_coverage(
        new_pools.dataset_id,
        {
            "launch_platform": "multiple Solana DEXs",
            "normalized_rows": 0,
            "missing_ranges": ["all launches outside the latest-ten-page rolling window"],
            "completeness_estimate": None,
            "point_in_time_safe": True,
            "timestamp_precision": "acquisition timestamp",
            "survivorship_bias": "LOW_WITHIN_WINDOW_UNKNOWN_OUTSIDE_WINDOW",
            "quality_state": "REAL_CURRENT_SNAPSHOT_NO_MATURE_OUTCOME",
            "licensing_limitations": "Free API caps pagination at page ten; provider terms apply.",
            "cost_class": "FREE_PUBLIC_ENDPOINT",
            "information_gain": "Prospective launch-universe seed; not immediately research-mature.",
        },
    )

    ranked = GeckoTerminalPoolProvider(
        "solana", endpoint="pools", maximum_pages=args.ranked_pool_pages
    )
    warehouse.register_dataset(
        _manifest(
            ranked.dataset_id,
            "provider-ranked-snapshots-v1",
            ranked.name,
            "solana",
            history_kind="TRUE_HISTORICAL",
            precision="acquisition timestamp",
            missing=["pools omitted by current provider ranking"],
            completeness=None,
        )
    )
    acquisition[ranked.dataset_id] = await engine.run(ranked)
    unique_ranked = {
        str((row.get("attributes") or {}).get("address") or row.get("id")): row
        for row in ranked.discovered
    }
    chronological_ranked = sorted(
        unique_ranked.values(),
        key=lambda row: str((row.get("attributes") or {}).get("pool_created_at") or ""),
    )
    sample_size = min(max(0, args.pool_history_count), len(chronological_ranked))
    selected = (
        []
        if sample_size == 0
        else [chronological_ranked[0]]
        if sample_size == 1
        else [
            chronological_ranked[round(index * (len(chronological_ranked) - 1) / (sample_size - 1))]
            for index in range(sample_size)
        ]
    )
    ohlcv = GeckoTerminalOhlcvProvider("solana", selected)
    warehouse.register_dataset(
        _manifest(
            ohlcv.dataset_id,
            "ranked-pool-daily-ohlcv-v1",
            ohlcv.name,
            "solana",
            history_kind="TRUE_HISTORICAL",
            precision="daily candle close",
            missing=[
                "all pools outside current provider ranking",
                "transaction-level and liquidity-removal history",
            ],
            completeness=None,
        )
    )
    acquisition[ohlcv.dataset_id] = await engine.run(ohlcv)
    normalization[ohlcv.dataset_id] = normalize_ranked_pool_ohlcv(
        warehouse, ohlcv.dataset_id
    )

    for requirement in (
        {
            "source_name": "gambit_jr_production_operational_database",
            "credential_name": "read-only copy of DATABASE_PATH",
            "expected_coverage": "All evidence observed since the production database began",
            "cost_class": "NO_PROVIDER_COST",
            "expected_information_gain": "Highest: unbiased Jr calls, rejections, misses and outcomes",
            "state": "CONFIG_REQUIRED",
            "limitation": "Production database is not present in this local workspace",
        },
        {
            "source_name": "coingecko_onchain_analyst",
            "credential_name": "COINGECKO_API_KEY",
            "expected_coverage": "Pagination beyond ten pages and deeper on-chain history",
            "cost_class": "PAID_SUBSCRIPTION",
            "expected_information_gain": "Broader launch universe and reduced rolling-window gaps",
            "state": "CREDENTIAL_REQUIRED",
            "limitation": "Free endpoint returned a hard page-ten cap",
        },
        {
            "source_name": "birdeye_historical",
            "credential_name": "BIRDEYE_API_KEY",
            "expected_coverage": "Token OHLCV, trades, holders and wallet activity by plan",
            "cost_class": "PAID_OR_LIMITED_FREE_TIER",
            "expected_information_gain": "Transaction-level runner/failure and wallet cohorts",
            "state": "CREDENTIAL_REQUIRED",
            "limitation": "No credential supplied; adapter promotion requires licensing review",
        },
        {
            "source_name": "dune_solana_exports",
            "credential_name": "DUNE_API_KEY",
            "expected_coverage": "Query-dependent Solana launches, trades and actor relationships",
            "cost_class": "FREE_TIER_OR_PAID_COMPUTE",
            "expected_information_gain": "Potentially unbiased dead/non-runner and actor corpora",
            "state": "CREDENTIAL_AND_QUERY_REQUIRED",
            "limitation": "No API key or validated query identifiers supplied",
        },
        {
            "source_name": "historical_social_firehose",
            "credential_name": "provider-specific archive credential",
            "expected_coverage": "Pre-launch posts, unique mentioners and account history",
            "cost_class": "PAID_ARCHIVE",
            "expected_information_gain": "Narrative freshness, preparation and bot-resistance tests",
            "state": "UNAVAILABLE",
            "limitation": "Static current social metadata cannot be converted to historical velocity",
        },
    ):
        warehouse.record_acquisition_requirement(requirement)

    research = None
    if normalization[ohlcv.dataset_id]["outcomes"] >= 6:
        try:
            research = run_real_research(warehouse, args.code_version)
        except ValueError as error:
            research = {"state": "NOT_RUN", "reason": str(error)}
    normalization["local_latency"] = measure_local_latency(warehouse)
    return write_completion_report(
        warehouse,
        args.report,
        acquisition=acquisition,
        normalization=normalization,
        research=research,
    )


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
