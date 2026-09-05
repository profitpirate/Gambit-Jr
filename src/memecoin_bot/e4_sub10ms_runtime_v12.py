from __future__ import annotations

import asyncio
import logging

from . import e4_sub10ms_transport_v12 as transport

core = transport.core
LOGGER = logging.getLogger("gambit.e4.sub10ms.runtime.v12")

_PREVIOUS_RUN = core.Engine.run


async def _run_with_final_transport_prewarm(self):
    sender = getattr(self, "sender", None)
    warm = getattr(sender, "warm", None)
    if callable(warm):
        await warm()

    builder = getattr(self, "builder", None)
    workers = getattr(builder, "workers", None)
    for worker in tuple(workers or ()):
        start = getattr(worker, "start", None)
        if callable(start):
            value = start()
            if asyncio.iscoroutine(value):
                await value

    try:
        return await _PREVIOUS_RUN(self)
    finally:
        close = getattr(sender, "close", None)
        if callable(close):
            try:
                value = close()
                if asyncio.iscoroutine(value):
                    await value
            except Exception:
                LOGGER.exception("V12 final transport cleanup failed")


core.Engine.run = _run_with_final_transport_prewarm
