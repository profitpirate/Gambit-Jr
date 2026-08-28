from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.realtime_research import (
    run_realtime_trajectory_research,
    write_realtime_research,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit the research-only exact-band realtime transaction challenger"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_realtime_trajectory_research(args.database)
    write_realtime_research(result, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "truth_state": result["truth_state"],
                "one_percent": result["one_percent"],
                "approved_features": result["approved_features"],
                "production_ready": result["production_ready"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
