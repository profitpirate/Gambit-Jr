from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_execution_journal import ExecutionJournal
from memecoin_bot.v12_recovery import reconcile_journal
from memecoin_bot.v12_safety import CircuitBreaker, SafetyMode, SafetyStore


class FakeRpc:
    def __init__(self, status):
        self.status = status

    async def call(self, method, params):
        assert method == "getSignatureStatuses"
        del params
        if self.status == "error":
            raise RuntimeError("rpc unavailable")
        if self.status == "missing":
            return {"value": [None]}
        if self.status == "failed":
            return {"value": [{"err": {"x": 1}, "slot": 5}]}
        return {
            "value": [
                {
                    "err": None,
                    "slot": 7,
                    "confirmationStatus": "confirmed",
                }
            ]
        }


class FakeSender:
    def __init__(self):
        self.calls = []

    async def submit(self, signed, signature):
        self.calls.append((signed, signature))
        return "fast", True, 99, None, []


@pytest.mark.asyncio
async def test_recovery_resubmits_exact_same_signed_transaction(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "e4.db")
    entry = journal.prepare(
        "r1",
        {"side": "BUY", "mint": "m", "amount": 1.0},
    )
    journal.mark_signed(
        entry.idempotency_key,
        signed_tx_b64="EXACT-SIGNED-TX",
        signature="EXACT-SIGNATURE",
    )
    journal.mark_submitted(entry.idempotency_key)

    safety = SafetyStore(journal.conn)
    breaker = CircuitBreaker(safety)
    engine = SimpleNamespace(rpc=FakeRpc("missing"), sender=FakeSender())

    result = await reconcile_journal(engine, journal, breaker)
    assert result == (1, 1, 1, 0)
    assert engine.sender.calls == [("EXACT-SIGNED-TX", "EXACT-SIGNATURE")]
    final = journal.get(entry.idempotency_key)
    assert final is not None and final.state == "CONFIRMED"


@pytest.mark.asyncio
async def test_recovery_rpc_failure_forces_exit_only(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "e4.db")
    entry = journal.prepare("r1", {"side": "SELL", "mint": "m", "amount": 1})
    journal.mark_signed(entry.idempotency_key, signed_tx_b64="tx", signature="sig")
    journal.mark_submitted(entry.idempotency_key)
    safety = SafetyStore(journal.conn)
    breaker = CircuitBreaker(safety)
    engine = SimpleNamespace(rpc=FakeRpc("error"), sender=FakeSender())

    checked, confirmed, retried, uncertain = await reconcile_journal(
        engine, journal, breaker
    )
    assert (checked, confirmed, retried, uncertain) == (1, 0, 0, 1)
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY


class NeverLandingSender:
    def __init__(self):
        self.calls = []

    async def submit(self, signed, signature):
        self.calls.append((signed, signature))
        return "fast", False, None, "confirmation timeout", []


@pytest.mark.asyncio
async def test_uncertain_recovery_consumes_retry_budget_once(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "e4.db")
    entry = journal.prepare("r-uncertain", {"side": "SELL", "mint": "m", "amount": 1})
    journal.mark_signed(entry.idempotency_key, signed_tx_b64="tx", signature="sig")
    journal.mark_submitted(entry.idempotency_key)
    journal.mark_failed(entry.idempotency_key, "initial timeout", terminal=False)

    safety = SafetyStore(journal.conn)
    breaker = CircuitBreaker(safety)
    sender = NeverLandingSender()
    engine = SimpleNamespace(rpc=FakeRpc("missing"), sender=sender)

    first = await reconcile_journal(
        engine,
        journal,
        breaker,
        states=("UNCERTAIN",),
    )
    after_first = journal.get(entry.idempotency_key)
    assert first == (1, 0, 1, 1)
    assert after_first is not None
    assert after_first.state == "UNCERTAIN"
    assert after_first.attempts == 2
    assert sender.calls == [("tx", "sig")]

    second = await reconcile_journal(
        engine,
        journal,
        breaker,
        states=("UNCERTAIN",),
    )
    after_second = journal.get(entry.idempotency_key)
    assert second == (1, 0, 0, 1)
    assert after_second is not None
    assert after_second.attempts == 2
    assert sender.calls == [("tx", "sig")]
    assert safety.snapshot().mode == SafetyMode.EXIT_ONLY
