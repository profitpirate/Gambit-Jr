from __future__ import annotations

import time
from pathlib import Path

from memecoin_bot.v12_creator_lifecycle import (
    CreatorLifecycleStore,
    CreatorState,
)
from memecoin_bot.v12_operator_graph import OperatorGraph


def test_creator_lifecycle_can_demote_and_quarantine(tmp_path: Path) -> None:
    store = CreatorLifecycleStore(tmp_path / "e4.db", half_life_days=14)
    seeded = store.seed_canonical(
        "creator",
        tier="ELITE",
        wins=7,
        losses=0,
        pnl_sol=2.0,
    )
    assert seeded.state in {CreatorState.ELITE, CreatorState.PROMOTED}

    now = time.time_ns()
    for index in range(4):
        state = store.observe_trade(
            "creator",
            won=False,
            pnl_sol=-0.1,
            observed_ns=now + index,
        )
    assert state.state == CreatorState.QUARANTINE
    assert store.entry_allowed("creator") is False


def test_operator_graph_never_clusters_on_metadata_host_alone(tmp_path: Path) -> None:
    graph = OperatorGraph(tmp_path / "e4.db")
    graph.observe("a", metadata_host="same.example")
    graph.observe("b", metadata_host="same.example")
    assert graph.rebuild() == []

    graph.observe("a", social_handle="samehandle")
    graph.observe("b", social_handle="samehandle")
    members = graph.rebuild()
    assert {item.creator for item in members} == {"a", "b"}
