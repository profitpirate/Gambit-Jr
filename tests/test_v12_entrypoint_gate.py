from __future__ import annotations

import sys

import pytest

from memecoin_bot.e4_exec import __main__ as entry
from memecoin_bot import e4_prod


def test_live_flag_fails_closed_without_e4_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["gambit-e4", "run", "--live"])
    monkeypatch.delenv("E4_LIVE", raising=False)
    with pytest.raises(SystemExit, match="requires both E4_LIVE=true and --live"):
        entry._preflight_live_if_requested()


def test_non_live_run_does_not_start_background_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["gambit-e4", "run"])
    monkeypatch.setattr(entry, "_preflight_live_if_requested", lambda: calls.append("preflight"))
    monkeypatch.setattr(entry, "_start_v12_pipelines", lambda: calls.append("pipelines"))
    monkeypatch.setattr(entry, "_engine_main", lambda: calls.append("engine"))

    entry.main()

    assert calls == ["preflight", "engine"]


def test_live_run_preflights_before_starting_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["gambit-e4", "run", "--live"])
    monkeypatch.setattr(entry, "_preflight_live_if_requested", lambda: calls.append("preflight"))
    monkeypatch.setattr(entry, "_start_v12_pipelines", lambda: calls.append("pipelines"))
    monkeypatch.setattr(entry, "_engine_main", lambda: calls.append("engine"))

    entry.main()

    assert calls == ["preflight", "pipelines", "engine"]


def test_status_never_starts_background_services(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["gambit-e4", "status"])
    monkeypatch.setattr(entry, "_preflight_live_if_requested", lambda: calls.append("preflight"))
    monkeypatch.setattr(entry, "_start_v12_pipelines", lambda: calls.append("pipelines"))
    monkeypatch.setattr(entry, "_engine_main", lambda: calls.append("engine"))

    entry.main()

    assert calls == ["preflight", "engine"]


def test_legacy_e4_prod_delegates_to_canonical_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(entry, "main", lambda: calls.append("canonical"))

    e4_prod.main()

    assert calls == ["canonical"]
