import os

os.environ.setdefault(
    "E4_BUILDER_COMMAND",
    "node tools/e4-builder/race-proxy-v3.mjs",
)

from memecoin_bot import e4_hardening_v10  # noqa: E402,F401 - V10 three-pipeline authority
from memecoin_bot.e4_pipeline_runtime_v10 import start_background_supervisor  # noqa: E402
from memecoin_bot.e4_final import main  # noqa: E402

if __name__ == "__main__":
    start_background_supervisor()
    main()
