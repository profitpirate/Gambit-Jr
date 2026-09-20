from __future__ import annotations

from types import SimpleNamespace

from memecoin_bot import e4_nextgen_creator_authority_v12 as subject
from memecoin_bot.v12_creator_library import CreatorLibrary


def promoted_library() -> CreatorLibrary:
    return CreatorLibrary(
        {
            "schema_version": "v12-creator-library-v2",
            "promoted": [
                {
                    "creator": "elite",
                    "status": "PROMOTED",
                    "tier": "ELITE",
                    "source": "E4_VERIFIED_HISTORY",
                    "quality_score": 0.95,
                    "selection_authority": "RECOGNISED_CREATOR_ONLY",
                    "auto_buy": False,
                    "e4": {"wins": 5, "losses": 0, "trades": 5},
                    "fresh": None,
                }
            ],
            "shortlisted": [],
        }
    )


def passed_state() -> dict:
    return {
        "completion": {"reached": True},
        "metrics": {
            "closed_trades": 100,
            "acceptance_gate_passed": True,
        },
    }


def enable(monkeypatch) -> None:
    lib = promoted_library()
    monkeypatch.setattr(subject, "_LIBRARY", lib)
    monkeypatch.setattr(subject, "_CAUSAL_STATE", passed_state())
    monkeypatch.setattr(
        subject,
        "_PROMOTED_RAW",
        {
            "elite": {
                "creator": "elite",
                "tier": "ELITE",
                "quality_score": 0.95,
                "e4": {"wins": 5, "losses": 0, "trades": 5},
                "fresh": None,
            }
        },
    )
    monkeypatch.setenv("V12_PROMOTED_LIBRARY_ENABLED", "true")
    monkeypatch.setenv("V12_POSTCERT_AGGRESSIVE_RISK_ENABLED", "true")
    monkeypatch.setenv(
        "V12_CURRENT_CLOSED_EQUITY_DRAWDOWN_FRACTION",
        "0",
    )


def test_active_creator_history_uses_only_canonical_promoted(monkeypatch) -> None:
    enable(monkeypatch)
    wins, losses, trades, rate, profile = subject._creator_history_nextgen("elite")
    assert (wins, losses, trades, rate) == (5, 0, 5, 1.0)
    assert profile.source == "v12-canonical-promoted-library"

    assert subject._creator_history_nextgen("stale") == (0, 0, 0, 0.0, None)


def test_promoted_elite_gets_postcert_fraction_after_selection(monkeypatch) -> None:
    enable(monkeypatch)
    mint = "mint-elite"
    subject.v6._CONTEXT_BY_MINT[mint] = {"creator": "elite"}
    profile = SimpleNamespace(
        family="v12_elite_creator_quality_launch",
        fraction=0.03,
        features={},
    )
    subject.v6._PROFILE_BY_MINT[mint] = profile
    monkeypatch.setattr(
        subject,
        "_PREVIOUS_ENTRY",
        lambda _self, _state: (
            True,
            0.95,
            0.03,
            "passed V12 launch-quality gates",
            {},
        ),
    )
    self = SimpleNamespace(settings=SimpleNamespace(max_position_fraction=0.20))
    state = SimpleNamespace(mint=mint, creator="elite")
    accepted, _score, fraction, reason, features = subject._entry_nextgen_authority(
        self,
        state,
    )
    assert accepted is True
    assert fraction == 0.175
    assert "post-cert canonical risk" in reason
    assert features["v12_canonical_creator"] == 1.0


def test_stale_noncanonical_creator_fast_path_is_vetoed(monkeypatch) -> None:
    enable(monkeypatch)
    mint = "mint-stale"
    subject.v6._CONTEXT_BY_MINT[mint] = {"creator": "stale"}
    subject.v6._PROFILE_BY_MINT[mint] = SimpleNamespace(
        family="v12_recent_e4_repeat_launch",
        fraction=0.0185,
        features={},
    )
    monkeypatch.setattr(
        subject,
        "_PREVIOUS_ENTRY",
        lambda _self, _state: (
            True,
            0.9,
            0.0185,
            "old live-E4 creator fast path",
            {},
        ),
    )
    self = SimpleNamespace(settings=SimpleNamespace(max_position_fraction=0.20))
    state = SimpleNamespace(mint=mint, creator="stale")
    result = subject._entry_nextgen_authority(self, state)
    assert result[0] is False
    assert "canonical creator veto" in result[3]
    assert mint not in subject.v6._PROFILE_BY_MINT


def test_inactive_authority_is_exact_pass_through(monkeypatch) -> None:
    monkeypatch.delenv("V12_PROMOTED_LIBRARY_ENABLED", raising=False)
    monkeypatch.delenv("V12_POSTCERT_AGGRESSIVE_RISK_ENABLED", raising=False)
    expected = (True, 0.8, 0.03, "legacy", {"x": 1.0})
    monkeypatch.setattr(subject, "_PREVIOUS_ENTRY", lambda _self, _state: expected)
    result = subject._entry_nextgen_authority(
        SimpleNamespace(settings=SimpleNamespace(max_position_fraction=0.20)),
        SimpleNamespace(mint="m", creator="c"),
    )
    assert result == expected
