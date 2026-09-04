from __future__ import annotations

import struct
import time
import unittest
from types import MappingProxyType, SimpleNamespace

from memecoin_bot import e4_preconfirm_v12 as preconfirm


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, rem = divmod(number, 58)
        encoded = _ALPHABET[rem] + encoded
    return "1" * zeros + (encoded or ("" if zeros else "1"))


def account_keys(size: int, replacements: dict[int, str]) -> list[str]:
    keys = [f"account-{index}" for index in range(size)]
    for index, value in replacements.items():
        keys[index] = value
    return keys


def legacy_tx(discriminator: bytes, *, spend: int, second: int, user: str) -> dict:
    keys = account_keys(
        9,
        {
            2: "Mint111111111111111111111111111111111111111",
            6: user,
            8: preconfirm.PUMP_PROGRAM,
        },
    )
    data = discriminator + struct.pack("<QQ", spend, second)
    return {
        "message": {
            "accountKeys": keys,
            "instructions": [
                {
                    "programId": preconfirm.PUMP_PROGRAM,
                    "accounts": [keys[index] for index in (0, 1, 2, 3, 4, 5, 6)],
                    "data": b58encode(data),
                }
            ],
        }
    }


class V12PreconfirmDecodeTests(unittest.TestCase):
    def test_exact_sol_in_is_hard_authority_with_encoded_spend(self):
        tx = legacy_tx(
            preconfirm.BUY_EXACT_SOL_IN,
            spend=3_000_000_000,
            second=80_000_000_000,
            user=preconfirm.role_model.E4_WALLET,
        )
        rows = preconfirm.decode_e4_buy_intents("sig-exact", tx, received_ns=123)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.instruction_family, "buy_exact_sol_in")
        self.assertTrue(row.exact_spend)
        self.assertAlmostEqual(row.spend_sol, 3.0)
        self.assertEqual(row.mint, "Mint111111111111111111111111111111111111111")

    def test_max_cost_buy_is_observational_only(self):
        tx = legacy_tx(
            preconfirm.BUY,
            spend=50_000_000_000,
            second=4_000_000_000,
            user=preconfirm.role_model.E4_WALLET,
        )
        row = preconfirm.decode_e4_buy_intents("sig-max", tx)[0]
        self.assertFalse(row.exact_spend)
        self.assertEqual(row.spend_sol, 0.0)
        self.assertAlmostEqual(row.spend_ceiling_sol, 4.0)

    def test_non_e4_signer_is_not_decoded(self):
        tx = legacy_tx(
            preconfirm.BUY_EXACT_SOL_IN,
            spend=1_000_000_000,
            second=1,
            user="NotE4Wallet11111111111111111111111111111111",
        )
        self.assertEqual(preconfirm.decode_e4_buy_intents("sig-nope", tx), [])

    def test_v2_exact_quote_in_requires_native_sol_and_e4_user(self):
        keys = account_keys(
            16,
            {
                1: "MintV211111111111111111111111111111111111111",
                2: preconfirm.NATIVE_MINT,
                13: preconfirm.role_model.E4_WALLET,
                15: preconfirm.PUMP_PROGRAM,
            },
        )
        data = preconfirm.BUY_EXACT_QUOTE_IN_V2 + struct.pack("<QQ", 2_000_000_000, 42_000_000)
        tx = {
            "message": {
                "accountKeys": keys,
                "instructions": [
                    {
                        "programId": preconfirm.PUMP_PROGRAM,
                        "accounts": [keys[index] for index in range(14)],
                        "data": b58encode(data),
                    }
                ],
            }
        }
        row = preconfirm.decode_e4_buy_intents("sig-v2", tx)[0]
        self.assertEqual(row.instruction_family, "buy_exact_quote_in_v2")
        self.assertTrue(row.exact_spend)
        self.assertAlmostEqual(row.spend_sol, 2.0)
        self.assertEqual(row.mint, keys[1])


class V12PreconfirmDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_entries = preconfirm.PIPELINES._e4_entries
        self.old_profiles = dict(preconfirm.v6._PROFILE_BY_MINT)
        preconfirm.PIPELINES._e4_entries = MappingProxyType({})
        preconfirm.v6._PROFILE_BY_MINT.clear()

    async def asyncTearDown(self):
        preconfirm.PIPELINES._e4_entries = self.old_entries
        preconfirm.v6._PROFILE_BY_MINT.clear()
        preconfirm.v6._PROFILE_BY_MINT.update(self.old_profiles)

    async def test_preconfirm_dispatch_executes_without_waiting_for_next_market_event(self):
        mint = "MintDispatch1111111111111111111111111111111111"
        state = SimpleNamespace(mint=mint, created_ns=100, latest_ns=200)
        decisions = []
        executed = []

        class Store:
            def has_entered(self, _mint):
                return False

            def decision(self, *args, **kwargs):
                decisions.append((args, kwargs))

        class Policy:
            def entry(self, _state):
                preconfirm.v6._PROFILE_BY_MINT[mint] = SimpleNamespace(
                    family=preconfirm.direct.DIRECT_COPY_FAMILY
                )
                return True, 0.97, 0.03, "direct preconfirm", {"test": 1.0}

        class Engine:
            def __init__(self):
                self.tokens = {mint: state}
                self.pending_entries = set()
                self.positions = {}
                self.store = Store()
                self.policy = Policy()

            async def execute_buy(self, got_state, score, fraction, reason):
                executed.append((got_state, score, fraction, reason))
                self.pending_entries.discard(got_state.mint)

        intent = preconfirm.E4InstructionIntent(
            mint=mint,
            signature="sig-dispatch",
            received_ns=time.time_ns(),
            instruction_family="buy_exact_sol_in",
            spend_sol=3.0,
            exact_spend=True,
            token_target=0.0,
            spend_ceiling_sol=3.0,
        )
        dispatcher = preconfirm.PreconfirmDispatcher()
        await dispatcher._execute(Engine(), intent)
        self.assertEqual(dispatcher.executed, 1)
        self.assertEqual(len(executed), 1)
        self.assertEqual(len(decisions), 1)
        source = preconfirm.PIPELINES.e4_signal(mint)
        self.assertIsNotNone(source)
        self.assertAlmostEqual(source.entry_sol, 3.0)


if __name__ == "__main__":
    unittest.main()
