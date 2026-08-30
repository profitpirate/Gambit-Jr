from __future__ import annotations

import time

from . import e4_hardening

core = e4_hardening.core


def _has_entered(self: core.Store, mint: str) -> bool:
    row = self.conn.execute(
        "SELECT entry_count,last_action FROM e4_seen_mints WHERE mint=?", (mint,)
    ).fetchone()
    return bool(
        row
        and (
            int(row[0]) >= 1
            or str(row[1]) in {"BUY_PENDING", "BUY_UNCERTAIN", "BUY_CONFIRMED"}
        )
    )


def _reserve_entry(self: core.Store, mint: str, score: float, reason: str) -> bool:
    self.conn.execute("BEGIN IMMEDIATE")
    try:
        row = self.conn.execute(
            "SELECT entry_count,last_action FROM e4_seen_mints WHERE mint=?", (mint,)
        ).fetchone()
        if row and (
            int(row[0]) >= 1
            or str(row[1]) in {"BUY_PENDING", "BUY_UNCERTAIN", "BUY_CONFIRMED"}
        ):
            self.conn.execute("ROLLBACK")
            return False
        now = time.time_ns()
        if row:
            self.conn.execute(
                "UPDATE e4_seen_mints SET first_seen_ns=?,entry_count=0,"
                "last_action='BUY_PENDING',last_reason=?,last_score=? WHERE mint=?",
                (now, reason, score, mint),
            )
        else:
            self.conn.execute(
                "INSERT INTO e4_seen_mints VALUES(?,?,?,?,?,?)",
                (mint, now, 0, "BUY_PENDING", reason, score),
            )
        self.conn.execute("COMMIT")
        return True
    except Exception:
        self.conn.execute("ROLLBACK")
        raise


core.Store.has_entered = _has_entered
core.Store.mark_entry = _reserve_entry
