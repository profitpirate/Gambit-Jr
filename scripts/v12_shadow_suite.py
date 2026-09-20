#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from memecoin_bot.v12_shadow import build_shadow_report


def load(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="V12 Pre-Armed shadow analytics suite")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--counterfactuals", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--risk-paths", type=int, default=20_000)
    args = parser.parse_args()

    state = load(args.state)
    if not state:
        raise SystemExit("state file is missing or empty")
    baseline = load(args.baseline)
    counterfactual_payload = load(args.counterfactuals) or {}
    rows = counterfactual_payload.get("rows", []) if isinstance(counterfactual_payload, dict) else []
    report = build_shadow_report(
        state,
        baseline_state=baseline,
        counterfactual_rows=rows,
        risk_paths=args.risk_paths,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "shadow_only": report["shadow_only"],
        "ledger_rows": report["integrity"]["ledger_rows"],
        "drift": report["drift"]["severity"],
        "risk_of_ruin": report["risk_of_ruin"].get("risk_of_ruin"),
        "creator_count": report["concentration"]["creator_count"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
