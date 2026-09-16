"""Loss2s campaign publisher using an isolated evidence prefix on its branch."""
from __future__ import annotations

import golden_top4_publish as base

base.PREFIX = "docs/research/golden-loss2s-"


def main():
    return base.main()


if __name__ == "__main__":
    main()
