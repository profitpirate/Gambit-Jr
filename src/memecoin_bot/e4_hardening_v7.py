from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Mapping

from . import e4_hardening_v6

core = e4_hardening_v6.core
final = e4_hardening_v6.final
v6 = e4_hardening_v6
LOGGER = logging.getLogger("gambit.e4.hardening.v7")

_BUILD_CONTEXT_BY_MINT: dict[str, dict[str, Any]] = {}
_PREFETCH_IN_FLIGHT: set[str] = set()


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _bool_value(value: Any) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def _integer_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        # Preserve already exact database strings. Floats are accepted only for
        # integral values; raw reserve fields should normally arrive as strings.
        text = str(value)
        if text.isdigit():
            return text
        number = float(value)
        if number.is_integer() and number >= 0:
            return str(int(number))
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _extract_context(row: Mapping[str, Any], mint: str) -> dict[str, Any]:
    merged: dict[str, Any] = dict(row)
    parser = getattr(v6.v4.hardening, "_parse_json_mapping", None)
    if parser is not None:
        for key in ("payload_json", "event_json", "raw_json", "data_json", "payload"):
            for nested_key, nested_value in parser(merged.get(key)).items():
                merged.setdefault(str(nested_key), nested_value)

    previous = dict(_BUILD_CONTEXT_BY_MINT.get(mint, {}))
    values = {
        "token_program": _first(
            merged,
            "token_program",
            "token_program_id",
            "base_token_program",
        ),
        "virtual_token_reserves": _integer_text(
            _first(merged, "virtual_token_reserves", "virtual_base_reserves")
        ),
        "virtual_sol_reserves": _integer_text(
            _first(merged, "virtual_sol_reserves", "virtual_quote_reserves")
        ),
        "real_token_reserves": _integer_text(
            _first(merged, "real_token_reserves", "real_base_reserves")
        ),
        "real_sol_reserves": _integer_text(
            _first(merged, "real_sol_reserves", "real_quote_reserves")
        ),
        "token_total_supply": _integer_text(
            _first(merged, "token_total_supply", "total_supply", "base_supply")
        ),
        "creator": _first(merged, "creator", "creator_wallet", "deployer"),
        "mayhem_mode": _bool_value(
            _first(merged, "is_mayhem_mode", "mayhem_mode", "mayhem", default=False)
        ) if False else None,
    }
    # `_first` deliberately has no default argument; booleans are handled
    # separately so a missing false value does not overwrite known context.
    mayhem = _first(merged, "is_mayhem_mode", "mayhem_mode", "mayhem")
    cashback = _first(merged, "is_cashback_coin", "cashback", "cashback_coin")
    complete = _first(merged, "complete", "curve_complete")
    if mayhem is not None:
        values["mayhem_mode"] = _bool_value(mayhem)
    if cashback is not None:
        values["cashback"] = _bool_value(cashback)
    if complete is not None:
        values["complete"] = _bool_value(complete)

    for key, value in values.items():
        if value not in (None, ""):
            previous[key] = value
    previous["updated_ns"] = time.time_ns()
    return previous


_PREVIOUS_FROM_ROW = core.Event.from_row.__func__


def _event_with_builder_context(
    cls: type[core.Event],
    row: Mapping[str, Any],
) -> core.Event:
    event = _PREVIOUS_FROM_ROW(cls, row)
    _BUILD_CONTEXT_BY_MINT[event.mint] = _extract_context(row, event.mint)
    if event.creator and not _BUILD_CONTEXT_BY_MINT[event.mint].get("creator"):
        _BUILD_CONTEXT_BY_MINT[event.mint]["creator"] = event.creator
    return event


core.Event.from_row = classmethod(_event_with_builder_context)


