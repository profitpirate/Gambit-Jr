#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from .e4_v12_forward_accumulate import aggregate, role_model_failures
else:
    from e4_v12_forward_accumulate import aggregate, role_model_failures


def evaluate_evidence(evidence: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    summary = aggregate(evidence)
    return bool(summary["role_model_targets_met"]), summary


def load_and_evaluate(path: Path) -> tuple[bool, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("V12 evidence must be a JSON object")
    return evaluate_evidence(payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enforce Gambit E4 V12 exact role-model performance targets"
    )
    parser.add_argument(
        "--evidence",
        default="models/e4/e4-v12-forward-evidence.json",
        help="Path to accumulated frozen V12 evidence",
    )
    args = parser.parse_args()
    try:
        passed, summary = load_and_evaluate(Path(args.evidence))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2, sort_keys=True))
        return 2

    status = "PASS" if passed else (
        "COLLECTING" if not summary["sufficient_evidence"] else "FAIL"
    )
    report = {
        "status": status,
        "classification": summary["classification"],
        "failures": role_model_failures(summary),
        "closed_positions": summary["gambit_closed_positions"],
        "target_closed_positions": summary["role_model_target_closed_positions"],
        "wins": summary["gambit_wins"],
        "target_wins": summary["role_model_target_wins"],
        "win_rate": summary["gambit_net_win_rate"],
        "target_win_rate": summary["role_model_target_win_rate"],
        "profit_factor": summary["gambit_profit_factor"],
        "target_profit_factor": summary["role_model_target_profit_factor"],
        "net_pnl_sol": summary["gambit_net_pnl_sol"],
        "role_model_targets_met": passed,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
