#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

HEX40 = re.compile(r"^[0-9a-f]{40}$", re.I)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def finite(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def find_commit(value: Any) -> str:
    for row in walk(value):
        for key in ("commit", "git_commit", "head_sha", "source_commit", "tested_commit"):
            candidate = str(row.get(key) or "").strip()
            if HEX40.match(candidate):
                return candidate.lower()
    return ""


def capture_epoch(batch: Mapping[str, Any]) -> int:
    values = []
    for row in walk(batch):
        for key in ("received_ns", "entry_ns", "captured_ns", "started_ns"):
            value = integer(row.get(key))
            if value > 10**17:
                values.append(value // 1_000_000_000)
        for key in ("generated_at_epoch", "started_at_epoch", "capture_started_epoch"):
            value = integer(row.get(key))
            if value > 1_000_000_000:
                values.append(value)
    return min(values) if values else 0


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def causal_ref(batch: Mapping[str, Any], explicit: str) -> tuple[str, str]:
    if explicit:
        return explicit, "explicit"
    commit = find_commit(batch)
    if commit:
        try:
            return git("rev-parse", f"{commit}^"), "parent_of_first_capture_commit"
        except Exception:
            pass
    epoch = capture_epoch(batch)
    if epoch:
        try:
            return git("rev-list", "-1", f"--before=@{epoch}", "--all"), "last_commit_before_capture"
        except Exception:
            pass
    raise RuntimeError("unable to derive a causal repository ref before the first capture")


def creator_key(row: Mapping[str, Any]) -> str:
    for key in (
        "creator", "creator_address", "creator_wallet", "wallet", "address",
        "deployer", "deployer_address", "pubkey",
    ):
        value = str(row.get(key) or "").strip()
        if 30 <= len(value) <= 50:
            return value
    return ""


def stats(row: Mapping[str, Any]) -> tuple[int, int, int]:
    wins = max(
        integer(row.get("wins")),
        integer(row.get("e4_observed_wins")),
        integer(row.get("gross_wins")),
        integer(row.get("successful_trades")),
    )
    losses = max(
        integer(row.get("losses")),
        integer(row.get("e4_observed_losses")),
        integer(row.get("gross_losses")),
        integer(row.get("failed_trades")),
    )
    trades = max(
        integer(row.get("trades")),
        integer(row.get("samples")),
        integer(row.get("observed_trades")),
        wins + losses,
    )
    if wins <= 0 and losses <= 0:
        rate = finite(row.get("win_rate") or row.get("gross_win_rate"), -1.0)
        if trades > 0 and 0 <= rate <= 1:
            wins = round(trades * rate)
            losses = max(0, trades - wins)
    return wins, losses, max(trades, wins + losses)


def load_json_at(ref: str, path: str) -> Any:
    try:
        return json.loads(git("show", f"{ref}:{path}"))
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the creator whitelist before the first test window")
    parser.add_argument("--first-batch", type=Path, required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch = json.loads(args.first_batch.read_text(encoding="utf-8"))
    ref, ref_method = causal_ref(batch, args.ref)
    names = git("ls-tree", "-r", "--name-only", ref).splitlines()
    candidates = [
        name for name in names
        if name.endswith(".json")
        and (
            name.startswith("models/e4/")
            or name.startswith("artifacts/e4")
            or "creator" in name.lower()
            or "expectancy" in name.lower()
        )
    ]
    merged: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "sources": [],
    })
    parsed_files = 0
    for path in candidates:
        payload = load_json_at(ref, path)
        if payload is None:
            continue
        parsed_files += 1
        for row in walk(payload):
            creator = creator_key(row)
            if not creator:
                continue
            wins, losses, trades = stats(row)
            if wins <= 0 and losses <= 0:
                continue
            current = merged[creator]
            # Multiple registry files often repeat the same aggregate rather
            # than representing independent samples, so take maxima—not sums.
            current["wins"] = max(current["wins"], wins)
            current["losses"] = max(current["losses"], losses)
            current["trades"] = max(current["trades"], trades, wins + losses)
            current["sources"].append(path)
    creators = {}
    for creator, row in merged.items():
        trades = max(row["trades"], row["wins"] + row["losses"])
        creators[creator] = {
            "wins": row["wins"],
            "losses": row["losses"],
            "trades": trades,
            "win_rate": row["wins"] / trades if trades else 0.0,
            "sources": sorted(set(row["sources"])),
        }
    payload = {
        "version": "e4-v12-causal-prior-registry-v1",
        "causal_ref": ref,
        "ref_method": ref_method,
        "first_capture_commit": find_commit(batch) or None,
        "first_capture_epoch": capture_epoch(batch) or None,
        "candidate_json_files": len(candidates),
        "parsed_json_files": parsed_files,
        "creator_count": len(creators),
        "creators": dict(sorted(creators.items())),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "causal_ref": ref,
        "ref_method": ref_method,
        "candidate_json_files": len(candidates),
        "parsed_json_files": parsed_files,
        "creator_count": len(creators),
        "creators_with_2_wins": sum(row["wins"] >= 2 for row in creators.values()),
        "creators_with_5_wins": sum(row["wins"] >= 5 for row in creators.values()),
    }, indent=2, sort_keys=True), flush=True)
    if not creators:
        raise SystemExit("no causal prior creator registry found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
