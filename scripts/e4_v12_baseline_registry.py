#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
CREATOR_KEYS = (
    "creator",
    "creator_address",
    "creator_wallet",
    "deployer",
    "deployer_address",
    "dev",
    "dev_wallet",
    "wallet",
    "address",
)
WIN_KEYS = ("wins", "e4_observed_wins", "winning_trades", "profitable_trades")
LOSS_KEYS = ("losses", "e4_observed_losses", "losing_trades", "unprofitable_trades")
TRADE_KEYS = ("trades", "total_trades", "observations", "e4_observed_trades")
RATE_KEYS = ("win_rate", "gross_win_rate", "rate", "e4_win_rate")


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def is_address(value: str) -> bool:
    return bool(BASE58_RE.match(str(value or "")))


def extract_rows(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, Mapping):
        explicit = str(first(value, CREATOR_KEYS) or "")
        wins_raw = first(value, WIN_KEYS)
        losses_raw = first(value, LOSS_KEYS)
        trades_raw = first(value, TRADE_KEYS)
        rate_raw = first(value, RATE_KEYS)
        has_stats = any(item is not None for item in (wins_raw, losses_raw, trades_raw, rate_raw))
        if explicit and is_address(explicit) and has_stats:
            wins = max(0, integer(wins_raw))
            losses = max(0, integer(losses_raw))
            trades = max(integer(trades_raw), wins + losses)
            rate = finite(rate_raw, wins / trades if trades else 0.0)
            yield {
                "creator": explicit,
                "wins": wins,
                "losses": losses,
                "trades": trades,
                "win_rate": max(0.0, min(1.0, rate)),
            }
        for key, item in value.items():
            if is_address(str(key)) and isinstance(item, Mapping):
                wins_raw = first(item, WIN_KEYS)
                losses_raw = first(item, LOSS_KEYS)
                trades_raw = first(item, TRADE_KEYS)
                rate_raw = first(item, RATE_KEYS)
                if any(row is not None for row in (wins_raw, losses_raw, trades_raw, rate_raw)):
                    wins = max(0, integer(wins_raw))
                    losses = max(0, integer(losses_raw))
                    trades = max(integer(trades_raw), wins + losses)
                    rate = finite(rate_raw, wins / trades if trades else 0.0)
                    yield {
                        "creator": str(key),
                        "wins": wins,
                        "losses": losses,
                        "trades": trades,
                        "win_rate": max(0.0, min(1.0, rate)),
                    }
            if isinstance(item, (Mapping, list, tuple)):
                yield from extract_rows(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from extract_rows(item)


def git_output(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def candidate_paths(repo: Path, ref: str) -> list[str]:
    names = git_output(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
    output = []
    for name in names:
        lower = name.lower()
        if not lower.endswith(".json"):
            continue
        if not (lower.startswith("models/e4/") or lower.startswith("docs/research/")):
            continue
        if any(term in lower for term in ("creator", "deployer", "registry", "expectancy", "identity")):
            output.append(name)
    return output


def commit_before(repo: Path, ref: str, path: str, cutoff_epoch: float) -> str:
    cutoff = dt.datetime.fromtimestamp(cutoff_epoch, tz=dt.timezone.utc).isoformat()
    try:
        return git_output(repo, "rev-list", "-1", f"--before={cutoff}", ref, "--", path)
    except Exception:
        return ""


def file_at(repo: Path, commit: str, path: str) -> Any:
    try:
        return json.loads(git_output(repo, "show", f"{commit}:{path}"))
    except Exception:
        return None


def build_registry(repo: Path, ref: str, cutoff_epoch: float) -> dict[str, Any]:
    creators: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for path in candidate_paths(repo, ref):
        commit = commit_before(repo, ref, path, cutoff_epoch)
        if not commit:
            continue
        payload = file_at(repo, commit, path)
        if payload is None:
            continue
        count = 0
        for row in extract_rows(payload):
            creator = str(row["creator"])
            previous = creators.get(creator)
            if previous is None:
                creators[creator] = dict(row)
            else:
                # Multiple files often contain the same historical observation.
                # Taking the maximum known counts avoids double-counting while
                # preserving the strongest information available before cutoff.
                wins = max(integer(previous.get("wins")), integer(row.get("wins")))
                losses = max(integer(previous.get("losses")), integer(row.get("losses")))
                trades = max(integer(previous.get("trades")), integer(row.get("trades")), wins + losses)
                creators[creator] = {
                    "creator": creator,
                    "wins": wins,
                    "losses": losses,
                    "trades": trades,
                    "win_rate": wins / trades if trades else 0.0,
                }
            count += 1
        if count:
            sources.append({"path": path, "commit": commit, "rows": count})
    return {
        "version": "e4-v12-causal-baseline-registry-v1",
        "cutoff_epoch": cutoff_epoch,
        "ref": ref,
        "sources": sources,
        "creator_count": len(creators),
        "creators": dict(sorted(creators.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a creator registry using only files committed before a causal cutoff")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--cutoff-epoch", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = build_registry(args.repo, args.ref, args.cutoff_epoch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"creator_count": registry["creator_count"], "sources": len(registry["sources"]), "cutoff_epoch": registry["cutoff_epoch"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