def _enrich_request(request: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(request)
    metadata = dict(enriched.get("metadata") or {})
    mint = str(enriched.get("mint") or "")
    context = _BUILD_CONTEXT_BY_MINT.get(mint)
    if context:
        metadata["state_hint"] = {
            key: value
            for key, value in context.items()
            if key != "updated_ns"
        }
    enriched["metadata"] = metadata
    return enriched


async def _worker_request(
    self: final.BuilderWorker,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    async with self.lock:
        for attempt in range(2):
            await self.start()
            assert self.process and self.process.stdin and self.process.stdout
            try:
                self.process.stdin.write(
                    json.dumps(dict(request), separators=(",", ":")).encode() + b"\n"
                )
                await self.process.stdin.drain()
                line = await asyncio.wait_for(
                    self.process.stdout.readline(),
                    timeout=self.timeout,
                )
                if not line:
                    raise RuntimeError("persistent E4 builder closed stdout")
                result = json.loads(line)
                if result.get("error"):
                    raise RuntimeError(str(result["error"]))
                if result.get("request_id") not in {None, request.get("request_id")}:
                    raise RuntimeError("persistent E4 builder response ID mismatch")
                return dict(result)
            except Exception:
                await self.stop()
                if attempt:
                    raise
        raise RuntimeError("E4 builder exhausted retries")


async def _worker_build(
    self: final.BuilderWorker,
    request: Mapping[str, Any],
) -> str:
    result = await _worker_request(self, request)
    encoded = str(result["transaction_base64"])
    base64.b64decode(encoded, validate=True)
    return encoded


async def _worker_prefetch(
    self: final.BuilderWorker,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(request)
    payload["action"] = "PREFETCH"
    payload["side"] = "PREFETCH"
    return await _worker_request(self, payload)


final.BuilderWorker.request = _worker_request
final.BuilderWorker.build = _worker_build
final.BuilderWorker.prefetch = _worker_prefetch


async def _pool_build(
    self: final.BuilderPool,
    request: Mapping[str, Any],
) -> str:
    worker = await self.available.get()
    try:
        return await worker.build(_enrich_request(request))
    finally:
        self.available.put_nowait(worker)


async def _pool_prefetch(
    self: final.BuilderPool,
    request: Mapping[str, Any],
) -> list[dict[str, Any]]:
    enriched = _enrich_request(request)
    return list(
        await asyncio.gather(
            *(worker.prefetch(enriched) for worker in self.workers),
            return_exceptions=False,
        )
    )


final.BuilderPool.build = _pool_build
final.BuilderPool.prefetch = _pool_prefetch


async def _prefetch_mint(engine: core.Engine, event: core.Event) -> None:
    mint = event.mint
    try:
        prefetch = getattr(engine.builder, "prefetch", None)
        if prefetch is None or not engine.signer.wallet:
            return
        await prefetch(
            {
                "request_id": f"prefetch:{mint}:{event.event_id}",
                "action": "PREFETCH",
                "side": "PREFETCH",
                "mint": mint,
                "public_key": engine.signer.wallet,
                "metadata": {"source_event_id": event.event_id},
            }
        )
    except Exception as exc:
        # Prefetch is an optimization only. A real trade may still build from
        # the latest event hint or bounded on-demand RPC state.
        LOGGER.debug("E4 builder prefetch failed mint=%s: %s", mint, exc)
    finally:
        _PREFETCH_IN_FLIGHT.discard(mint)


_PREVIOUS_ON_EVENT = core.Engine.on_event


async def _on_event_v7(self: core.Engine, event: core.Event) -> None:
    if (
        event.kind in {core.EventKind.CREATE, core.EventKind.BUY, core.EventKind.CURVE}
        and event.mint not in _PREFETCH_IN_FLIGHT
        and event.mint not in self.positions
        and event.mint not in self.pending_entries
    ):
        _PREFETCH_IN_FLIGHT.add(event.mint)
        self.spawn(_prefetch_mint(self, event))
    await _PREVIOUS_ON_EVENT(self, event)


core.Engine.on_event = _on_event_v7
