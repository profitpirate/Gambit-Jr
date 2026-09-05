#!/usr/bin/env python3
from __future__ import annotations

from scripts import e4_v12_reactive_profit_model as model
from scripts import e4_v12_true_latency_replay_v3  # noqa: F401,E402


if __name__ == "__main__":
    raise SystemExit(model.main())
