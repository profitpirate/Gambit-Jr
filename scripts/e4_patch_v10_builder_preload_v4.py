#!/usr/bin/env python3
from pathlib import Path


def main() -> int:
    path = Path("tools/e4-builder/race-proxy-v3.mjs")
    text = path.read_text(encoding="utf-8")
    old = 'const preload = path.join(here, "fast-preload-v3.mjs");'
    new = '''const preload = process.env.E4_BUILDER_PRELOAD
  ? path.resolve(process.env.E4_BUILDER_PRELOAD)
  : path.join(here, "fast-preload-v4.mjs");'''
    if old in text:
        text = text.replace(old, new, 1)
    elif "fast-preload-v4.mjs" not in text:
        raise RuntimeError("race proxy preload declaration not found")
    path.write_text(text, encoding="utf-8")
    print("switched raced builder default preload to v4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
