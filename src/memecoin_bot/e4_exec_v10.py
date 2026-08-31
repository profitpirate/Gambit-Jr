from __future__ import annotations

import atexit
import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v2.mjs")
os.environ.setdefault("E4_REMOTE_BUILDER_FALLBACK", "false")

from memecoin_bot import e4_hardening_v10  # noqa: E402,F401
from memecoin_bot import e4_fast_execution_v10  # noqa: E402,F401
from memecoin_bot.e4_runtime_services_v10 import (  # noqa: E402
    start_runtime_services,
    stop_runtime_services,
)
from memecoin_bot.e4_final import main  # noqa: E402


def run() -> None:
    start_runtime_services()
    atexit.register(stop_runtime_services)
    main()


if __name__ == "__main__":
    run()
