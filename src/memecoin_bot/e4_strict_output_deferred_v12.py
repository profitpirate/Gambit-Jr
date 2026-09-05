from __future__ import annotations

from typing import Any, Mapping

from . import e4_strict_output_v12 as strict
from . import e4_tight_output_v12 as guard


def _has_curve_metadata(metadata: Mapping[str, Any]) -> bool:
    return bool(
        metadata.get("virtual_sol_reserves") not in (None, "", 0, 0.0)
        and metadata.get("virtual_token_reserves") not in (None, "", 0, 0.0)
    )


def guarded_request(request: Mapping[str, Any]) -> dict[str, Any]:
    # Attach the bounded slippage/reference fields immediately. Direct-copy
    # requests receive current curve reserves later inside V10's async request
    # enrichment, so the Python layer must not mistake "not enriched yet" for
    # an economically bad quote. The strict Node builder wrapper performs the
    # same check after enrichment and remains fail-closed.
    enriched = strict._PREVIOUS_GUARDED_REQUEST(request)
    metadata = enriched.get("metadata") if isinstance(enriched.get("metadata"), Mapping) else {}
    if not bool(metadata.get("strict_output_guard")):
        return enriched

    current = strict.current_curve_token_quote(enriched)
    expected = max(0.0, strict._finite(metadata.get("expected_token_output")))
    shortfall_bps = max(
        0,
        min(
            guard.MAX_ALLOWED_GUARD_BPS,
            int(strict._finite(metadata.get("max_output_shortfall_bps"), guard.max_output_shortfall_bps())),
        ),
    )
    if expected > 0 and _has_curve_metadata(metadata):
        minimum = expected * (1.0 - shortfall_bps / 10_000.0)
        if current <= 0 or current + max(1e-9, minimum * 1e-12) < minimum:
            raise RuntimeError(
                "E4 V12 strict token-output rejection: "
                f"quoted={current:.9f} required={minimum:.9f} "
                f"expected={expected:.9f} guard_bps={shortfall_bps}"
            )

    result = dict(enriched)
    updated = dict(metadata)
    updated["current_quoted_token_output"] = current if current > 0 else None
    updated["strict_output_check_deferred"] = not _has_curve_metadata(metadata)
    result["metadata"] = updated
    return result


guard.guarded_request = guarded_request
