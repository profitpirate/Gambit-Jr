"""Final Gambit Jr E4 autonomous execution entrypoint.

Package-level main deliberately delegates to the canonical CLI wrapper so
console scripts cannot bypass live-readiness, policy fingerprints, or runtime
service startup.
"""


def main() -> None:
    from memecoin_bot.e4_exec.__main__ import main as canonical_main

    canonical_main()


__all__ = ["main"]
