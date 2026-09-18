import os

os.environ.setdefault("E4_BUILDER_COMMAND", "node tools/e4-builder/daemon-v2.mjs")

from memecoin_bot.e4_exec import main

if __name__ == "__main__":
    main()
