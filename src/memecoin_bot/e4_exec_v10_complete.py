from __future__ import annotations

# Importing the copy-exit layer after the base V10 runtime preserves the
# identity/social/E4-teacher entry policy and adds E4 sell-intent mirroring for
# positions whose entry family was an E4-confirmed copy.
from memecoin_bot import e4_copy_exit_v10  # noqa: F401
from memecoin_bot.e4_exec_v10 import run

if __name__ == "__main__":
    run()
