from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any, Mapping

from . import e4_direct_copy_v12 as direct

core = direct.core
v6 = direct.v6
PIPELINES = direct.PIPELINES

DEFAULT_BUY_SLIPPAGE_BPS = 800
DEFAULT_MAX_OUTPUT_SHORTFALL_BPS = 800
MAX_ALLOWED_GUARD_BPS = 2_500


def policy_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assert_policy_fingerprint(expected: str) -> None:
    expected = str(expected or "").strip().lower()
    actual = policy_fingerprint()
    if not expected or expected != actual:
        raise RuntimeError(
            "E4 V12 tight-output policy fingerprint mismatch "
            f"expected={expected or '<missing>'} actual={actual}"
        )


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def guarded_buy_slippage_bps(_settings: Any | None = None) -> int:
    """Return the maximum deterioration V12 is willing to sign for a BUY.

    This deliberately replaces the old 9,000-bps direct-copy setting. E4's
    failed BuyExactSolIn transactions show that the role model permits a buy to
    fail when token output deteriorates; V12 must not force a poisoned fill.
    """
    requested = _integer(
        os.getenv("E4_V12_BUY_SLIPPAGE_BPS"),
        DEFAULT_BUY_SLIPPAGE_BPS,
    )
    return min(MAX_ALLOWED_GUARD_BPS, max(0, requested))


def max_output_shortfall_bps() -> int:
    requested = _integer(
        os.getenv("E4_V12_MAX_OUTPUT_SHORTFALL_BPS"),
        DEFAULT_MAX_OUTPUT_SHORTFALL_BPS,
    )
    return min(MAX_ALLOWED_GUARD_BPS, max(0, requested))


def _profile_family(mint: str) -> str:
    profile = v6._PROFILE_BY_MINT.get(str(mint))
    return str(getattr(profile, "family", "") or "") if profile is not None else ""


def _scaled_source_token_reference(mint: str, submitted_sol: float) -> float:
    source = PIPELINES.e4_signal(str(mint))
    if source is None:
        return 0.0
    source_sol = max(0.0, _finite(getattr(source, "entry_sol", 0.0)))
    source_tokens = max(0.0, _finite(getattr(source, "entry_tokens", 0.0)))
    if source_sol <= 0 or source_tokens <= 0 or submitted_sol <= 0:
        return 0.0
    return source_tokens * submitted_sol / source_sol


def guarded_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Attach immutable BuyExactSolIn-style output protection to a BUY.

    The local builder converts this reference into a minimum-token instruction.
    For a reactive E4 copy, the reference is E4's observed token output scaled
    to V12's submitted SOL. If the current curve cannot still provide the
    protected minimum, construction fails and no transaction is broadcast.
    Pre-impact entries use the current decision-time reserve quote plus the
    same bounded slippage guard.
    """
    enriched = dict(request)
    if str(enriched.get("side") or "").upper() != "BUY":
        return enriched

    mint = str(enriched.get("mint") or "")
    metadata = dict(enriched.get("metadata") or {})
    family = str(metadata.get("e4_family") or _profile_family(mint))
    is_direct = bool(metadata.get("e4_direct_copy")) or family in {
        direct.DIRECT_COPY_FAMILY,
        "e4_confirmed_fast_copy",
    }
    is_preimpact = bool(metadata.get("e4_preimpact")) or family in {
        "authorized_prearmed_launch",
        "v12_golden_preimpact",
        "v12_golden_entry",
        "e4_sequential_preintent",
    }
    if not (is_direct or is_preimpact):
        return enriched

    submitted_sol = max(0.0, _finite(enriched.get("amount")))
    expected_tokens = max(
        0.0,
        _finite(metadata.get("expected_token_output")),
    )
    if expected_tokens <= 0 and is_direct:
        expected_tokens = _scaled_source_token_reference(mint, submitted_sol)

    guard_bps = max_output_shortfall_bps()
    metadata.update(
        {
            "strict_output_guard": True,
            "max_output_shortfall_bps": guard_bps,
            "output_reference": (
                "scaled_e4_observed_tokens"
                if expected_tokens > 0 and is_direct
                else "decision_time_curve_quote"
            ),
        }
    )
    if expected_tokens > 0:
        metadata["expected_token_output"] = expected_tokens

    enriched["metadata"] = metadata
    enriched["slippage_bps"] = min(
        guarded_buy_slippage_bps(),
        max(0, _integer(enriched.get("slippage_bps"), guarded_buy_slippage_bps())),
    )
    return enriched


# Direct-copy code resolves this function through its module globals at runtime,
# so replacing it here removes the 9,000-bps forced-fill default without
# editing or weakening the immutable direct-copy recognition policy.
direct.direct_copy_slippage_bps = guarded_buy_slippage_bps

_PREVIOUS_EXECUTE = core.Engine.execute


async def _execute_with_tight_output_v12(
    self: Any,
    request_id: str,
    request: Mapping[str, Any],
):
    return await _PREVIOUS_EXECUTE(self, request_id, guarded_request(request))


_execute_with_tight_output_v12._e4_tight_output_v12 = True  # type: ignore[attr-defined]
core.Engine.execute = _execute_with_tight_output_v12
