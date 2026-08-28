from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.intelligence_v2_research import IntelligenceV2Experiment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the research-only V1.5 Intelligence V2 experiment"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    experiment = IntelligenceV2Experiment(args.database)
    try:
        result = experiment.write(args.output)
    finally:
        experiment.close()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "version": result["version"],
                "state": result["state"],
                "approved_features": result["approved_features"],
                "production_ready": result["production_ready"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
