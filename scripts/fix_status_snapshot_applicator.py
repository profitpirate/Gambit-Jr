from pathlib import Path

path = Path(__file__).with_name("apply_status_snapshot_consistency.py")
text = path.read_text(encoding="utf-8")
original = text
text = text.replace("from __future__ import annotations\n\n", "", 1)
text = text.replace("from pathlib import Path\n\n\n", "from pathlib import Path\n\n", 1)
if text != original:
    path.write_text(text, encoding="utf-8")
    print("normalized status snapshot applicator imports")
else:
    print("status snapshot applicator imports already normalized")
