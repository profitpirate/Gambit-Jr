import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v3.mjs")

from memecoin_bot import e4_hardening_v7  # noqa: E402,F401 - applies evidence-backed V6 policy plus V7 compatibility repairs
from memecoin_bot.e4_final import main  # noqa: E402

if __name__ == "__main__":
    main()
