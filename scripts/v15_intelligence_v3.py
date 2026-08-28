from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.intelligence_v3_execution import (
    run_available_data_experiment,
    run_red_pump_social_incremental,
    run_wallet_copyability_study,
    write_results,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit and evaluate the research-only Intelligence V3 available-data candidate"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--shadow-output",
        type=Path,
        help="External research-only SQLite replay; never routes to the live outbox",
    )
    parser.add_argument("--red-pump", type=Path)
    parser.add_argument("--wallet-study", action="store_true")
    args = parser.parse_args()
    result = run_available_data_experiment(
        args.database,
        args.corpus,
        shadow_output=args.shadow_output,
    )
    if args.red_pump:
        result["social_infrastructure"] = run_red_pump_social_incremental(args.red_pump)
    if args.wallet_study:
        result["wallet_copyability"] = run_wallet_copyability_study(
            args.database, args.corpus
        )
    write_results(result, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "version": result["version"],
                "truth_state": result["truth_state"],
                "rows": result["rows"],
                "sealed_validation": result["sealed_validation"],
                "production_ready": result["production_ready"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
