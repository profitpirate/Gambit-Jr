from __future__ import annotations

from types import SimpleNamespace

from memecoin_bot import e4_adaptive_exit_v12 as subject


class Flow:
    def __init__(self, net, ratio):
        self.net = net
        self.ratio = ratio


class State:
    price_sol = 1.20

    def flow(self, milliseconds):
        return Flow(1.0, 2.0) if milliseconds in {250, 1000} else Flow(0, 0)


def test_adverse_exit_is_never_relaxed(monkeypatch) -> None:
    monkeypatch.setattr(subject, "_enabled", lambda: True)
    monkeypatch.setattr(
        subject,
        "_PREVIOUS_EXIT",
        lambda _self, _position, _state: (
            "SELL_ALL",
            1.0,
            "fast adverse failure",
        ),
    )
    position = SimpleNamespace(
        age_ms=1000,
        entry_price=1.0,
        last_price=1.2,
        max_price=1.2,
        first_partial_done=False,
    )
    assert subject._exit_adaptive(object(), position, State()) == (
        "SELL_ALL",
        1.0,
        "fast adverse failure",
    )


def test_strong_survivor_can_extend_but_hard_ceiling_remains(monkeypatch) -> None:
    monkeypatch.setattr(subject, "_enabled", lambda: True)
    monkeypatch.setattr(
        subject,
        "_PREVIOUS_EXIT",
        lambda _self, _position, _state: (
            "SELL_ALL",
            1.0,
            "maximum hold",
        ),
    )
    position = SimpleNamespace(
        age_ms=1500,
        entry_price=1.0,
        last_price=1.2,
        max_price=1.2,
        first_partial_done=False,
    )
    assert subject._exit_adaptive(object(), position, State())[0] == "HOLD"
    position.age_ms = 11_000
    assert subject._exit_adaptive(object(), position, State())[0] == "SELL_ALL"
