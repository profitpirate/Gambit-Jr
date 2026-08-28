from __future__ import annotations

import argparse
import json
from pathlib import Path

from memecoin_bot.historical.realtime_replay import (
    build_realtime_event_replay,
    write_replay_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the point-in-time V1.5 transaction-event replay and coverage manifest"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_realtime_event_replay(args.database, args.corpus)
    write_replay_manifest(result, args.output)
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
