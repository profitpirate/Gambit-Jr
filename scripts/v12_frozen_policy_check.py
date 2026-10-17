#!/usr/bin/env python3
"""Offline evidence check. This is not a trading or live-readiness entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from memecoin_bot.v12_prearmed_evidence import (
    APPROVAL_COMMIT, FROZEN_MODEL_SHA256, evaluate_research_acceptance, read_evidence,
)
from memecoin_bot.v12_prearmed_policy import FrozenPrearmedPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        model, state = read_evidence(args.model), read_evidence(args.state)
        acceptance = evaluate_research_acceptance(model, state)
        if not acceptance.accepted:
            raise ValueError(acceptance.reason)
        policy = FrozenPrearmedPolicy(model)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'REJECTED', 'reason': type(exc).__name__,
                          'live_enabled': False, 'production_ready': False}))
        return 1
    print(json.dumps({
        'status': 'FROZEN_POLICY_EVIDENCE_VERIFIED_ONLY',
        'research_acceptance': asdict(acceptance),
        'closed_trades': state['metrics']['closed_trades'],
        'approval_commit': APPROVAL_COMMIT,
        'model_sha256': FROZEN_MODEL_SHA256,
        'strategy_family': policy.family,
        'maximum_hold_ms': policy.hold_ns // 1_000_000,
        'maximum_positions': policy.maximum_concurrent_positions,
        'fraction_of_available_cash': policy.fraction_of_available_cash,
        'live_enabled': False,
        'production_ready': False,
        'live_runtime_integration_complete': False,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
