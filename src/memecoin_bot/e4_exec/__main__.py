import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v2.mjs")

from memecoin_bot import e4_hardening_v8  # noqa: E402,F401 - identity-first E4 policy, repeat-winning creators, prelaunch social/J7 authorization
from memecoin_bot.e4_final import main  # noqa: E402

if __name__ == "__main__":
    main()
