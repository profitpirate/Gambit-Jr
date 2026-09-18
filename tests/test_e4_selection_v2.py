from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memecoin_bot import e4_live as core
from memecoin_bot.e4_selection_v2 import OnlineMemory, UnifiedE4Policy


def _event(
    event_id: int,
    kind: core.EventKind,
    mint: str,
    ns: int,
    *,
    trader: str | None = None,
    sol: float = 0.0,
    fdv: float = 4_800.0,
    creator: str = "creator",
) -> core.Event:
    return core.Event(
        event_id=event_id,
        kind=kind,
        mint=mint,
        source_ns=ns,
        received_ns=ns,
        trader=trader,
        sol_amount=sol,
        fdv_usd=fdv,
        creator=creator,
    )


def _state(
    mint: str,
    creator: str,
    *,
    buys: list[tuple[str, float]] | None = None,
    sells: list[tuple[str, float]] | None = None,
    fdv: float = 4_800.0,
) -> core.TokenState:
    start = 1_800_000_000_000_000_000
    state = core.TokenState(mint)
    state.apply(
        _event(1, core.EventKind.CREATE, mint, start, fdv=fdv, creator=creator),
        "test-wallet",
    )
    event_id = 10
    for wallet, amount in buys or []:
        event_id += 1
        state.apply(
            _event(
                event_id,
                core.EventKind.BUY,
                mint,
                start + event_id * 10_000_000,
                trader=wallet,
                sol=amount,
                fdv=fdv,
                creator=creator,
            ),
            "test-wallet",
        )
    for wallet, amount in sells or []:
        event_id += 1
        state.apply(
            _event(
                event_id,
                core.EventKind.SELL,
                mint,
                start + event_id * 10_000_000,
                trader=wallet,
                sol=amount,
                fdv=fdv,
                creator=creator,
            ),
            "test-wallet",
        )
    return state


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UnifiedE4Policy:
    expectancy = tmp_path / "expectancy.json"
    winners = tmp_path / "winners.json"
    discovered = tmp_path / "discovered.json"
    profile = tmp_path / "profile.json"
    expectancy.write_text(json.dumps({"top_creators": []}))
    winners.write_text(json.dumps({"creators": {}}))
    discovered.write_text(json.dumps({"creators": {}, "watchlist": {}}))
    profile.write_text(
        json.dumps(
            {
                "relative_size_tiers": {
                    "probe": 0.0075,
                    "standard": 0.0125,
                    "strong": 0.0185,
                    "high": 0.03,
                    "elite": 0.05,
                    "exceptional": 0.10,
                }
            }
        )
    )
    monkeypatch.setenv("E4_CREATOR_EXPECTANCY_PATH", str(expectancy))
    monkeypatch.setenv("E4_WINNING_CREATORS_PATH", str(winners))
    monkeypatch.setenv("E4_DISCOVERED_CREATORS_PATH", str(discovered))
    monkeypatch.setenv("E4_SELECTION_PROFILE_PATH", str(profile))
    monkeypatch.setenv("E4_SELECTION_MEMORY_PATH", str(tmp_path / "memory.json"))
    monkeypatch.setenv("E4_SELECTION_MEMORY_PERSIST", "0")
    monkeypatch.setenv("E4_SELECTION_V2_ENABLED", "true")
    settings = core.Settings(
        live=False,
        wallet="test-wallet",
        model_path=tmp_path / "missing-model.json",
        max_position_fraction=0.20,
    )
    return UnifiedE4Policy(settings)


def test_tiny_creator_history_never_authorises_without_flow(policy: UnifiedE4Policy) -> None:
    policy.library.expectancy["thin"] = {"creator": "thin", "wins": 3, "losses": 0, "trades": 3}
    assessment = policy.library.assess("thin")
    decision = policy.decision(_state("mint-thin", "thin"))
    assert assessment.score_delta <= 0.03
    assert not decision.accepted
    assert decision.reason == "live flow confirmation missing"


def test_robust_negative_creator_is_vetoed(policy: UnifiedE4Policy) -> None:
    policy.library.expectancy["bad"] = {"creator": "bad", "wins": 0, "losses": 12, "trades": 12}
    decision = policy.decision(
        _state("mint-bad", "bad", buys=[(f"buyer-{i}", 1.0) for i in range(8)])
    )
    assert not decision.accepted
    assert decision.creator.robust_negative
    assert decision.reason == "robust negative creator veto"


