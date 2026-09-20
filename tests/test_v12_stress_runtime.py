from __future__ import annotations

import math
import random
from pathlib import Path

from memecoin_bot.v12_capacity import decide_capacity
from memecoin_bot.v12_execution_journal import ExecutionJournal
from memecoin_bot.v12_route_health import RouteHealthStore
from memecoin_bot.v12_safety import CircuitBreaker, SafetyMode, SafetyStore


def test_execution_journal_thousands_of_exactly_once_cycles(tmp_path: Path) -> None:
    path = tmp_path / "stress.db"
    journal = ExecutionJournal(path)
    total = 2_500
    for index in range(total):
        request = {
            "side": "BUY" if index % 2 == 0 else "SELL",
            "mint": f"mint-{index % 137}",
            "amount": 0.01 + (index % 50) / 1000,
        }
        request_id = f"request-{index}"
        first = journal.prepare(request_id, request)
        second = journal.prepare(request_id, request)
        assert first.idempotency_key == second.idempotency_key
        journal.mark_signed(
            first.idempotency_key,
            signed_tx_b64=f"signed-{index}",
            signature=f"signature-{index}",
        )
        journal.mark_submitted(first.idempotency_key)
        journal.mark_confirmed(
            first.idempotency_key,
            route="fast",
            slot=1_000_000 + index,
        )
    assert journal.state_counts() == {"CONFIRMED": total}
    journal.close()

    reopened = ExecutionJournal(path)
    try:
        assert reopened.state_counts() == {"CONFIRMED": total}
        assert reopened.recoverable() == []
        for index in range(0, total, 101):
            row = reopened.by_request(f"request-{index}")
            assert row is not None
            assert row.signature == f"signature-{index}"
            assert row.state == "CONFIRMED"
    finally:
        reopened.close()


def test_capacity_fuzz_never_exceeds_configured_impact_or_reserve() -> None:
    rng = random.Random(12092026)
    for _ in range(5_000):
        virtual_sol = rng.uniform(5.0, 500.0)
        virtual_tokens = rng.uniform(1e6, 1e12)
        requested = rng.uniform(0.001, 50.0)
        max_impact = rng.uniform(100.0, 900.0)
        reserve_fraction = rng.uniform(0.01, 0.12)
        decision = decide_capacity(
            requested_sol=requested,
            virtual_sol=virtual_sol,
            virtual_tokens=virtual_tokens,
            max_price_impact_bps=max_impact,
            max_virtual_sol_fraction=reserve_fraction,
            absolute_cap_sol=20.0,
            minimum_trade_sol=0.01,
        )
        assert math.isfinite(decision.allowed_sol)
        assert 0.0 <= decision.allowed_sol <= min(requested, 20.0) + 1e-9
        if not decision.blocked:
            assert decision.price_impact_bps <= max_impact + 1e-4
            assert decision.reserve_fraction <= reserve_fraction + 1e-8


def test_route_health_survives_long_failure_recovery_cycle(tmp_path: Path) -> None:
    store = RouteHealthStore(tmp_path / "routes.db")
    for _ in range(100):
        store.record("primary", accepted=True, latency_ms=20)
        store.record("secondary", accepted=True, latency_ms=45)
        store.record("bad", accepted=False, latency_ms=900)
    assert store.ranked(["primary", "secondary", "bad"])[0] == "primary"
    assert "bad" not in store.healthy(["primary", "secondary", "bad"])

    # A previously bad route can recover over time; it is not permanently poisoned.
    for _ in range(250):
        store.record("bad", accepted=True, latency_ms=10)
    assert store.get("bad").ewma_success > 0.99
    assert store.get("bad").consecutive_failures == 0


def test_circuit_breaker_state_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "safety.db"
    store = SafetyStore(path)
    breaker = CircuitBreaker(store)
    breaker.evaluate_equity(10.0)
    breaker.evaluate_equity(8.0)
    assert store.snapshot().mode == SafetyMode.HALTED
    store.close()

    reopened = SafetyStore(path)
    try:
        assert reopened.snapshot().mode == SafetyMode.HALTED
        assert reopened.snapshot().entries_allowed is False
    finally:
        reopened.close()
