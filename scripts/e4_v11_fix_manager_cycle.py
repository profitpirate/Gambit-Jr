#!/usr/bin/env python3
from pathlib import Path

pipelines = Path("src/memecoin_bot/e4_pipelines_v10.py")
text = pipelines.read_text(encoding="utf-8")
text = text.replace(
    "from .e4_pipeline_singleton_v11 import manager  # noqa: E402",
    "manager = PipelineManager()  # canonical same-process V11 authority",
)
pipelines.write_text(text, encoding="utf-8")

hardening = Path("src/memecoin_bot/e4_hardening_v10.py")
text = hardening.read_text(encoding="utf-8")
text = text.replace(
    "from .e4_pipeline_singleton_v11 import manager as PIPELINES",
    "from .e4_pipelines_v10 import manager as PIPELINES",
)
needle = '    features = dict(v8._identity_features(state))\n'
if 'microburst_bundled_buys' not in text[text.index('def _entry_v10('):text.index('core.E4Policy.entry = _entry_v10')]:
    addition = '''    # Keep the observed V4/V5 burst telemetry even though public flow no\n    # longer has entry authority. These metrics are useful for forensics and\n    # regression testing without reintroducing the failed flow-only strategy.\n    buy_events = [\n        event for event in state.events\n        if event.kind in {core.EventKind.BUY, core.EventKind.PUMPSWAP_BUY}\n    ]\n    features["microburst_buyers"] = float(len({event.trader for event in buy_events if event.trader}))\n    signature_counts: dict[str, int] = {}\n    for event in buy_events:\n        if event.signature:\n            signature_counts[event.signature] = signature_counts.get(event.signature, 0) + 1\n    features["microburst_bundled_buys"] = float(sum(\n        count for count in signature_counts.values() if count > 1\n    ))\n'''
    text = text.replace(needle, needle + addition, 1)
hardening.write_text(text, encoding="utf-8")
print("V11 manager import cycle removed; burst telemetry preserved")
