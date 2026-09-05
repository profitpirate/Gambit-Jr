#!/usr/bin/env python3
from __future__ import annotations

from memecoin_bot import e4_strict_output_deferred_v12 as deferred
from scripts import e4_v12_hotpath_benchmark as base

# The first benchmark fixture used a placeholder Token-2022 key. Replace only
# that immutable fixture constant with Solana's canonical Token-2022 program ID
# while retaining the exact measured benchmark implementation.
_PLACEHOLDER = "TokenzQdY9rKXbX7mBfYvKz2zZ1zV7P3GmZrM7vQqWk"
_CANONICAL = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
base.benchmark.__code__ = base.benchmark.__code__.replace(
    co_consts=tuple(_CANONICAL if value == _PLACEHOLDER else value for value in base.benchmark.__code__.co_consts)
)
base.strict.guarded_request = deferred.guarded_request


if __name__ == "__main__":
    raise SystemExit(base.main())
