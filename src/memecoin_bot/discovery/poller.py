from __future__ import annotations

from memecoin_bot.models import DiscoveryEvent
from memecoin_bot.providers.base import ProviderError


class DiscoveryPoller:
    def __init__(self, providers: object | list[object]):
        self.providers = providers if isinstance(providers, list) else [providers]

    async def poll(self) -> list[DiscoveryEvent]:
        events: dict[tuple[str, str], DiscoveryEvent] = {}
        failures = 0
        for provider in self.providers:
            try:
                discovered = await provider.discover()
            except ProviderError:
                failures += 1
                continue
            for event in discovered:
                key = (event.chain, event.token_address)
                if key not in events:
                    events[key] = event
                else:
                    existing = events[key]
                    sources = set(existing.metadata.get("additional_sources") or [])
                    sources.add(event.source)
                    existing.metadata["additional_sources"] = sorted(sources)
        if failures == len(self.providers) and failures:
            raise ProviderError("all discovery providers unavailable")
        return list(events.values())
