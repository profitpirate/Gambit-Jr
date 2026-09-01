#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        if new in text:
            return
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:120]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    hardening = Path("src/memecoin_bot/e4_hardening_v10.py")
    registry = Path("src/memecoin_bot/e4_pipelines_v10.py")
    harness = Path("scripts/e4_live_market_stress.py")

    replace_once(
        hardening,
        '    creator_seed = float(features.get("creator_buy_sol", 0.0) or 0.0)\n\n    # A cooperating/prearmed launch is an explicit authority path and must be\n',
        '    creator_seed = float(features.get("creator_buy_sol", 0.0) or 0.0)\n'
        '    current_fdv = float(getattr(state, "fdv_usd", 0.0) or 0.0)\n'
        '    features["entry_fdv_usd"] = current_fdv\n'
        '    if current_fdv > 0 and current_fdv > self.settings.max_entry_fdv_usd:\n'
        '        return False, 0.0, 0.0, "entry FDV above observed E4 envelope", features\n\n'
        '    # A cooperating/prearmed launch is an explicit authority path and must be\n',
    )
    replace_once(
        hardening,
        '            elif wins >= 1 and rate >= 0.50:\n',
        '            elif trades >= 3 and wins >= 2 and rate >= 0.60:\n',
    )
    replace_once(
        registry,
        '    if wins >= 1 and rate >= 0.50:\n        return CreatorTier.APPROVED\n',
        '    if trades >= 3 and wins >= 2 and rate >= 0.60:\n        return CreatorTier.APPROVED\n',
    )

    replace_once(
        harness,
        'import random\nimport statistics\nimport subprocess\n',
        'import random\nimport shlex\nimport statistics\nimport subprocess\n',
    )
    replace_once(
        harness,
        '    entry_decision_ns = 0\n    for index, event in enumerate(events):\n',
        '    entry_decision_ns = 0\n    decision_price_sol = 0.0\n    decision_fdv_usd = 0.0\n    for index, event in enumerate(events):\n',
    )
    replace_once(
        harness,
        '            requested_fraction = fraction\n            entry_decision_ns = event.received_ns\n            break\n',
        '            requested_fraction = fraction\n            entry_decision_ns = event.received_ns\n            decision_price_sol = float(state.price_sol or event.price_sol or 0.0)\n            decision_fdv_usd = float(state.fdv_usd or event.fdv_usd or 0.0)\n            break\n',
    )
    replace_once(
        harness,
        '    entry_price = fill_event.price_sol\n    if not entry_price or entry_price <= 0:\n        return None\n',
        '    entry_price = fill_event.price_sol\n    if not entry_price or entry_price <= 0:\n        return None\n'
        '    # A simulated fill must respect the exact buy protection the real transaction carries.\n'
        '    # If the next observable trade is already outside the allowed price/FDV envelope,\n'
        '    # the correct counterfactual is a failed/missed entry, not an impossible bad fill.\n'
        '    max_price = decision_price_sol * (1.0 + settings.buy_slippage_bps / 10_000.0) if decision_price_sol > 0 else 0.0\n'
        '    if max_price > 0 and entry_price > max_price:\n'
        '        return None\n'
        '    fill_fdv = float(fill_event.fdv_usd or 0.0)\n'
        '    if fill_fdv > 0 and fill_fdv > settings.max_entry_fdv_usd:\n'
        '        return None\n'
        '    if decision_fdv_usd > 0 and decision_fdv_usd > settings.max_entry_fdv_usd:\n'
        '        return None\n',
    )
    replace_once(
        harness,
        '    keypair = Keypair()\n    process = await asyncio.create_subprocess_exec(\n        "node",\n        "tools/e4-builder/daemon.mjs",\n',
        '    keypair = Keypair()\n'
        '    builder_command = tuple(shlex.split(os.getenv("E4_BUILDER_COMMAND", "node tools/e4-builder/race-proxy-v3.mjs")))\n'
        '    if not builder_command:\n'
        '        return {"available": False, "reason": "empty V11 builder command"}\n'
        '    process = await asyncio.create_subprocess_exec(\n'
        '        *builder_command,\n',
    )
    replace_once(
        harness,
        '        latencies = [250.0, 500.0, 1_000.0]\n',
        '        latencies = [36.0, 100.0, 250.0, 500.0, 1_000.0]\n',
    )
    replace_once(
        harness,
        '    primary_scenario = scenarios["500ms"]["balances"]["1.2"]\n',
        '    primary_scenario = scenarios["36ms"]["balances"]["1.2"]\n',
    )
    replace_once(
        harness,
        '            "latency_ms": 500,\n',
        '            "latency_ms": 36,\n',
    )
    replace_once(
        harness,
        '                f"**Hypothetical closed positions (500ms / 1.2 SOL):** {primary_scenario[\'closed_positions\']}",\n',
        '                f"**Hypothetical closed positions (36ms / 1.2 SOL):** {primary_scenario[\'closed_positions\']}",\n',
    )

    print("V11 market repairs applied")


if __name__ == "__main__":
    main()
