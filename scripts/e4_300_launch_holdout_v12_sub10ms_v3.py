#!/usr/bin/env python3
from __future__ import annotations

import runpy
from pathlib import Path

from memecoin_bot import e4_hardening_v12  # noqa: F401
from memecoin_bot import e4_role_model_v12  # noqa: F401
from memecoin_bot import e4_direct_copy_v12  # noqa: F401
from memecoin_bot import e4_sub10ms_repairs_v12  # noqa: F401
from memecoin_bot import e4_sub10ms_transport_v12  # noqa: F401
from memecoin_bot import e4_sub10ms_runtime_v12  # noqa: F401


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("e4_300_launch_holdout_v12.py")),
        run_name="__main__",
    )
