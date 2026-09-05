from __future__ import annotations

import math
from typing import Any, Mapping

from . import e4_tight_output_v12 as guard

LAMPORTS_PER_SOL = 1_000_000_000.0
TOKEN_SCALE = 1_000_000.0


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normal_sol(value: Any) -> float:
    amount = _finite(value)
    return amount / LAMPORTS_PER_SOL if amount >= 1_000_000.0 else amount


def _normal_tokens(value: Any) -> float:
    amount = _finite(value)
    return amount / TOKEN_SCALE if amount >= 10_000_000_000.0 else amount


def current_curve_token_quote(request: Mapping[str, Any]) -> float:
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
    virtual_sol = _normal_sol(metadata.get("virtual_sol_reserves"))
    virtual_tokens = _normal_tokens(metadata.get("virtual_token_reserves"))
    real_tokens = _normal_tokens(metadata.get("real_token_reserves"))
    max_cost = max(0.0, _finite(request.get("amount")))
    total_fee_bps = _finite(
        metadata.get("total_fee_bps", metadata.get("fee_bps")),
        _finite(metadata.get("protocol_fee_bps"), 100.0)
        + _finite(metadata.get("creator_fee_bps"), 25.0),
    )
    if min(virtual_sol, virtual_tokens, max_cost) <= 0:
        return 0.0
    curve_input = max_cost / (1.0 + max(0.0, total_fee_bps) / 10_000.0)
    quoted = curve_input * virtual_tokens / (virtual_sol + curve_input)
    if real_tokens > 0:
        quoted = min(quoted, real_tokens)
    return max(0.0, quoted)


_PREVIOUS_GUARDED_REQUEST = guard.guarded_request


def guarded_request(request: Mapping[str, Any]) -> dict[str, Any]:
    enriched = _PREVIOUS_GUARDED_REQUEST(request)
    metadata = enriched.get("metadata") if isinstance(enriched.get("metadata"), Mapping) else {}
    if not bool(metadata.get("strict_output_guard")):
        return enriched

    expected = max(0.0, _finite(metadata.get("expected_token_output")))
    current = current_curve_token_quote(enriched)
    shortfall_bps = max(0, min(guard.MAX_ALLOWED_GUARD_BPS, int(_finite(metadata.get("max_output_shortfall_bps"), guard.max_output_shortfall_bps()))))
    if expected > 0:
        minimum = expected * (1.0 - shortfall_bps / 10_000.0)
        if current <= 0 or current + max(1e-9, minimum * 1e-12) < minimum:
            raise RuntimeError(
                "E4 V12 strict token-output rejection: "
                f"quoted={current:.9f} required={minimum:.9f} "
                f"expected={expected:.9f} guard_bps={shortfall_bps}"
            )
    result = dict(enriched)
    updated = dict(metadata)
    updated["current_quoted_token_output"] = current
    result["metadata"] = updated
    return result


# The executor installed by e4_tight_output_v12 resolves this module global at
# call time. Replacing it upgrades the same hot path without stacking another
# asynchronous wrapper or adding an extra await to receive-to-build latency.
guard.guarded_request = guarded_request
