"""Authoritative Gambit Jr E4 autonomous execution entrypoint.

Boot order is deliberate:
1. load the evidence-backed legacy hardening chain;
2. load final execution/reconciliation patches;
3. install Unified Selection v2 last so no legacy module can overwrite it.
"""

import importlib

_legacy_v9 = importlib.import_module("memecoin_bot.e4_hardening_v9")
_final = importlib.import_module("memecoin_bot.e4_final")
_selection = importlib.import_module("memecoin_bot.e4_selection_v2")

_selection.install(_final.core, force=True)

main = _final.main

__all__ = ["main"]
