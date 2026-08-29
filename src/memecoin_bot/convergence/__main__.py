from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from memecoin_bot.config import load_dotenv
from memecoin_bot.historical.dune_pilot import DuneAcquisitionConfig, DunePilotRunner
from memecoin_bot.historical.store import HistoricalWarehouse

from .audits import RepositoryAuditor
from .runner import ConvergenceOrchestrator


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Gambit Jr persistent V1.5 convergence runner")
    value.add_argument(
        "--warehouse",
        default=os.getenv("HISTORICAL_WAREHOUSE_PATH", "data/historical/warehouse.db"),
    )
    value.add_argument(
        "--archive",
        default=os.getenv("HISTORICAL_ARCHIVE_PATH", "data/archive/historical"),
    )
    value.add_argument("--operational-db", default=os.getenv("DATABASE_PATH"))
    commands = value.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run or resume one complete convergence cycle")
    run.add_argument("--run-id")
    run.add_argument("--phase", action="append", dest="phases")
    run.add_argument("--no-live-probes", action="store_true")
    run.add_argument("--code-version", default=os.getenv("SOFTWARE_VERSION", "working-tree"))

    commands.add_parser("status", help="view the latest convergence cycle")
    providers = commands.add_parser("providers", help="view provider capability/admission state")
    providers.add_argument("--probe", action="store_true")
    providers.add_argument("--provider", action="append", dest="provider_names")
    commands.add_parser("historical", help="view explicit monthly historical progress")
    commands.add_parser(
        "historical-plan",
        help="print the controlled Dune month/query plan without executing queries",
    )
    pilot = commands.add_parser(
        "historical-pilot", help="run only the explicitly bounded Dune plan"
    )
    pilot.add_argument("--execute", action="store_true")
    pilot.add_argument("--force", action="store_true")
    commands.add_parser("champion", help="view the current champion and challenger state")
    commands.add_parser("metrics", help="view latest precision/recall evidence")
    audit = commands.add_parser("audits", help="run the full static, security, DB and query audit")
    audit.add_argument("--repository", default=".")
    commands.add_parser("report", help="generate the internal daily machine report")
    return value


async def _run(args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    warehouse = HistoricalWarehouse(args.warehouse, args.archive)
    try:
        runner = ConvergenceOrchestrator(
            warehouse,
            operational_database=args.operational_db,
            code_version=getattr(args, "code_version", "working-tree"),
        )
        if args.command == "run":
            return await runner.run(
                run_id=args.run_id,
                phases=set(args.phases) if args.phases else None,
                live_probes=not args.no_live_probes,
            )
        if args.command == "status":
            return runner.status()
        if args.command == "providers":
            if args.probe:
                selected = set(args.provider_names) if args.provider_names else None
                return await runner.providers.probe(selected)
            else:
                runner.providers.refresh()
            return runner.providers.status()
        if args.command == "historical":
            return runner.historical_status()
        if args.command in {"historical-plan", "historical-pilot"}:
            config = DuneAcquisitionConfig.from_environment()
            pilot_runner = DunePilotRunner(warehouse, os.getenv("DUNE_API_KEY"), config)
            if args.command == "historical-plan":
                return pilot_runner.plan()
            return await pilot_runner.run(execute=args.execute, force=args.force)
        if args.command == "champion":
            return runner.champion_status()
        if args.command == "metrics":
            return _metrics(warehouse)
        if args.command == "audits":
            return RepositoryAuditor(warehouse, Path(args.repository)).run(args.operational_db)
        if args.command == "report":
            return runner.daily_report()
        raise ValueError(f"unsupported command: {args.command}")
    finally:
        warehouse.close()


def _metrics(warehouse: HistoricalWarehouse) -> dict[str, Any]:
    latest = warehouse.conn.execute(
        "SELECT research_run_id,metrics_json,result_json,created_at FROM research_runs "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return {
        "champion": "CONTROL_V15",
        "latest": (
            {
                "research_run_id": latest["research_run_id"],
                "created_at": latest["created_at"],
                "metrics": json.loads(latest["metrics_json"]),
                "result": json.loads(latest["result_json"]),
            }
            if latest
            else None
        ),
        "public_route": False,
        "status": "AWAITING_REAL_EVIDENCE" if latest is None else "RESEARCH_ONLY",
    }


def main() -> None:
    load_dotenv()
    args = parser().parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
