from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.runner_autopsy import RunnerAutopsy


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the diagnostic-only V1.5 runner autopsy")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--reuse-replay", action="store_true")
    args = parser.parse_args()
    autopsy = RunnerAutopsy(args.database, args.corpus)
    try:
        if not args.reuse_replay:
            autopsy.build_replay_table()
        result = autopsy.write_report(args.output, args.markdown)
    finally:
        autopsy.close()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_rows": result["source_rows"],
                "valid_analysis_rows": result["valid_analysis_rows"],
                "truth_state": result["truth_state"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
