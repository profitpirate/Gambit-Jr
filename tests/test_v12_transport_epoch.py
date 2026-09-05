from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from aiohttp import web

from memecoin_bot import e4_transport_epoch_v12 as transport


class TransportEpochTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def handler(request: web.Request) -> web.Response:
            if request.method == "POST":
                await request.read()
                return web.json_response(
                    {"jsonrpc": "2.0", "id": 1, "result": "S" * 88}
                )
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def settings(self):
        origin = f"http://127.0.0.1:{self.port}"
        return SimpleNamespace(
            route_urls={"route-a": origin + "/a"},
            direct_rpc_route=False,
            rpc_url=origin + "/rpc",
            route_headers={},
            route_stagger_ms=25,
            confirmation_timeout_seconds=1.0,
        )

    async def test_route_result_uses_epoch_while_telemetry_uses_monotonic(self):
        sender = transport.EpochWarmFanoutRouteSender(
            self.settings(), SimpleNamespace()
        )
        name, url = sender.routes[0]
        before_epoch = time.time_ns()
        result = await sender._send(
            0, name, url, "signed-base64", "S" * 88
        )
        after_epoch = time.time_ns()
        await sender.close()

        self.assertTrue(result.success)
        self.assertGreaterEqual(result.started_ns, before_epoch)
        self.assertLessEqual(result.finished_ns, after_epoch)
        self.assertGreaterEqual(result.finished_ns, result.started_ns)
        self.assertEqual(len(sender.telemetry), 1)
        self.assertGreaterEqual(sender.telemetry[0].elapsed_ms, 0.0)
        # Epoch nanoseconds are currently around 1e18; monotonic process time is
        # deliberately a different clock and must not leak into persistence.
        self.assertGreater(result.started_ns, 1_000_000_000_000_000_000)
        self.assertLess(sender.telemetry[0].started_ns, result.started_ns)


if __name__ == "__main__":
    unittest.main()
