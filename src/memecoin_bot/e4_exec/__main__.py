import atexit
import os

os.environ.setdefault(
    "E4_BUILDER_COMMAND",
    "node tools/e4-builder/race-proxy-v12.mjs",
)

from memecoin_bot import e4_hardening_v12  # noqa: E402,F401 - permanent V12 authority
from memecoin_bot import e4_role_model_v12  # noqa: E402 - direct E4/creator/social wiring
from memecoin_bot import e4_direct_copy_v12  # noqa: E402 - forced recognized-E4 execution
from memecoin_bot import e4_copy_fidelity_v12  # noqa: E402 - E4-authoritative exits + warm fanout
from memecoin_bot import e4_preconfirm_v12  # noqa: E402 - instruction-level preconfirm authority
from memecoin_bot.e4_pipeline_runtime_v10 import start_background_supervisor  # noqa: E402
from memecoin_bot.e4_role_model_v12 import stop_background_supervisor  # noqa: E402
from memecoin_bot.e4_runtime_services_v10 import (  # noqa: E402
    start_runtime_services,
    stop_runtime_services,
)
from memecoin_bot.e4_final import main  # noqa: E402

E4_V12_ROLE_MODEL_POLICY_SHA256 = "f4d5959b25f607bc667073b672d66570bf29d8d2b2020811605808ce08e032df"
E4_V12_DIRECT_COPY_POLICY_SHA256 = "b286da26c965420ce2146b396b76717cb79c681c1ea90e72120662d953a6bdc9"
E4_V12_COPY_FIDELITY_POLICY_SHA256 = "f02f3bafd259fcfee918568397ebb83906681773a4958b5d58728ade500f0633"
E4_V12_PRECONFIRM_POLICY_SHA256 = "3aef1c8ec287f529be4dd9314c383b748b818fe913e62639a3c0b52b29420961"
e4_role_model_v12.assert_policy_fingerprint(E4_V12_ROLE_MODEL_POLICY_SHA256)
e4_direct_copy_v12.assert_policy_fingerprint(E4_V12_DIRECT_COPY_POLICY_SHA256)
e4_copy_fidelity_v12.assert_policy_fingerprint(E4_V12_COPY_FIDELITY_POLICY_SHA256)
e4_preconfirm_v12.assert_policy_fingerprint(E4_V12_PRECONFIRM_POLICY_SHA256)


def _start_v12_pipelines() -> None:
    start_runtime_services()
    start_background_supervisor()
    atexit.register(stop_background_supervisor)
    atexit.register(stop_runtime_services)


if __name__ == "__main__":
    _start_v12_pipelines()
    main()
