from __future__ import annotations

import atexit
import os

os.environ.setdefault(
    "E4_BUILDER_COMMAND",
    "node tools/e4-builder/strict-race-proxy-v12.mjs",
)

from memecoin_bot import e4_hardening_v12  # noqa: E402,F401
from memecoin_bot import e4_role_model_v12  # noqa: E402
from memecoin_bot import e4_direct_copy_v12  # noqa: E402
from memecoin_bot import e4_tight_output_v12  # noqa: E402,F401
from memecoin_bot import e4_strict_output_v12  # noqa: E402,F401
from memecoin_bot import e4_strict_output_deferred_v12  # noqa: E402,F401
from memecoin_bot import e4_transport_v12  # noqa: E402,F401
from memecoin_bot.e4_pipeline_runtime_v10 import start_background_supervisor  # noqa: E402
from memecoin_bot.e4_role_model_v12 import stop_background_supervisor  # noqa: E402
from memecoin_bot.e4_runtime_services_v10 import (  # noqa: E402
    start_runtime_services,
    stop_runtime_services,
)
from memecoin_bot.e4_final import main  # noqa: E402

E4_V12_ROLE_MODEL_POLICY_SHA256 = "f4d5959b25f607bc667073b672d66570bf29d8d2b2020811605808ce08e032df"
E4_V12_DIRECT_COPY_POLICY_SHA256 = "ed3e29edef1484a46a16858c303b97d0155ecf88aa63a23d95e6839592ee2f5e"
e4_role_model_v12.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)
e4_direct_copy_v12.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)


def _start_v12_pipelines() -> None:
    start_runtime_services()
    start_background_supervisor()
    atexit.register(stop_background_supervisor)
    atexit.register(stop_runtime_services)


if __name__ == "__main__":
    _start_v12_pipelines()
    main()
