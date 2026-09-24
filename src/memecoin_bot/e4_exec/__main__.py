import atexit
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "E4_BUILDER_COMMAND",
    "node tools/e4-builder/race-proxy-v3.mjs",
)
os.environ.setdefault("E4_BUILDER_WORKERS", "2")
os.environ.setdefault("E4_BUILDER_RACE_CHILDREN", "2")
os.environ.setdefault("E4_BALANCE_CACHE_MAX_STALENESS_MS", "5000")
os.environ.setdefault("E4_DIRECT_COPY_MAX_OUTPUT_SHORTFALL_BPS", "600")

from memecoin_bot import e4_hardening_v12  # noqa: E402,F401 - permanent V12 authority
from memecoin_bot import e4_role_model_v12  # noqa: E402 - direct E4/creator/social wiring
from memecoin_bot import e4_direct_copy_v12  # noqa: E402 - recognized-E4 execution
from memecoin_bot import e4_sub10ms_repairs_v12  # noqa: E402,F401 - output guard, exact exits
from memecoin_bot import e4_sub10ms_runtime_final_v12  # noqa: E402,F401 - final prewarm + route authority
from memecoin_bot import e4_notifications_v12  # noqa: E402,F401 - durable close/sweep notifications
from memecoin_bot import e4_nextgen_creator_authority_v12  # noqa: E402,F401 - gated canonical post-cert authority
from memecoin_bot import e4_adaptive_exit_v12  # noqa: E402,F401 - gated post-cert survivor exits
from memecoin_bot import e4_production_guard_v12  # noqa: E402,F401 - final live safety/recovery authority
from memecoin_bot.e4_pipeline_runtime_v10 import start_background_supervisor  # noqa: E402
from memecoin_bot.e4_role_model_v12 import stop_background_supervisor  # noqa: E402
from memecoin_bot.e4_runtime_services_v10 import (  # noqa: E402
    start_runtime_services,
    stop_runtime_services,
)
from memecoin_bot.e4_final import main as _engine_main  # noqa: E402
from memecoin_bot.v12_live_readiness import run_live_readiness  # noqa: E402

E4_V12_ROLE_MODEL_POLICY_SHA256 = "2eb324971185c4eacf09ca57c8e06609028381edd03e68c702a0a88e57600ea6"
E4_V12_DIRECT_COPY_POLICY_SHA256 = "cec133a234fa7e59dc3950dc6c2aa4902e5c12b43eb59bf31eb76d28287380b3"
e4_role_model_v12.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)
e4_direct_copy_v12.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)


_LIVE_INSTANCE_LOCK = None


def _preflight_live_if_requested() -> None:
    global _LIVE_INSTANCE_LOCK
    if "--live" not in sys.argv:
        return
    if os.getenv("E4_LIVE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("E4 live execution requires both E4_LIVE=true and --live")
    settings = e4_hardening_v12.core.Settings.from_env()
    settings.live = True
    settings.validate()
    _checks, _LIVE_INSTANCE_LOCK = run_live_readiness(
        settings,
        repository_root=Path.cwd(),
        acquire_lock=True,
    )
    if _LIVE_INSTANCE_LOCK is not None:
        atexit.register(_LIVE_INSTANCE_LOCK.release)


def _start_v12_pipelines() -> None:
    # Creator/social/intent services and direct E4 wallet observation are both
    # required. Starting only one side leaves a pipeline present but inert.
    start_runtime_services()
    start_background_supervisor()
    atexit.register(stop_background_supervisor)
    atexit.register(stop_runtime_services)


def main() -> None:
    """Canonical CLI wrapper; every live invocation passes the V12 readiness gate."""
    _preflight_live_if_requested()
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _start_v12_pipelines()
    _engine_main()


if __name__ == "__main__":
    main()
