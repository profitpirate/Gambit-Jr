#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from memecoin_bot.e4_prearmed_readiness import readiness


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V12 Pre-Armed production readiness without placing trades.")
    parser.add_argument("--include-external", action="store_true", help="Also report external key/RPC/route configuration.")
    parser.add_argument("--no-fail", action="store_true", help="Always exit zero; useful while the 100-trade confirmation is still collecting.")
    args = parser.parse_args()

    report = readiness(include_external=args.include_external)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.no_fail:
        return 0
    return 0 if report["ready_without_external_credentials"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
