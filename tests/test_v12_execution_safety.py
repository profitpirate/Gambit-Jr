from __future__ import annotations

from pathlib import Path

import pytest

from memecoin_bot.v12_capacity import decide_capacity
from memecoin_bot.v12_execution_journal import ExecutionJournal
from memecoin_bot.v12_route_health import RouteHealthStore
from memecoin_bot.v12_safety import CircuitBreaker, SafetyMode, SafetyStore


def test_execution_journal_is_exactly_once(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "e4.db")
    request = {"side": "BUY", "mint": "m", "amount": 1.0}
    first = journal.prepare("request-1", request)
    second = journal.prepare("request-1", request)
    assert first.idempotency_key == second.idempotency_key

    journal.mark_signed(
        first.idempotency_key,
        signed_tx_b64="signed",
        signature="sig",
    )
    journal.mark_submitted(first.idempotency_key)
    journal.mark_confirmed(first.idempotency_key, route="fast", slot=123)
    final = journal.get(first.idempotency_key)
    assert final is not None
    assert final.state == "CONFIRMED"
    assert final.signature == "sig"

    with pytest.raises(RuntimeError, match="different payload"):
        journal.prepare("request-1", {"side": "BUY", "mint": "m", "amount": 2.0})


def test_capacity_clamps_large_trade_without_changing_selection() -> None:
    result = decide_capacity(
        requested_sol=10.0,
        virtual_sol=30.0,
        virtual_tokens=1_000_000_000.0,
        max_price_impact_bps=600.0,
        max_virtual_sol_fraction=0.08,
        absolute_cap_sol=20.0,
    )
    assert result.blocked is False
    assert 0 < result.allowed_sol < 10.0
    assert result.price_impact_bps <= 600.0001
    assert result.reserve_fraction <= 0.080001


def test_circuit_breaker_halts_on_drawdown(tmp_path: Path) -> None:
    store = SafetyStore(tmp_path / "e4.db")
    breaker = CircuitBreaker(store)
    breaker.evaluate_equity(10.0)
    snapshot = breaker.evaluate_equity(8.4)
    assert snapshot.mode == SafetyMode.HALTED
    assert snapshot.entries_allowed is False


def test_route_health_demotes_repeated_failures(tmp_path: Path) -> None:
    store = RouteHealthStore(tmp_path / "e4.db")
    for _ in range(6):
        store.record("bad", accepted=False, latency_ms=1000)
    for _ in range(3):
        store.record("good", accepted=True, latency_ms=20)
    assert store.ranked(["bad", "good"])[0] == "good"
    assert store.healthy(["bad", "good"]) == ["good"]
