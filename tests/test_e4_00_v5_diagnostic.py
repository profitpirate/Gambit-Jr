from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from memecoin_bot import e4_hardening_v5
from tests.test_e4_hardening_v5 import _Engine, _event, _load, _seed_position

core = e4_hardening_v5.core


class V5Diagnostic(unittest.IsolatedAsyncioTestCase):
    async def test_print_concurrent_real_path_fractions(self) -> None:
        fixture = _load()["scenarios"]["two_concurrent_launches"]
        engine = _Engine()
        base_ns = time.time_ns()
        remaining: list[dict] = []
        for path in fixture["paths"]:
            _, _, rows = _seed_position(engine, path, base_ns)
            remaining.extend(rows)
        remaining.sort(key=lambda row: (row["t_us"], row["mint"], row["signature"]))
        for row in remaining:
            state = engine.tokens[row["mint"]]
            event = _event(row, base_ns)
            with patch.object(core.time, "time_ns", return_value=event.source_ns):
                state.apply(event, None)
                position = engine.positions.get(row["mint"])
                if position is not None:
                    await e4_hardening_v5._evaluate_current_position(
                        engine,
                        position,
                        "diagnostic",
                        event_id=event.event_id,
                    )
            await engine.drain()
        print("E4_V5_DIAGNOSTIC_SELL_CALLS", engine.sell_calls, flush=True)


if __name__ == "__main__":
    unittest.main()
