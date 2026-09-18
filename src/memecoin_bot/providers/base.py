from __future__ import annotations

import asyncio
import json
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    pass


class CircuitOpen(ProviderError):
    pass


def sanitize_provider_error(error: object) -> str:
    """Remove common query/path/header credential shapes before persistence."""
    value = str(error)
    value = re.sub(
        r"(?i)((?:api[-_]?key|token|secret|password)=)[^&\s'\"]+",
        r"\1<redacted>",
        value,
    )
    value = re.sub(
        r"(?i)(x-[a-z0-9-]*(?:key|token)[=: ]+)[^&\s'\"]+",
        r"\1<redacted>",
        value,
    )
    value = re.sub(
        r"(https?://[^/\s]+/(?:v1|v2|v3)/)[^/?&\s]+",
        r"\1<redacted>",
        value,
    )
    return value


@dataclass(slots=True)
class HealthState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None


class ResilientJsonClient:
    def __init__(
        self,
        name: str,
        timeout: float = 10,
        retries: int = 2,
        circuit_failures: int = 4,
        circuit_cooldown: float = 60,
        health_callback: Callable[[str, bool, int, str | None], None] | None = None,
    ):
        self.name = name
        self.timeout = timeout
        self.retries = retries
        self.circuit_failures = circuit_failures
        self.circuit_cooldown = circuit_cooldown
        self.health = HealthState()
        self.health_callback = health_callback

    def _check_circuit(self) -> None:
        if self.health.opened_at is None:
            return
        if time.monotonic() - self.health.opened_at >= self.circuit_cooldown:
            self.health.opened_at = None
            return
        if self.health_callback:
            self.health_callback(
                self.name,
                False,
                self.health.consecutive_failures,
                f"{self.name} circuit is open",
            )
        raise CircuitOpen(f"{self.name} circuit is open")

    async def request(
        self,
        url: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self._check_circuit()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                data = None if payload is None else json.dumps(payload).encode()
                request_headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "solana-memecoin-intelligence/1.0",
                }
                request_headers.update(headers or {})

                def perform(
                    data: bytes | None = data, request_headers: dict[str, str] = request_headers
                ) -> Any:
                    request = urllib.request.Request(
                        url, data=data, headers=request_headers, method=method
                    )
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        return json.loads(response.read().decode("utf-8"))

                result = await asyncio.wait_for(asyncio.to_thread(perform), self.timeout + 1)
                self.health = HealthState()
                if self.health_callback:
                    self.health_callback(self.name, True, 0, None)
                return result
            except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
                last = exc
                safe_error = sanitize_provider_error(exc)
                self.health.consecutive_failures += 1
                self.health.last_error = safe_error
                if self.health.consecutive_failures >= self.circuit_failures:
                    self.health.opened_at = time.monotonic()
                if self.health_callback:
                    self.health_callback(
                        self.name, False, self.health.consecutive_failures, safe_error
                    )
                if attempt < self.retries:
                    await asyncio.sleep(min(0.5 * (2**attempt) + random.random() * 0.2, 5))
        safe_last = sanitize_provider_error(last) if last is not None else "unknown provider error"
        raise ProviderError(f"{self.name} request failed: {safe_last}") from None
