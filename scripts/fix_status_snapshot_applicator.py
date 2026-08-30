from pathlib import Path


path = Path(__file__).with_name("apply_status_snapshot_consistency.py")
text = path.read_text(encoding="utf-8")
future_import = "from __future__ import annotations\n\n"
if future_import in text:
    text = text.replace(future_import, "", 1)
    path.write_text(text, encoding="utf-8")
    print("normalized status snapshot applicator imports")
else:
    print("status snapshot applicator imports already normalized")
