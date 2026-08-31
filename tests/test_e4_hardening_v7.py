from __future__ import annotations

import json
import unittest

from memecoin_bot import e4_hardening_v7


class BuilderContextTests(unittest.TestCase):
    def setUp(self) -> None:
        e4_hardening_v7._BUILD_CONTEXT_BY_MINT.clear()
        e4_hardening_v7._PREFETCH_IN_FLIGHT.clear()

    def tearDown(self) -> None:
        e4_hardening_v7._BUILD_CONTEXT_BY_MINT.clear()
        e4_hardening_v7._PREFETCH_IN_FLIGHT.clear()

    def test_nested_canonical_payload_becomes_exact_builder_hint(self) -> None:
        payload = {
            "token_program": "TokenzQdYf...",
            "virtual_token_reserves": "1073000000000000",
            "virtual_sol_reserves": "30000000000",
            "real_token_reserves": "793100000000000",
            "real_sol_reserves": "0",
            "token_total_supply": "1000000000000000",
            "creator": "11111111111111111111111111111111",
            "is_mayhem_mode": False,
            "is_cashback_coin": True,
            "complete": False,
        }
        row = {
            "id": 1,
            "event_type": "CREATE",
            "canonical_token": "mint",
            "source_timestamp": "2026-08-31T00:00:00Z",
            "received_timestamp": "2026-08-31T00:00:00Z",
            "payload_json": json.dumps(payload),
        }
        event = e4_hardening_v7.core.Event.from_row(row)
        self.assertEqual(event.mint, "mint")
        context = e4_hardening_v7._BUILD_CONTEXT_BY_MINT["mint"]
        self.assertEqual(context["virtual_sol_reserves"], "30000000000")
        self.assertEqual(context["token_total_supply"], "1000000000000000")
        self.assertTrue(context["cashback"])
        self.assertFalse(context["complete"])

    def test_build_request_is_enriched_without_float_coercion(self) -> None:
        e4_hardening_v7._BUILD_CONTEXT_BY_MINT["mint"] = {
            "virtual_token_reserves": "1073000000000000",
            "virtual_sol_reserves": "30000000000",
            "real_token_reserves": "793100000000000",
            "real_sol_reserves": "0",
            "token_total_supply": "1000000000000000",
            "creator": "11111111111111111111111111111111",
            "token_program": "token2022",
            "updated_ns": 123,
        }
        request = e4_hardening_v7._enrich_request(
            {
                "request_id": "request",
                "side": "BUY",
                "mint": "mint",
                "public_key": "11111111111111111111111111111111",
                "metadata": {"score": 0.9},
            }
        )
        hint = request["metadata"]["state_hint"]
        self.assertEqual(hint["virtual_token_reserves"], "1073000000000000")
        self.assertNotIn("updated_ns", hint)
        self.assertEqual(request["metadata"]["score"], 0.9)


if __name__ == "__main__":
    unittest.main()
