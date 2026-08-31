#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    path = Path("scripts/e4_v10_social_stream.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''import time
from collections import Counter, deque
''',
        '''import time
from collections import Counter, deque
from datetime import datetime, timezone
''',
        "datetime import",
    )
    text = replace_once(
        text,
        '''def authority(followers: int, configured_score: float = 0.0) -> float:
    follower_component = min(1.0, max(0.0, math.log10(max(1, followers)) / 6.0))
    return min(1.0, max(configured_score, follower_component))
''',
        '''def authority(followers: int, configured_score: float = 0.0, verified: bool = False) -> float:
    follower_component = min(1.0, max(0.0, math.log10(max(1, followers)) / 6.0))
    # Verification is supporting evidence, not automatic maximum authority.
    verification_bonus = 0.04 if verified else 0.0
    return min(1.0, max(configured_score, follower_component + verification_bonus))


def timestamp_ns(value: object) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000_000 else number * 1_000_000_000
    text = str(value or "").strip()
    if not text:
        return time.time_ns()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return time.time_ns()
''',
        "authority and timestamp helper",
    )
    text = replace_once(
        text,
        '''                        created_ns = time.time_ns()
                        payload = {
''',
        '''                        created_ns = timestamp_ns(data.get("created_at"))
                        payload = {
''',
        "created_at parsing",
    )
    text = replace_once(
        text,
        '''                            "authority": authority(followers, 1.0 if user.get("verified") else 0.0),
''',
        '''                            "authority": authority(
                                followers,
                                float(user.get("e4_authority_score") or 0.0),
                                bool(user.get("verified")),
                            ),
''',
        "authority call",
    )
    path.write_text(text, encoding="utf-8")
    print("patched X social stream timestamps and authority scoring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
