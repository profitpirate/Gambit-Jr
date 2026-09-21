"""Audit E4 wallet identity across every primary artifact-producing commit."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = "E4EzXdwf7NNdqM2XGswWaWHfxgucVCo24PTCcrimTKBz"
WALLET_RE = re.compile(
    r"""(?ix)
    (?:E4[_A-Z0-9]*WALLET|WALLET[_A-Z0-9]*E4)
    \s*(?::[^=]+)?=\s*
    ["']([1-9A-HJ-NP-Za-km-z]{32,44})["']
    """
)


def primary(name: str) -> bool:
    import v12_e4_source_reconciliation as reconciliation

    return reconciliation.is_primary(name)


def grep_commit(sha: str) -> tuple[set[str], str | None]:
    try:
        proc = subprocess.run(
            ["git", "grep", "-h", "-E", "E4.*WALLET|WALLET.*E4", sha, "--", "*.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"{type(exc).__name__}:{exc}"
    if proc.returncode not in {0, 1}:
        return set(), f"git_grep_exit_{proc.returncode}:{proc.stderr[-300:]}"
    wallets: set[str] = set()
    for line in proc.stdout.splitlines():
        wallets.update(WALLET_RE.findall(line))
    # The canonical literal itself is also strong evidence when the assignment
    # was assembled through aliases that the regex cannot parse.
    if CANONICAL in proc.stdout:
        wallets.add(CANONICAL)
    return wallets, None


def build(inventory: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for row in (inventory.get("artifacts") or [])
        if not row.get("expired") and primary(str(row.get("name") or ""))
    ]
    heads = sorted(
        {
            str(row.get("workflow_run_head_sha") or "")
            for row in rows
            if str(row.get("workflow_run_head_sha") or "")
        }
    )
    results = []
    all_wallets: set[str] = set()
    unresolved = []
    for sha in heads:
        wallets, error = grep_commit(sha)
        all_wallets.update(wallets)
        row = {"sha": sha, "wallets": sorted(wallets), "error": error}
        if error or not wallets:
            unresolved.append(row)
        results.append(row)

    unexpected = sorted(wallet for wallet in all_wallets if wallet != CANONICAL)
    complete = bool(heads) and not unresolved and not unexpected
    return {
        "schema_version": "e4-wallet-identity-inventory-v1",
        "canonical_wallet": CANONICAL,
        "primary_artifact_heads": len(heads),
        "heads_resolved": len(heads) - len(unresolved),
        "heads_unresolved": len(unresolved),
        "wallets_observed": sorted(all_wallets),
        "unexpected_wallets": unexpected,
        "certified_complete": complete,
        "definition": (
            "Every retained primary E4 execution artifact head must resolve an "
            "E4 wallet assignment, and every resolved assignment must equal the "
            "canonical E4 wallet."
        ),
        "unresolved": unresolved,
        "heads": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path("research/e4-lifetime-artifact-inventory.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/e4-wallet-identity-inventory.json"),
    )
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    result = build(inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        key: value
        for key, value in result.items()
        if key not in {"heads", "unresolved"}
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
