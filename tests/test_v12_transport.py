from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace

from aiohttp import web

from memecoin_bot import e4_transport_v12 as transport


class TransportV12Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []

        async def handler(request: web.Request) -> web.Response:
            if request.method == "POST":
                payload = await request.json()
                self.received.append((request.path, time.perf_counter_ns(), payload))
                params = payload.get("params") or []
                signature = "S" * 88
                return web.json_response({"jsonrpc": "2.0", "id": 1, "result": signature})
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets
        self.port = sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def settings(self):
        origin = f"http://127.0.0.1:{self.port}"
        return SimpleNamespace(
            route_urls={"route-a": origin + "/a", "route-b": origin + "/b"},
            direct_rpc_route=False,
            rpc_url=origin + "/rpc",
            route_headers={},
            route_stagger_ms=25,
            confirmation_timeout_seconds=1.0,
        )

    async def test_two_routes_receive_same_transaction_without_stagger(self):
        sender = transport.WarmFanoutRouteSender(self.settings(), SimpleNamespace())
        await sender.warm()
        expected = "S" * 88
        routes = list(sender.routes)
        results = await asyncio.gather(
            *(
                sender._send(index, name, url, "signed-base64", expected)
                for index, (name, url) in enumerate(routes)
            )
        )
        await sender.close()
        self.assertGreaterEqual(len(results), 2)
        self.assertTrue(all(result.success for result in results))
        posts = [row for row in self.received if row[2].get("method") == "sendTransaction"]
        self.assertEqual(len(posts), len(routes))
        self.assertTrue(all((row[2].get("params") or [None])[0] == "signed-base64" for row in posts))
        timestamps = [row[1] for row in posts]
        self.assertLess((max(timestamps) - min(timestamps)) / 1_000_000.0, 25.0)

    async def test_telemetry_records_route_response(self):
        sender = transport.WarmFanoutRouteSender(self.settings(), SimpleNamespace())
        name, url = sender.routes[0]
        result = await sender._send(99, name, url, "signed-base64", "S" * 88)
        await sender.close()
        self.assertTrue(result.success)
        self.assertEqual(len(sender.telemetry), 1)
        self.assertTrue(sender.telemetry[0].success)
        self.assertGreaterEqual(sender.telemetry[0].elapsed_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
