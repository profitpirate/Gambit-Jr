#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from memecoin_bot import e4_live as core
from memecoin_bot.e4_selection_v2 import UnifiedE4Policy


def write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def state(
    mint: str,
    creator: str,
    *,
    fdv: float = 4_800.0,
    buys: list[tuple[str, float]] | None = None,
    sells: list[tuple[str, float]] | None = None,
) -> core.TokenState:
    start = 1_800_000_000_000_000_000
    result = core.TokenState(mint)
    result.apply(
        core.Event(
            event_id=1,
            kind=core.EventKind.CREATE,
            mint=mint,
            source_ns=start,
            received_ns=start,
            fdv_usd=fdv,
            creator=creator,
        ),
        "audit-wallet",
    )
    event_id = 10
    for wallet, amount in buys or []:
        event_id += 1
        result.apply(
            core.Event(
                event_id=event_id,
                kind=core.EventKind.BUY,
                mint=mint,
                source_ns=start + event_id * 10_000_000,
                received_ns=start + event_id * 10_000_000,
                trader=wallet,
                sol_amount=amount,
                fdv_usd=fdv,
                creator=creator,
            ),
            "audit-wallet",
        )
    for wallet, amount in sells or []:
        event_id += 1
        result.apply(
            core.Event(
                event_id=event_id,
                kind=core.EventKind.SELL,
                mint=mint,
                source_ns=start + event_id * 10_000_000,
                received_ns=start + event_id * 10_000_000,
                trader=wallet,
                sol_amount=amount,
                fdv_usd=fdv,
                creator=creator,
            ),
            "audit-wallet",
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stress-report", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="e4-selection-audit-") as tmp:
        os.environ["E4_SELECTION_MEMORY_PATH"] = str(Path(tmp) / "memory.json")
        os.environ["E4_SELECTION_MEMORY_PERSIST"] = "0"
        os.environ["E4_SELECTION_V2_ENABLED"] = "true"
        settings = core.Settings(
            live=False,
            wallet="audit-wallet",
            model_path=Path(tmp) / "missing-model.json",
            max_position_fraction=0.20,
        )
        policy = UnifiedE4Policy(settings)
        snapshot = policy.audit_snapshot()

        # A deliberately fake 3/3 creator must never be an entry authorization.
        policy.library.expectancy["thin-3-of-3"] = {
            "creator": "thin-3-of-3",
            "wins": 3,
            "losses": 0,
            "trades": 3,
        }
        thin_no_flow = policy.decision(state("thin-no-flow", "thin-3-of-3"))
        thin_assessment = policy.library.assess("thin-3-of-3")

        # A robust negative creator must be vetoed even with excellent live flow.
        policy.library.expectancy["bad-creator"] = {
            "creator": "bad-creator",
            "wins": 0,
            "losses": 12,
            "trades": 12,
        }
        negative = policy.decision(
            state(
                "negative",
                "bad-creator",
                buys=[(f"b-{i}", 0.80) for i in range(7)],
            )
        )

        # A high-quality live flow may pass for an unknown creator; this proves
        # the library is advisory rather than mandatory.
        unknown = policy.decision(
            state(
                "unknown-good-flow",
                "unknown-creator",
                buys=[(f"u-{i}", 0.85) for i in range(8)],
            )
        )

        # Causal learning must only become defensive after resolved outcomes.
        baseline_regime = policy.memory.regime()
        for index in range(12):
            mint = f"loss-{index}"
            policy.memory.register_entry(
                mint,
                creator="runtime-creator",
                buyers=[f"rb-{index % 3}"],
                score=0.70,
                decision_ns=index,
            )
            assert policy.memory.resolve(mint, -0.10)
        defensive_regime = policy.memory.regime()

        # Buyer memory requires evidence; a single outcome cannot create a signal.
        one_wallet = "one-shot-wallet"
        policy.memory.register_entry(
            "one-shot",
            creator="creator-x",
            buyers=[one_wallet],
            score=0.7,
            decision_ns=100,
        )
        policy.memory.resolve("one-shot", 0.5)
        one_shot = policy.memory.buyer_assessment([one_wallet])

        # More resolved history can become useful, and duplicate outcomes are rejected.
        learned_wallet = "learned-wallet"
        duplicate_rejected = True
        for index in range(8):
            mint = f"buyer-learn-{index}"
            policy.memory.register_entry(
                mint,
                creator="creator-y",
                buyers=[learned_wallet],
                score=0.7,
                decision_ns=200 + index,
            )
            first = policy.memory.resolve(mint, 0.20 if index < 7 else -0.10)
            second = policy.memory.resolve(mint, 0.20)
            duplicate_rejected = duplicate_rejected and first and not second
        learned = policy.memory.buyer_assessment([learned_wallet])

        stress: dict[str, Any] | None = None
        if args.stress_report and args.stress_report.exists():
            stress = json.loads(args.stress_report.read_text())

        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root / "src")
        boot_code = (
            "import json;"
            "from memecoin_bot import e4_live as core;"
            "import memecoin_bot.e4_exec;"
            "from memecoin_bot.e4_selection_v2 import UnifiedE4Policy;"
            "print(json.dumps({"
            "'policy':core.E4Policy is UnifiedE4Policy,"
            "'learning':bool(getattr(core.Engine.execute_sell,"
            "'_e4_selection_v2_learning_wrapper',False))"
            "}))"
        )
        boot = subprocess.run(
            [sys.executable, "-c", boot_code],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        boot_payload: dict[str, Any] = {}
        if boot.returncode == 0:
            try:
                boot_payload = json.loads(boot.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                boot_payload = {}

        checks = {
            "library_cannot_authorize_without_live_flow": not thin_no_flow.accepted,
            "three_of_three_creator_is_confidence_capped": thin_assessment.score_delta <= 0.03 + 1e-12,
            "robust_negative_creator_vetoes": (
                not negative.accepted and "negative creator veto" in negative.reason
            ),
            "unknown_creator_can_pass_on_real_live_signal": unknown.accepted,
            "regime_starts_neutral": baseline_regime.threshold_add == 0.0,
            "resolved_losses_make_system_more_conservative": (
                defensive_regime.threshold_add >= 0.04
                and defensive_regime.size_multiplier <= 0.65
            ),
            "single_buyer_outcome_is_not_reputation": one_shot.qualified_wallets == 0,
            "resolved_buyer_history_can_influence": (
                learned.qualified_wallets == 1
                and learned.posterior_mean > 0.5
                and learned.score_delta > 0
            ),
            "duplicate_outcomes_cannot_inflate_memory": duplicate_rejected,
            "sizing_is_hard_capped": policy.config.maximum_position_fraction <= 0.10,
            "stress_report_passed": stress is None or bool(stress.get("passed")),
            "authoritative_cli_uses_unified_policy": bool(boot_payload.get("policy")),
            "final_sell_path_retains_causal_learning": bool(boot_payload.get("learning")),
        }

        expectancy = json.loads(
            Path("models/e4/e4-creator-expectancy.json").read_text()
        )
        historical_rows = [
            row
            for row in expectancy.get("top_creators") or []
            if isinstance(row, Mapping)
        ]
        trade_counts = [
            int(row.get("trades") or int(row.get("wins") or 0) + int(row.get("losses") or 0))
            for row in historical_rows
        ]
        report = {
            "version": "e4-selection-v2-audit-v1",
            "passed": all(checks.values()),
            "checks": checks,
            "system": snapshot,
            "historical_library": {
                "source": "models/e4/e4-creator-expectancy.json",
                "repeated_creator_rows": len(historical_rows),
                "rows_with_at_least_5_trades": sum(x >= 5 for x in trade_counts),
                "rows_with_at_least_10_trades": sum(x >= 10 for x in trade_counts),
                "median_trades": (
                    sorted(trade_counts)[len(trade_counts) // 2] if trade_counts else 0
                ),
                "note": (
                    "Creator history is contextual evidence only; weak one-off winner "
                    "registries and externally discovered creators cannot independently buy."
                ),
            },
            "authoritative_boot": {
                "returncode": boot.returncode,
                "payload": boot_payload,
                "stderr_tail": boot.stderr[-1000:],
            },
            "stress": stress,
        }
        encoded = json.dumps(report, indent=2, sort_keys=True)
        print(encoded)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n")
        if args.strict and not report["passed"]:
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
