#!/usr/bin/env python3
"""Small deterministic developer-library policy used by FULL_3S_V2_LIBRARY.

This module is intentionally boring. It never talks to a wallet, signer or order API.
It only answers whether a creator has enough *prior resolved* evidence to qualify and
provides append-only helpers for forward paper evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

MODEL = "FULL_3S_V2_LIBRARY"
SOURCE = "HG"
SCHEMA = "gambit-library-runtime-v1"


@dataclass(frozen=True)
class RuleConfig:
    min_resolved: int = 3
    min_wins: int = 2
    min_win_rate: float = 2 / 3
    min_mean_return: float = 0.0
    position_fraction: float = 0.05
    horizon_ms: int = 3000
    observation_stake_sol: float = 0.10

    def __post_init__(self) -> None:
        if self.min_resolved < 1 or self.min_wins < 1 or self.min_wins > self.min_resolved:
            raise ValueError("invalid history thresholds")
        if not (0 < self.min_win_rate <= 1):
            raise ValueError("invalid win-rate threshold")
        if not (0 < self.position_fraction <= 0.25):
            raise ValueError("invalid position fraction")
        if self.horizon_ms != 3000:
            raise ValueError("library competitor is intentionally fixed to 3 seconds")
        if self.observation_stake_sol <= 0:
            raise ValueError("invalid observation stake")


class AppendStore:
    """Durable append-only JSONL with deterministic event ids supplied by caller."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("event_id"):
                        self.seen.add(str(row["event_id"]))

    def append(self, row: Mapping[str, Any]) -> bool:
        event_id = str(row.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id required")
        if event_id in self.seen:
            return False
        body = dict(row)
        body.setdefault("schema", SCHEMA)
        body.setdefault("source", SOURCE)
        body.setdefault("model", MODEL)
        encoded = json.dumps(body, sort_keys=True, allow_nan=False)
        with self.path.open("a", encoding="utf-8", buffering=1) as f:
            f.write(encoded + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.seen.add(event_id)
        return True


class DeveloperLibrary:
    """Combines frozen prior developer rows with causal forward outcomes.

    Unknown/malformed historical fields are ignored rather than inferred. Forward
    outcomes are one mint once; duplicates cannot inflate a developer history.
    """

    def __init__(self, master: Mapping[str, Any] | None = None, config: RuleConfig | None = None):
        self.c = config or RuleConfig()
        self.base: dict[str, dict[str, float]] = {}
        self.forward: dict[str, dict[str, Any]] = {}
        self.resolved_mints: set[str] = set()
        if master:
            self._load_master(master)

    @staticmethod
    def _number(row: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
        for name in names:
            value = row.get(name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return float(value)
        return None

    def _load_master(self, master: Mapping[str, Any]) -> None:
        raw = master.get("devs", {})
        if isinstance(raw, list):
            rows = [(str(r.get("developer") or r.get("creator") or r.get("dev") or ""), r)
                    for r in raw if isinstance(r, Mapping)]
        elif isinstance(raw, Mapping):
            rows = [(str(k), v) for k, v in raw.items() if isinstance(v, Mapping)]
        else:
            rows = []
        for creator, row in rows:
            if not creator:
                continue
            wins = self._number(row, ("unique_coin_wins", "wins", "known_wins"))
            losses = self._number(row, ("unique_coin_losses", "losses", "known_losses"))
            resolved = self._number(row, ("unique_resolved_coins", "resolved", "known_basis_cycles"))
            net_return = self._number(row, ("return_sum", "sum_return", "record_return_sum", "net_return"))
            if wins is None or losses is None:
                continue
            if resolved is None:
                resolved = wins + losses
            if resolved < wins + losses:
                resolved = wins + losses
            # No invented profitability: absent historical return remains zero and
            # therefore cannot independently make a developer qualify.
            self.base[creator] = {
                "resolved": float(max(0, resolved)),
                "wins": float(max(0, wins)),
                "losses": float(max(0, losses)),
                "return_sum": float(net_return or 0.0),
            }

    def stats(self, creator: str) -> dict[str, Any]:
        base = self.base.get(creator, {})
        fwd = self.forward.get(creator, {})
        resolved = int(base.get("resolved", 0) + fwd.get("resolved", 0))
        wins = int(base.get("wins", 0) + fwd.get("wins", 0))
        losses = int(base.get("losses", 0) + fwd.get("losses", 0))
        return_sum = float(base.get("return_sum", 0.0) + fwd.get("return_sum", 0.0))
        win_rate = wins / resolved if resolved else None
        mean_return = return_sum / resolved if resolved else None
        return {
            "creator": creator,
            "resolved": resolved,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "return_sum": return_sum,
            "mean_return": mean_return,
            "base_resolved": int(base.get("resolved", 0)),
            "forward_resolved": int(fwd.get("resolved", 0)),
        }

    def decide(self, creator: str) -> dict[str, Any]:
        s = self.stats(creator)
        reasons: list[str] = []
        if not creator:
            reasons.append("UNKNOWN_CREATOR")
        if s["resolved"] < self.c.min_resolved:
            reasons.append("INSUFFICIENT_RESOLVED_HISTORY")
        if s["wins"] < self.c.min_wins:
            reasons.append("INSUFFICIENT_VERIFIED_WINS")
        if s["win_rate"] is None or s["win_rate"] < self.c.min_win_rate:
            reasons.append("WIN_RATE_BELOW_THRESHOLD")
        if s["mean_return"] is None or s["mean_return"] <= self.c.min_mean_return:
            reasons.append("NONPOSITIVE_MEAN_RETURN")
        return {
            "model": MODEL,
            "qualifies": not reasons,
            "reason": "QUALIFIED_DEV_LIBRARY" if not reasons else reasons[0],
            "reasons": reasons,
            "developer_stats_before_decision": s,
            "rule": asdict(self.c),
        }

    def update(self, *, mint: str, creator: str, return_fraction: float) -> bool:
        if not mint or not creator or mint in self.resolved_mints:
            return False
        if not math.isfinite(return_fraction):
            return False
        row = self.forward.setdefault(creator, {"resolved": 0, "wins": 0, "losses": 0, "return_sum": 0.0})
        row["resolved"] += 1
        row["wins"] += int(return_fraction > 0)
        row["losses"] += int(return_fraction < 0)
        row["return_sum"] += float(return_fraction)
        self.resolved_mints.add(mint)
        return True

    def snapshot(self) -> dict[str, Any]:
        creators = sorted(set(self.base) | set(self.forward))
        return {
            "schema": SCHEMA,
            "model": MODEL,
            "rule": asdict(self.c),
            "developers": {creator: self.stats(creator) for creator in creators},
            "resolved_mints": sorted(self.resolved_mints),
        }
