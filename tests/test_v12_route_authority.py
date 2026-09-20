from __future__ import annotations

from types import SimpleNamespace

from memecoin_bot import e4_v12_authority as subject


class Low:
    pass


class High:
    pass


def test_higher_priority_route_authority_cannot_be_silently_downgraded() -> None:
    core = SimpleNamespace(RouteSender=object)
    subject.install_route_sender(
        core,
        High,
        priority=subject.ROUTE_PRIORITY_FINAL,
        authority="final",
    )
    result = subject.install_route_sender(
        core,
        Low,
        priority=subject.ROUTE_PRIORITY_EPOCH,
        authority="epoch",
    )
    assert result is High
    assert core.RouteSender is High
    assert subject.route_authority(core)["authority"] == "final"


def test_equal_or_higher_priority_can_explicitly_replace_authority() -> None:
    core = SimpleNamespace(RouteSender=object)
    subject.install_route_sender(core, Low, priority=100, authority="low")
    subject.install_route_sender(core, High, priority=400, authority="high")
    assert core.RouteSender is High
    assert subject.route_authority(core)["priority"] == 400
