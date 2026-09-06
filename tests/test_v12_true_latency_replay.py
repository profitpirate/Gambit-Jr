from __future__ import annotations

import unittest

from scripts import e4_v12_true_latency_replay as replay


class TrueLatencyReplayTests(unittest.TestCase):
    @staticmethod
    def _row(
        *,
        mint: str,
        received_ns: int,
        sequence: int,
        kind: str,
        virtual_sol: float,
        virtual_tokens: float,
        real_tokens: float,
        trader: str = "other",
    ) -> dict[str, object]:
        return {
            "received_ns": received_ns,
            "slot": 1,
            "event_index": sequence,
            "__sequence": sequence,
            "kind": kind,
            "mint": mint,
            "trader": trader,
            "signature": f"sig-{sequence}",
            "raw": {
                "virtual_sol_reserves": virtual_sol,
                "virtual_token_reserves": virtual_tokens,
                "real_token_reserves": real_tokens,
            },
        }

    @classmethod
    def _run(cls, *, mint: str, decision_ns: int) -> replay.RunData:
        rows = [
            cls._row(
                mint=mint,
                received_ns=decision_ns,
                sequence=0,
                kind="CREATE",
                virtual_sol=30.0,
                virtual_tokens=1_000_000.0,
                real_tokens=800_000.0,
                trader="creator",
            ),
            cls._row(
                mint=mint,
                received_ns=decision_ns + 5_000_000,
                sequence=1,
                kind="BUY",
                virtual_sol=31.0,
                virtual_tokens=967_741.935483871,
                real_tokens=767_741.935483871,
            ),
            cls._row(
                mint=mint,
                received_ns=decision_ns + 10_000_000,
                sequence=2,
                kind="BUY",
                virtual_sol=40.0,
                virtual_tokens=750_000.0,
                real_tokens=550_000.0,
                trader="other-2",
            ),
            cls._row(
                mint=mint,
                received_ns=decision_ns + 20_000_000,
                sequence=3,
                kind="SELL",
                virtual_sol=30.5,
                virtual_tokens=983_606.5573770492,
                real_tokens=783_606.5573770492,
            ),
        ]
        states = [
            replay.ReserveState(
                received_ns=int(row["received_ns"]),
                sequence=int(row["__sequence"]),
                virtual_sol=float(row["raw"]["virtual_sol_reserves"]),
                virtual_tokens=float(row["raw"]["virtual_token_reserves"]),
                real_tokens=float(row["raw"]["real_token_reserves"]),
                price_sol=0.0,
                fdv_usd=4_000.0,
            )
            for row in rows
        ]
        return replay.RunData(
            run_id="synthetic-run",
            batch={},
            events_by_mint={mint: rows},
            reserves_by_mint={mint: states},
            e4_positions={},
        )

    @staticmethod
    def _prediction(mint: str, decision_ns: int) -> dict[str, object]:
        return {
            "run_id": "synthetic-run",
            "mint": mint,
            "decision_ns": decision_ns,
            "decision_sequence": 0,
            "score": 0.96,
            "entry_fraction": 0.0185,
            "family": "test",
        }

    def test_received_time_is_primary_event_order(self) -> None:
        earlier = {"received_ns": 100, "slot": 9, "event_index": 99, "raw": {}}
        later = {"received_ns": 200, "slot": 1, "event_index": 0, "raw": {}}
        self.assertLess(replay.event_sort_key(earlier), replay.event_sort_key(later))

    def test_constant_product_buy_and_sell_are_inverse_without_external_flow(self) -> None:
        state = replay.ReserveState(
            received_ns=1,
            sequence=0,
            virtual_sol=30.0,
            virtual_tokens=1_000_000.0,
            real_tokens=800_000.0,
            price_sol=0.0,
            fdv_usd=4_000.0,
        )
        tokens = replay.buy_tokens(1.0, state)
        post = replay.ReserveState(
            received_ns=2,
            sequence=1,
            virtual_sol=state.virtual_sol + 1.0,
            virtual_tokens=state.virtual_tokens - tokens,
            real_tokens=state.real_tokens - tokens,
            price_sol=0.0,
            fdv_usd=4_000.0,
        )
        recovered = replay.sell_sol(tokens, post)
        self.assertAlmostEqual(recovered, 1.0, places=9)

    def test_fill_latency_changes_quote_and_triggers_strict_guard(self) -> None:
        decision_ns = 1_000_000_000
        mint = "mint-a"
        run = self._run(mint=mint, decision_ns=decision_ns)
        prediction = self._prediction(mint, decision_ns)

        fast, fast_rejection = replay.simulate_one(
            run,
            prediction,
            liquid_sol=3.0,
            latency_ms=0.0,
            output_shortfall_bps=800,
            entry_fraction=0.0185,
            maximum_position_sol=0.30,
            reserve_sol=0.03,
            pump_fee_bps=125,
            confirmation_ms=1_500.0,
            unconfirmed_timeout_ms=20.0,
        )
        slow, slow_rejection = replay.simulate_one(
            run,
            prediction,
            liquid_sol=3.0,
            latency_ms=10.0,
            output_shortfall_bps=800,
            entry_fraction=0.0185,
            maximum_position_sol=0.30,
            reserve_sol=0.03,
            pump_fee_bps=125,
            confirmation_ms=1_500.0,
            unconfirmed_timeout_ms=20.0,
        )

        self.assertIsNotNone(fast)
        self.assertIsNone(fast_rejection)
        self.assertIsNone(slow)
        self.assertIsNotNone(slow_rejection)
        self.assertTrue(slow_rejection.reason.startswith("BuyExactSolIn"))
        self.assertGreater(slow_rejection.output_shortfall_bps or 0.0, 800.0)

    def test_each_latency_is_replayed_independently(self) -> None:
        decision_ns = 2_000_000_000
        mint = "mint-b"
        run = self._run(mint=mint, decision_ns=decision_ns)
        runs = {run.run_id: run}
        prediction = self._prediction(mint, decision_ns)

        result_zero = replay.portfolio(
            runs,
            [prediction],
            latency_ms=0.0,
            output_shortfall_bps=2_500,
            starting_balance_sol=3.0,
            entry_fraction=0.0185,
            maximum_position_sol=0.30,
            reserve_sol=0.03,
            pump_fee_bps=125,
            confirmation_ms=1_500.0,
            unconfirmed_timeout_ms=20.0,
            max_concurrency=2,
        )
        result_five = replay.portfolio(
            runs,
            [prediction],
            latency_ms=5.0,
            output_shortfall_bps=2_500,
            starting_balance_sol=3.0,
            entry_fraction=0.0185,
            maximum_position_sol=0.30,
            reserve_sol=0.03,
            pump_fee_bps=125,
            confirmation_ms=1_500.0,
            unconfirmed_timeout_ms=20.0,
            max_concurrency=2,
        )

        zero_position = result_zero["positions"][0]
        five_position = result_five["positions"][0]
        self.assertNotEqual(zero_position["received_tokens"], five_position["received_tokens"])
        self.assertNotEqual(zero_position["pnl_sol"], five_position["pnl_sol"])


if __name__ == "__main__":
    unittest.main()
