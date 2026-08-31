import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v2.mjs")
os.environ.setdefault("E4_ROUTE_RACE_DELAY_MS", "0")
os.environ.setdefault("E4_HISTORY_SCANNER_COMMAND", "python scripts/e4_creator_history_scanner.py")

from memecoin_bot import e4_hardening_v10  # noqa: E402,F401
from memecoin_bot.e4_final import main  # noqa: E402

if __name__ == "__main__":
    main()
