"""Share only concurrent reads of the exact URI; never cache mutable documents."""
import asyncio
import copy

from .v12_prearmed_diagnostics import DiagnosticError


class MetadataRequests:
    def __init__(self):
        self.active = {}

    async def fetch(self, getter, session, uri, *, trace=None, timeout_seconds=3):
        # Invalid inputs still receive the underlying fetcher's normal validation.
        if not isinstance(uri, str):
            return await getter(session, uri, trace=trace, timeout_seconds=timeout_seconds)
        entry = self.active.get(uri)
        shared = entry is not None and not entry['task'].done()
        if not shared:
            entry = {'subscribers': [], 'events': []}
            def report(**details):
                entry['events'].append(details)
                del entry['events'][:-32]
                for subscriber in tuple(entry['subscribers']):
                    if subscriber is not None:
                        subscriber(**details)
            entry['task'] = asyncio.create_task(getter(session, uri, trace=report, timeout_seconds=timeout_seconds))
            self.active[uri] = entry
        subscriber = (lambda **details: trace(**{**details, 'metadata_shared_request': True})) if shared and trace is not None else trace
        entry['subscribers'].append(subscriber)
        try:
            if shared and trace is not None:
                for details in entry['events']:
                    subscriber(**details)
            async with asyncio.timeout(max(0, min(3, timeout_seconds))):
                document, observed_ns = await asyncio.shield(entry['task'])
            return copy.deepcopy(document), observed_ns
        except TimeoutError:
            raise DiagnosticError('METADATA_TIMEOUT') from None
        finally:
            entry['subscribers'].remove(subscriber)
            if not entry['subscribers']:
                if self.active.get(uri) is entry:
                    del self.active[uri]
                if not entry['task'].done():
                    entry['task'].cancel()
                await asyncio.gather(entry['task'], return_exceptions=True)
