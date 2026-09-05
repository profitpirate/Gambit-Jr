from __future__ import annotations

import json
import time
from typing import Any, Mapping

from . import e4_transport_v12 as base

core = base.core


class EpochWarmFanoutRouteSender(base.WarmFanoutRouteSender):
    """Use monotonic time for latency and epoch time for persisted receipts."""

    async def _send(
        self,
        index: int,
        name: str,
        url: str,
        tx: str,
        expected_signature: str,
    ):
        del index
        started_perf_ns = time.perf_counter_ns()
        started_epoch_ns = time.time_ns()
        base_name = str(name).split("#", 1)[0]
        try:
            session = self._session()
            if base_name == "allenhark_relay":
                payload: Mapping[str, Any] = {"tx": tx, "simulate": False}
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        tx,
                        {
                            "encoding": "base64",
                            "skipPreflight": True,
                            "maxRetries": 0,
                            "preflightCommitment": "processed",
                        },
                    ],
                }
            async with session.post(
                url,
                json=payload,
                headers=self._headers_for(name),
            ) as response:
                text = await response.text()
                response_perf_ns = time.perf_counter_ns()
                response_epoch_ns = time.time_ns()
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {text[:300]}")
                body = json.loads(text) if text else {}
                if isinstance(body, Mapping) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                if base_name == "allenhark_relay":
                    state = (
                        str((body or {}).get("status") or "accepted").lower()
                        if isinstance(body, Mapping)
                        else "accepted"
                    )
                    if state in {"rejected", "error", "failed"}:
                        raise RuntimeError(
                            str((body or {}).get("error") or state)
                        )
                    returned = (
                        str(
                            (body or {}).get("signature")
                            or (body or {}).get("result")
                            or expected_signature
                        )
                        if isinstance(body, Mapping)
                        else expected_signature
                    )
                else:
                    returned = (
                        str((body or {}).get("result") or expected_signature)
                        if isinstance(body, Mapping)
                        else expected_signature
                    )
                if (
                    returned
                    and expected_signature
                    and returned != expected_signature
                    and len(returned) >= 64
                ):
                    raise RuntimeError(
                        "route signature mismatch "
                        f"expected={expected_signature} got={returned}"
                    )
                self._record(
                    base.DispatchTelemetry(
                        str(name),
                        started_perf_ns,
                        response_perf_ns,
                        True,
                        "accepted",
                    )
                )
                return core.RouteResult(
                    name,
                    started_epoch_ns,
                    response_epoch_ns,
                    True,
                    returned or expected_signature,
                )
        except Exception as exc:
            response_perf_ns = time.perf_counter_ns()
            response_epoch_ns = time.time_ns()
            self._record(
                base.DispatchTelemetry(
                    str(name),
                    started_perf_ns,
                    response_perf_ns,
                    False,
                    str(exc),
                )
            )
            return core.RouteResult(
                name,
                started_epoch_ns,
                response_epoch_ns,
                False,
                "rejected",
                str(exc),
            )


core.RouteSender = EpochWarmFanoutRouteSender
