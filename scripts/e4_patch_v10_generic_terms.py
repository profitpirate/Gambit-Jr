#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    path = Path("src/memecoin_bot/e4_pipelines_v10.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''    for size in range(2, min(5, len(words) + 1)):
        for index in range(0, len(words) - size + 1):
            terms.add(" ".join(words[index : index + size]))
''',
        '''    for size in range(2, min(5, len(words) + 1)):
        for index in range(0, len(words) - size + 1):
            parts = words[index : index + size]
            if any(part not in _GENERIC_SOCIAL_TERMS for part in parts):
                terms.add(" ".join(parts))
''',
        "launch ngram generic filter",
    )
    text = replace_once(
        text,
        '''            if any(word not in _STOPWORDS for word in words[index : index + size]):
                terms.add(phrase)
''',
        '''            parts = words[index : index + size]
            if any(
                word not in _STOPWORDS and word not in _GENERIC_SOCIAL_TERMS
                for word in parts
            ):
                terms.add(phrase)
''',
        "post ngram generic filter",
    )
    path.write_text(text, encoding="utf-8")
    print("patched generic-only narrative matching")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
