from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from memecoin_bot import e4_sub10ms_transport_v12 as transport


class Sub10msTransportTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            route_urls={"existing": "https://existing.invalid"},
            direct_rpc_route=True,
            rpc_url="https://rpc.invalid",
            route_headers={},
            route_stagger_ms=25,
            confirmation_timeout_seconds=1.0,
        )

    def test_optional_low_latency_routes_are_prepended(self):
        with patch.dict(
            os.environ,
            {
                "E4_ALLENHARK_RELAY_URL": "https://relay.invalid/v1/sendTx",
                "E4_JITO_SEND_TRANSACTION_URLS": "https://jito-a.invalid/api/v1/transactions,https://jito-b.invalid/api/v1/transactions",
                "E4_RPC_FANOUT_URLS": "https://rpc-a.invalid,https://rpc-b.invalid",
            },
            clear=False,
        ):
            sender = transport.Sub10msPersistentSender(
                self.settings(),
                SimpleNamespace(),
            )
        names = [name for name, _ in sender.routes]
        self.assertIn("allenhark_relay", names)
        self.assertIn("jito_1", names)
        self.assertIn("jito_2", names)
        self.assertIn("rpc_fanout_1", names)
        self.assertIn("rpc_fanout_2", names)
        self.assertLess(names.index("allenhark_relay"), names.index("existing"))

    def test_route_payloads_preserve_same_signed_transaction(self):
        sender = transport.Sub10msPersistentSender(
            self.settings(),
            SimpleNamespace(),
        )
        wire = "same-base64-transaction"
        rpc = sender._payload("rpc_fanout_1", wire)
        jito = sender._payload("jito_1", wire)
        relay = sender._payload("allenhark_relay", wire)
        self.assertEqual(rpc["params"][0], wire)
        self.assertEqual(jito["params"][0], wire)
        self.assertEqual(relay["tx"], wire)
        self.assertFalse(relay["simulate"])

    def test_sender_has_no_stagger_sleep_in_send_path(self):
        import inspect

        source = inspect.getsource(transport.Sub10msPersistentSender._send)
        self.assertNotIn("sleep(", source)
        self.assertIn("del index", source)

    def test_transport_is_authoritative_route_sender(self):
        self.assertIs(transport.core.RouteSender, transport.Sub10msPersistentSender)
        self.assertIs(
            transport.repairs.FastPersistentRouteSender,
            transport.Sub10msPersistentSender,
        )


if __name__ == "__main__":
    unittest.main()