def test_creator_history_is_context_not_requirement(policy: UnifiedE4Policy) -> None:
    decision = policy.decision(
        _state("mint-unknown", "unknown", buys=[(f"buyer-{i}", 1.0) for i in range(8)])
    )
    assert decision.accepted
    assert decision.score >= decision.threshold
    assert decision.fraction <= 0.10


def test_buyer_reputation_requires_resolved_sample(policy: UnifiedE4Policy) -> None:
    wallet = "buyer-a"
    policy.memory.register_entry("one", creator="c", buyers=[wallet], score=0.7, decision_ns=1)
    assert policy.memory.resolve("one", 0.4)
    one = policy.memory.buyer_assessment([wallet])
    assert one.qualified_wallets == 0

    for index in range(2, 9):
        mint = f"m-{index}"
        policy.memory.register_entry(mint, creator="c", buyers=[wallet], score=0.7, decision_ns=index)
        assert policy.memory.resolve(mint, 0.2 if index < 8 else -0.1)
    learned = policy.memory.buyer_assessment([wallet])
    assert learned.qualified_wallets == 1
    assert learned.posterior_mean > 0.5
    assert learned.score_delta > 0


def test_duplicate_outcomes_do_not_inflate_memory(policy: UnifiedE4Policy) -> None:
    policy.memory.register_entry("dup", creator="c", buyers=["b"], score=0.7, decision_ns=1)
    assert policy.memory.resolve("dup", 0.2)
    assert not policy.memory.resolve("dup", 0.2)
    assert policy.memory.creators["c"]["resolved"] == 1
    assert policy.memory.buyers["b"]["resolved"] == 1


def test_bad_live_streak_only_makes_system_more_conservative(policy: UnifiedE4Policy) -> None:
    before = policy.memory.regime()
    for index in range(12):
        mint = f"loss-{index}"
        policy.memory.register_entry(mint, creator="c", buyers=["b"], score=0.7, decision_ns=index)
        assert policy.memory.resolve(mint, -0.1)
    after = policy.memory.regime()
    assert before.threshold_add == 0
    assert after.threshold_add >= 0.04
    assert after.size_multiplier <= 0.65


def test_decision_is_deterministic_and_sizing_bounded(policy: UnifiedE4Policy) -> None:
    state = _state("det", "unknown", buys=[(f"buyer-{i}", 0.9) for i in range(7)])
    first = policy.decision(state)
    second = policy.decision(state)
    assert first == second
    assert 0 <= first.score <= 1
    assert 0 <= first.fraction <= 0.10


def test_memory_round_trip_is_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    one = OnlineMemory(path, persist=True)
    one.register_entry("mint", creator="creator", buyers=["a", "b"], score=0.8, decision_ns=123)
    two = OnlineMemory(path, persist=True)
    assert "mint" in two.pending
    assert two.resolve("mint", 0.25)
    three = OnlineMemory(path, persist=True)
    assert "mint" in three.resolved_mints
    assert "mint" not in three.pending
    assert three.creators["creator"]["resolved"] == 1


def test_invalid_state_never_enters(policy: UnifiedE4Policy) -> None:
    state = _state("invalid", "unknown", buys=[(f"buyer-{i}", 1.0) for i in range(8)])
    state.complete = True
    assert not policy.decision(state).accepted

    state2 = _state(
        "invalid-fdv",
        "unknown",
        buys=[(f"buyer-{i}", 1.0) for i in range(8)],
        fdv=50_000,
    )
    assert not policy.decision(state2).accepted


def test_authoritative_e4_boot_installs_unified_policy_last() -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    code = (
        "from memecoin_bot import e4_live as core;"
        "import memecoin_bot.e4_exec;"
        "from memecoin_bot.e4_selection_v2 import UnifiedE4Policy;"
        "assert core.E4Policy is UnifiedE4Policy;"
        "assert getattr(core.Engine.execute_sell,"
        "'_e4_selection_v2_learning_wrapper',False);"
        "print('AUTHORITATIVE_E4_BOOT_OK')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "AUTHORITATIVE_E4_BOOT_OK" in completed.stdout
