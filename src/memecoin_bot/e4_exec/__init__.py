"""Authoritative Gambit Jr E4 autonomous execution entrypoint.

Boot order is deliberate:
1. load the evidence-backed legacy hardening chain;
2. load final execution/reconciliation patches;
3. install Unified Selection v2 last so no legacy module can overwrite it.
"""

from memecoin_bot import e4_hardening_v9 as _legacy_v9  # noqa: F401
from memecoin_bot import e4_final as _final
from memecoin_bot.e4_selection_v2 import install as _install_selection_v2

_install_selection_v2(_final.core, force=True)

main = _final.main

__all__ = ["main"]
