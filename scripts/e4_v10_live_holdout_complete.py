#!/usr/bin/env python3
from __future__ import annotations

import runpy
from pathlib import Path

from memecoin_bot import e4_copy_exit_v10  # noqa: F401

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("e4_v10_live_holdout.py")), run_name="__main__")
