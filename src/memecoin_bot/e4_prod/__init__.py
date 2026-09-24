"""Compatibility alias for the canonical V12 production entrypoint.

The historical e4_prod command is retained for operator compatibility but
must never provide an alternate execution path around V12 readiness/security.
"""


def main() -> None:
    from memecoin_bot.e4_exec.__main__ import main as canonical_main

    canonical_main()


__all__ = ["main"]
