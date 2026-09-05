from __future__ import annotations

import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_sub10ms_transport_final_v12 as transport


class FinalSub10msTransportTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            route_urls={"existing": "https://existing.invalid"},
            direct_rpc_route=True,
            rpc_url="https://rpc.invalid",
            route_headers={},
            route_stagger_ms=25,
            confirmation_timeout_seconds=1.0,
        )

    def test_session_api_remains_callable(self):
        sender = transport.FinalPersistentRouteSender(
            self.settings(),
            SimpleNamespace(),
        )
        self.assertTrue(callable(sender._session))
        self.assertIsNone(sender._final_http_session)

    def test_low_latency_routes_are_optional_and_prepended(self):
        with patch.dict(
            os.environ,
            {
                "E4_ALLENHARK_RELAY_URL": "https://relay.invalid/v1/sendTx",
                "E4_JITO_SEND_TRANSACTION_URLS": "https://jito-a.invalid/api/v1/transactions,https://jito-b.invalid/api/v1/transactions",
                "E4_RPC_FANOUT_URLS": "https://rpc-a.invalid,https://rpc-b.invalid",
            },
            clear=False,
        ):
            sender = transport.FinalPersistentRouteSender(
                self.settings(),
                SimpleNamespace(),
            )
        names = [name for name, _ in sender.routes]
        for expected in (
            "allenhark_relay", "jito_1", "jito_2",
            "rpc_fanout_1", "rpc_fanout_2",
        ):
            self.assertIn(expected, names)
        self.assertLess(names.index("allenhark_relay"), names.index("existing"))

    def test_all_routes_receive_identical_signed_wire(self):
        sender = transport.FinalPersistentRouteSender(
            self.settings(),
            SimpleNamespace(),
        )
        wire = "one-signed-base64-wire"
        self.assertEqual(sender._payload("rpc_fanout_1", wire)["params"][0], wire)
        self.assertEqual(sender._payload("jito_1", wire)["params"][0], wire)
        self.assertEqual(sender._payload("allenhark_relay", wire)["tx"], wire)

    def test_send_path_cannot_stagger(self):
        source = inspect.getsource(transport.FinalPersistentRouteSender._send)
        self.assertIn("del index", source)
        self.assertNotIn("sleep(", source)

    def test_final_sender_is_route_authority(self):
        self.assertIs(transport.core.RouteSender, transport.FinalPersistentRouteSender)
        self.assertIs(
            transport.repairs.FastPersistentRouteSender,
            transport.FinalPersistentRouteSender,
        )


if __name__ == "__main__":
    unittest.main()
