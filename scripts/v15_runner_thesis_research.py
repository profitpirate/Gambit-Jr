from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.thesis_research import (
    run_runner_thesis_research,
    write_runner_thesis_research,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate research-only runner thesis archetypes")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_runner_thesis_research(args.database)
    write_runner_thesis_research(result, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "outer": result["outer"],
                "decision": result["decision"],
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
