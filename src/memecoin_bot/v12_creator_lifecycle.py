"""Time-decayed creator lifecycle for V12 production."""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS v12_creator_live_evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator TEXT NOT NULL,
    won INTEGER NOT NULL,
    pnl_sol REAL NOT NULL,
    source TEXT NOT NULL,
    observed_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v12_creator_evidence
ON v12_creator_live_evidence(creator,observed_ns);
CREATE TABLE IF NOT EXISTS v12_creator_lifecycle(
    creator TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    score REAL NOT NULL,
    effective_samples REAL NOT NULL,
    recent_win_rate REAL NOT NULL,
    decayed_pnl_sol REAL NOT NULL,
    consecutive_losses INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL,
    reason TEXT NOT NULL
);
"""


class CreatorState(StrEnum):
    ELITE = "ELITE"
    PROMOTED = "PROMOTED"
    PROBATION = "PROBATION"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True, slots=True)
class CreatorLifecycle:
    creator: str
    state: CreatorState
    score: float
    effective_samples: float
    recent_win_rate: float
    decayed_pnl_sol: float
    consecutive_losses: int
    reason: str

    @property
    def entry_allowed(self) -> bool:
        return self.state != CreatorState.QUARANTINE

    @property
    def risk_multiplier(self) -> float:
        return {
            CreatorState.ELITE: 1.0,
            CreatorState.PROMOTED: 1.0,
            CreatorState.PROBATION: 0.50,
            CreatorState.QUARANTINE: 0.0,
        }[self.state]


class CreatorLifecycleStore:
    def __init__(
        self,
        database: Path | sqlite3.Connection,
        *,
        half_life_days: float = 14.0,
        retention_days: float = 120.0,
    ):
        if isinstance(database, sqlite3.Connection):
            self.conn = database
            self.owns_connection = False
        else:
            database.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(database, timeout=5, isolation_level=None, check_same_thread=False)
            self.owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.half_life_ns = max(1.0, half_life_days) * 86_400 * 1e9
        self.retention_ns = max(half_life_days * 2, retention_days) * 86_400 * 1e9

    def close(self) -> None:
        if self.owns_connection:
            self.conn.close()

    def observe_trade(
        self,
        creator: str,
        *,
        won: bool,
        pnl_sol: float,
        observed_ns: int | None = None,
        source: str = "LIVE_TRADE",
    ) -> CreatorLifecycle:
        creator = str(creator or "")
        if not creator:
            raise ValueError("creator is required")
        observed = int(observed_ns or time.time_ns())
        self.conn.execute(
            """
            INSERT INTO v12_creator_live_evidence(
                creator,won,pnl_sol,source,observed_ns
            ) VALUES(?,?,?,?,?)
            """,
            (creator, int(won), float(pnl_sol), str(source), observed),
        )
        self.prune(observed)
        return self.recompute(creator, now_ns=observed)

    def seed_canonical(
        self,
        creator: str,
        *,
        tier: str,
        wins: int,
        losses: int,
        pnl_sol: float = 0.0,
    ) -> CreatorLifecycle:
        existing = self.conn.execute(
            "SELECT 1 FROM v12_creator_live_evidence WHERE creator=? LIMIT 1",
            (creator,),
        ).fetchone()
        if existing is None:
            now = time.time_ns()
            # Historical evidence is deliberately down-weighted by spacing it
            # back one half-life; fresh live evidence can override it quickly.
            base = now - int(self.half_life_ns)
            for index in range(max(0, int(wins))):
                self.conn.execute(
                    "INSERT INTO v12_creator_live_evidence(creator,won,pnl_sol,source,observed_ns) VALUES(?,?,?,?,?)",
                    (creator, 1, max(0.0, pnl_sol) / max(wins, 1), f"CANONICAL_{tier}", base + index),
                )
            for index in range(max(0, int(losses))):
                self.conn.execute(
                    "INSERT INTO v12_creator_live_evidence(creator,won,pnl_sol,source,observed_ns) VALUES(?,?,?,?,?)",
                    (creator, 0, 0.0, f"CANONICAL_{tier}", base + wins + index),
                )
        return self.recompute(creator)

    def recompute(self, creator: str, *, now_ns: int | None = None) -> CreatorLifecycle:
        now = int(now_ns or time.time_ns())
        rows = self.conn.execute(
            """
            SELECT won,pnl_sol,observed_ns FROM v12_creator_live_evidence
            WHERE creator=? ORDER BY observed_ns
            """,
            (creator,),
        ).fetchall()
        if not rows:
            lifecycle = CreatorLifecycle(
                creator,
                CreatorState.PROBATION,
                0.5,
                0.0,
                0.0,
                0.0,
                0,
                "no_evidence",
            )
            self._persist(lifecycle, now)
            return lifecycle

        weighted_wins = 0.0
        weighted_total = 0.0
        weighted_pnl = 0.0
        consecutive_losses = 0
        for row in rows:
            age = max(0, now - int(row["observed_ns"]))
            weight = math.exp(-math.log(2.0) * age / self.half_life_ns)
            weighted_total += weight
            weighted_wins += weight * int(row["won"])
            weighted_pnl += weight * float(row["pnl_sol"])
        for row in reversed(rows):
            if int(row["won"]):
                break
            consecutive_losses += 1

        win_rate = weighted_wins / weighted_total if weighted_total > 0 else 0.0
        pnl_score = 0.5 + 0.5 * math.tanh(weighted_pnl / max(0.05, weighted_total * 0.05))
        sample_score = min(1.0, weighted_total / 6.0)
        score = 0.60 * win_rate + 0.25 * pnl_score + 0.15 * sample_score

        if consecutive_losses >= 4 or (weighted_total >= 3 and score < 0.30):
            state = CreatorState.QUARANTINE
            reason = "fresh_evidence_deteriorated"
        elif weighted_total >= 4 and score >= 0.78 and win_rate >= 0.72:
            state = CreatorState.ELITE
            reason = "elite_recent_evidence"
        elif weighted_total >= 2 and score >= 0.60 and win_rate >= 0.55:
            state = CreatorState.PROMOTED
            reason = "positive_recent_evidence"
        else:
            state = CreatorState.PROBATION
            reason = "insufficient_or_mixed_recent_evidence"

        lifecycle = CreatorLifecycle(
            creator,
            state,
            score,
            weighted_total,
            win_rate,
            weighted_pnl,
            consecutive_losses,
            reason,
        )
        self._persist(lifecycle, now)
        return lifecycle

    def _persist(self, value: CreatorLifecycle, now: int) -> None:
        self.conn.execute(
            """
            INSERT INTO v12_creator_lifecycle(
                creator,state,score,effective_samples,recent_win_rate,
                decayed_pnl_sol,consecutive_losses,updated_ns,reason
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(creator) DO UPDATE SET
                state=excluded.state,score=excluded.score,
                effective_samples=excluded.effective_samples,
                recent_win_rate=excluded.recent_win_rate,
                decayed_pnl_sol=excluded.decayed_pnl_sol,
                consecutive_losses=excluded.consecutive_losses,
                updated_ns=excluded.updated_ns,reason=excluded.reason
            """,
            (
                value.creator,
                value.state.value,
                value.score,
                value.effective_samples,
                value.recent_win_rate,
                value.decayed_pnl_sol,
                value.consecutive_losses,
                now,
                value.reason,
            ),
        )

    def get(self, creator: str) -> CreatorLifecycle:
        row = self.conn.execute(
            "SELECT * FROM v12_creator_lifecycle WHERE creator=?",
            (str(creator),),
        ).fetchone()
        if row is None:
            return self.recompute(str(creator))
        return CreatorLifecycle(
            str(row["creator"]),
            CreatorState(str(row["state"])),
            float(row["score"]),
            float(row["effective_samples"]),
            float(row["recent_win_rate"]),
            float(row["decayed_pnl_sol"]),
            int(row["consecutive_losses"]),
            str(row["reason"]),
        )

    def entry_allowed(self, creator: str) -> bool:
        return self.get(creator).entry_allowed

    def risk_multiplier(self, creator: str) -> float:
        return self.get(creator).risk_multiplier

    def prune(self, now_ns: int | None = None) -> None:
        cutoff = int(now_ns or time.time_ns()) - int(self.retention_ns)
        self.conn.execute(
            "DELETE FROM v12_creator_live_evidence WHERE observed_ns<?",
            (cutoff,),
        )
