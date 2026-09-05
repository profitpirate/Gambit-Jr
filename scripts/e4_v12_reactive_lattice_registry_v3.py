#!/usr/bin/env python3
from __future__ import annotations

from scripts import e4_v12_reactive_profit_registry  # noqa: F401
from scripts import e4_v12_reactive_lattice_search as lattice
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401,E402


if __name__ == "__main__":
    raise SystemExit(lattice.main())
