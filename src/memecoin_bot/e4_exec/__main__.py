import atexit
import os

os.environ.setdefault(
    "E4_BUILDER_COMMAND",
    "node tools/e4-builder/race-proxy-v3.mjs",
)

from memecoin_bot import e4_hardening_v12  # noqa: E402,F401 - permanent V12 authority
from memecoin_bot import e4_role_model_v12  # noqa: E402 - direct E4/creator/social wiring
from memecoin_bot import e4_direct_copy_v12  # noqa: E402 - forced recognized-E4 execution
from memecoin_bot.e4_pipeline_runtime_v10 import start_background_supervisor  # noqa: E402
from memecoin_bot.e4_role_model_v12 import stop_background_supervisor  # noqa: E402
from memecoin_bot.e4_runtime_services_v10 import (  # noqa: E402
    start_runtime_services,
    stop_runtime_services,
)
from memecoin_bot.e4_final import main  # noqa: E402

E4_V12_ROLE_MODEL_POLICY_SHA256 = "f4d5959b25f607bc667073b672d66570bf29d8d2b2020811605808ce08e032df"
E4_V12_DIRECT_COPY_POLICY_SHA256 = "7721bef97c992e1fff5dffb1b678e0bb779d3c62eddcec7b6ce53c20b51731cb"
e4_role_model_v12.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)
e4_direct_copy_v12.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)


def _start_v12_pipelines() -> None:
    # Creator/social/intent services and direct E4 wallet observation are both
    # required. Starting only one side leaves a pipeline present but inert.
    start_runtime_services()
    start_background_supervisor()
    atexit.register(stop_background_supervisor)
    atexit.register(stop_runtime_services)


if __name__ == "__main__":
    _start_v12_pipelines()
    main()
