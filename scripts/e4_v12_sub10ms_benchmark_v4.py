#!/usr/bin/env python3
from __future__ import annotations

import runpy
from pathlib import Path

from memecoin_bot import e4_sub10ms_runtime_final_v12  # noqa: F401


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("e4_v12_sub10ms_benchmark.py")),
        run_name="__main__",
    )
