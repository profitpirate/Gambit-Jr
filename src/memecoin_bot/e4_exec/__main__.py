import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v2.mjs")

from memecoin_bot import e4_hardening_v9  # noqa: E402,F401 - causal creator expectancy, negative-history veto, identity-only entry authority
from memecoin_bot.e4_final import main  # noqa: E402

if __name__ == "__main__":
    main()
