from __future__ import annotations

import time

from . import e4_live


def _save_position(self: e4_live.Store, position: e4_live.Position) -> None:
    self.conn.execute(
        """INSERT INTO e4_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(position_id) DO UPDATE SET status=excluded.status,remaining=excluded.remaining,
        max_price=excluded.max_price,last_price=excluded.last_price,
        first_partial_done=excluded.first_partial_done,
        first_partial_fraction=excluded.first_partial_fraction,
        realized_sol=excluded.realized_sol,close_signature=excluded.close_signature,
        route=excluded.route,updated_ns=excluded.updated_ns""",
        (
            position.position_id,
            position.mint,
            position.status.value,
            position.opened_ns,
            position.entry_sol,
            position.tokens,
            position.remaining,
            position.entry_price,
            position.max_price,
            position.last_price,
            position.entry_signature,
            int(position.first_partial_done),
            position.first_partial_fraction,
            position.realized_sol,
            position.close_signature,
            position.route,
            time.time_ns(),
        ),
    )


# Apply the corrected persistence implementation before any Engine is created.
e4_live.Store.save_position = _save_position


def main() -> None:
    e4_live.main()


if __name__ == "__main__":
    main()
