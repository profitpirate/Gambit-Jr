import asyncio
import copy
import hashlib
import importlib.util
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from memecoin_bot.v12_prearmed_discord import library_text
from memecoin_bot.v12_prearmed_library import (
    ACTIVATION_PATH,
    LIBRARY_PATH,
    LibraryPrearmedPolicy,
    PrearmedLibrary,
)
from memecoin_bot.v12_prearmed_policy import EntryFacts, FrozenPrearmedPolicy
from memecoin_bot.v12_prearmed_runtime import Runtime, State

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT / "research/v12-pre-armed-100-trade-frozen-model.json").read_text())
PAYLOAD = json.loads((ROOT / 'tests/fixtures/v12-prearmed-library-promotions-before-review.json').read_text())
ACTIVATION = json.loads((ROOT / ACTIVATION_PATH).read_text())
ACTIVATION = ACTIVATION | {"library_sha256": hashlib.sha256(
    json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
).hexdigest()}


def activation(payload):
    return ACTIVATION | {
        "library_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    }


def library():
    return PrearmedLibrary(PAYLOAD, ACTIVATION, MODEL)


def facts(creator):
    now = MODEL["maximum_evidence_ns"] + 100_000_000_000
    handle = MODEL["creator_handles"].get(creator, ["unverified"])[0]
    return EntryFacts(
        creator, handle, now - 1_000_000_000, now, now + 1_000_000, now, 2.0, False, 100, 100, 1, 1
    )


def test_historical_promotion_fixture_recognises_without_faking_certification():
    value = library()
    assert value.status() == {
        "state": "CONNECTED",
        "mode": "PROMOTED_RECOGNITION",
        "activation_basis": "OWNER_REQUEST",
        "history_certified": False,
        "sha256": ACTIVATION["library_sha256"],
        "promoted": 44,
        "shortlisted": 28,
        "elite": 14,
        "additional_creators": 31,
        "recognised_creators": 197,
        "promoted_missing_social_history": 31,
        "promoted_social_history_required": False,
        "auto_buy": False,
    }
    assert all(value.recognises(c) for c in value.promoted)
    assert all(not value.recognises(c) for c in set(value.shortlisted) - value.frozen)
    assert PAYLOAD["activation"]["history_certified"] is False


def test_all_promoted_creators_pass_recognition_without_a_historical_handle():
    value = library()
    policy = LibraryPrearmedPolicy(MODEL, value)
    for creator in value.promoted:
        assert policy.creator_known(creator)
        assert policy.entry(replace(facts(creator), social_handle="fresh_handle")).accepted
        assert value.describe(creator)["historical_social_required"] is False


@pytest.mark.parametrize("changes,reason", [
    ({"social_handle": ""}, "NOT_PREARMED_HANDLE"),
    ({"social_handle": None}, "NOT_PREARMED_HANDLE"),
    ({"social_handle": "bad/path"}, "NOT_PREARMED_HANDLE"),
    ({"social_status_ns": 1}, "SOCIAL_AGE_OUTSIDE_WINDOW"),
    ({"creator_seed_sol": 1.99}, "CREATOR_SEED_BELOW_FLOOR"),
    ({"mayhem_mode": True}, "MAYHEM_OR_UNKNOWN_MODE"),
    ({"current_price": 1.51}, "ENTRY_CHASE_GUARD_REJECTED"),
    ({"current_token_output": 64}, "ENTRY_OUTPUT_GUARD_REJECTED"),
    ({"current_token_output": float("nan")}, "INVALID_QUOTE"),
    ({"decision_ns": 1}, "INPUT_NOT_AVAILABLE_AT_DECISION"),
])
def test_promotion_never_bypasses_current_launch_and_quote_guards(changes, reason):
    value=library();policy=LibraryPrearmedPolicy(MODEL,value)
    for creator in value.promoted:
        decision=policy.entry(replace(facts(creator),**changes))
        assert not decision.accepted and decision.reason==reason


@pytest.mark.parametrize("changes", [
    {}, {"social_handle":"fresh_handle"}, {"social_handle":""},
    {"social_status_ns":1}, {"creator_seed_sol":1.99}, {"mayhem_mode":True},
])
def test_recognition_matches_the_existing_nextgen_selector(changes):
    spec=importlib.util.spec_from_file_location("reference_selector",ROOT/"scripts/e4_v12_fast_prearmed_selector.py")
    reference=importlib.util.module_from_spec(spec);spec.loader.exec_module(reference)
    value=library();policy=LibraryPrearmedPolicy(MODEL,value)
    for creator in value.frozen | set(value.promoted) | set(value.shortlisted) | {"unknown"}:
        item=replace(facts(creator),**changes)
        expected=reference.should_select_nextgen(
            creator=creator,social_handle=item.social_handle,social_status_ns=item.social_status_ns,
            create_ns=item.create_ns,prior_e4_attempts=MODEL["creator_prior_e4_attempts"].get(creator,0),
            creator_seed_sol=item.creator_seed_sol,mayhem_mode=item.mayhem_mode,
            creator_handles=MODEL["creator_handles"],promoted_creators=set(value.promoted),
            promoted_library_enabled=True)
        assert policy.entry(item).accepted is expected


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"creator_seed_sol": 1},
        {"current_price": 1.51},
        {"current_token_output": 64},
        {"mayhem_mode": True},
        {"social_handle": "not_prearmed"},
        {"social_status_ns": 1},
        {"decision_ns": 1},
    ],
)
def test_frozen_creator_decisions_and_safety_guards_are_unchanged(changes):
    value = library()
    extended, frozen = LibraryPrearmedPolicy(MODEL, value), FrozenPrearmedPolicy(MODEL)
    for creator in set(MODEL["creator_handles"]) - set(value.promoted):
        item = replace(facts(creator), **changes)
        assert extended.entry(item) == frozen.entry(item)
    assert extended.hold_ns == frozen.hold_ns == 2_000_000_000
    assert extended.fraction_of_available_cash == frozen.fraction_of_available_cash == 0.1
    assert extended.maximum_concurrent_positions == frozen.maximum_concurrent_positions == 2


@pytest.mark.parametrize(
    "corruption", ["auto_buy", "authority", "duplicate", "address", "nan", "activation"]
)
def test_corrupt_or_overprivileged_library_fails_closed(corruption):
    payload = copy.deepcopy(PAYLOAD)
    if corruption == "auto_buy":
        payload["promoted"][0]["auto_buy"] = True
    if corruption == "authority":
        payload["promoted"][0]["selection_authority"] = "AUTO_BUY"
    if corruption == "duplicate":
        payload["promoted"].append(payload["promoted"][0])
    if corruption == "address":
        payload["promoted"][0]["creator"] = "../../secrets"
    auth = activation(payload)
    if corruption == "nan":
        payload["promoted"][0]["quality_score"] = float("nan")
    if corruption == "activation":
        auth["auto_buy"] = True
    with pytest.raises(ValueError):
        PrearmedLibrary(payload, auth, MODEL)


def test_loaded_lookup_cannot_be_changed_by_caller_mutation():
    payload = copy.deepcopy(PAYLOAD)
    value = PrearmedLibrary(payload, activation(payload), MODEL)
    creator = payload["promoted"][0]["creator"]
    payload["promoted"].clear()
    assert value.recognises(creator)
    with pytest.raises(TypeError):
        value.promoted[creator] = None


def test_release_integrity_covers_library_and_activation(tmp_path):
    manifest = {"files": {}}
    for name, value in ((LIBRARY_PATH, PAYLOAD), (ACTIVATION_PATH, ACTIVATION)):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        manifest["files"][name] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest))
    assert PrearmedLibrary.from_root(tmp_path, MODEL).status()["promoted"] == 44
    (tmp_path / LIBRARY_PATH).write_text("{}")
    with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
        PrearmedLibrary.from_root(tmp_path, MODEL)


@pytest.mark.parametrize("stale", [False, True])
def test_promoted_feed_reaches_order_only_with_a_valid_current_launch(tmp_path, monkeypatch, stale):
    async def scenario():
        value = library()
        creator = next(iter(set(value.promoted) - value.frozen))
        now = facts(creator).create_ns
        state = State(tmp_path / "state.sqlite", wallet="wallet", mode="live")
        events = []
        orders = []
        social_ns=now-(11_000_000_000 if stale else 1_000_000_000)
        snowflake=((social_ns//1_000_000)-1288834974657)<<22
        async def metadata(session, uri, **kwargs):
            return {"twitter":f"https://x.com/fresh_handle/status/{snowflake}"},now
        async def quote(request):
            assert not stale, "Stale social evidence must be rejected before requesting a quote"
            assert request["action"]=="QUOTE"
            return {"quoted_tokens_raw":"100", "curve_price":1.0}
        monkeypatch.setattr("memecoin_bot.v12_prearmed_runtime.metadata",metadata)
        runner = Runtime(
            model=MODEL,
            state=state,
            rpc=None,
            builder=SimpleNamespace(request=quote),
            execution=SimpleNamespace(),
            wallet="wallet",
            metadata_session=None,
            emit=lambda *args: events.append(args),
            kill_path=tmp_path / "STOP",
            clock=lambda: now,
            creator_library=value,
        )
        runner.connected = True
        runner.order=lambda payload,context,**kwargs: orders.append((payload,context,kwargs))
        runner.balance = 1_000_000_000
        runner.balance_ns = runner.last_feed_ns = now
        await runner.on_batch(
            "signature",
            [
                {
                    "anchor_event": "CreateEvent",
                    "creator": creator,
                    "mint": "mint",
                    "is_mayhem_mode": False,
                    "uri":"https://metadata.example/token",
                    "timestamp":now//1_000_000_000,
                    "virtual_sol_reserves":100_000_000_000,
                    "virtual_token_reserves":100_000_000,
                },
                {
                    "anchor_event": "TradeEvent",
                    "user": creator,
                    "mint": "mint",
                    "is_buy": True,
                    "sol_amount": 2_000_000_000,
                },
            ],
            now,
        )
        await asyncio.gather(*list(runner.tasks))
        if stale:
            decision = next(p for e, p in events if e == "CANDIDATE_REJECTED")
            assert decision["reason"] == "SOCIAL_AGE_OUTSIDE_WINDOW"
            assert orders==[]
        else:
            decision = next(p for e, p in events if e == "FROZEN_SIGNAL")
            assert len(orders)==1 and orders[0][0]["side"]=="BUY"
            assert orders[0][0]["creator"]==creator
        assert decision["creator_source"] == "PROMOTED_LIBRARY"
        assert decision["creator_recognised"] is True
        assert decision["historical_social_required"] is False
        assert decision["library_history_certified"] is False
        assert runner.diagnostics.counts["KNOWN_CREATOR_SEEN"] == 1
        assert not state.data["pending"]
        assert not runner.busy_mints
        assert runner.status()["creator_library"]["state"] == "CONNECTED"
        state.conn.close()

    asyncio.run(scenario())


def test_discord_shows_connected_state_and_material_entry_blocker():
    value = library()
    report = {"library": value.status(), "loaded_by_live_bot": True}
    text = library_text(report)
    assert "44 promoted" in text and "Promotion passes the creator check" in text
    assert "31 need prior social history" not in text
    assert "certification: pending" in text
    report["creator"] = value.describe(next(iter(set(value.promoted) - value.frozen)))
    text = library_text(report)
    assert "Creator check: **passed**" in text and "Entry blocked" not in text
    assert "Fresh social post" in text
    report["loaded_by_live_bot"] = False
    assert "live access not confirmed" in library_text(report)


def test_discord_log_download_retains_library_context_and_redacts_secrets(tmp_path):
    from memecoin_bot.v12_prearmed_operator_backend import OperatorBackend

    shutil.copyfile(
        ROOT / "deployment/gambit-monitor-collector.py", tmp_path / "gambit-monitor-collector.py"
    )
    (tmp_path / "prearmed-data").mkdir()
    value = library()
    creator = next(iter(set(value.promoted) - value.frozen))
    details = value.describe(creator) | {
        "reason": "LIBRARY_SOCIAL_HISTORY_MISSING",
        "provider_url": "https://secret.invalid/?key=SECRET",
    }
    row = {"ts_ns": time.time_ns(), "event": "CANDIDATE_REJECTED", "payload": details}
    (tmp_path / "prearmed-data/live-audit.jsonl").write_text(json.dumps(row) + "\n")
    result = OperatorBackend(tmp_path).logs()
    payload = result["events"][0]["payload"]
    assert payload["library_status"] == "PROMOTED"
    assert payload["creator_recognised"] is True
    assert payload["prearmed_social_available"] is False
    assert payload["historical_social_required"] is False
    assert payload["library_history_certified"] is False
    assert payload["library_sha256"] == value.sha256
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {}, {"social_handle": "not_prearmed"}, {"social_handle": ""},
    {"social_status_ns": 1}, {"creator_seed_sol": 1.99}, {"mayhem_mode": True},
    {"current_price": 1.51}, {"current_token_output": 64}, {"decision_ns": 1},
])
def test_installed_review_library_matches_original_selector_for_every_creator(changes):
    payload = json.loads((ROOT / LIBRARY_PATH).read_text())
    auth = json.loads((ROOT / ACTIVATION_PATH).read_text())
    value = PrearmedLibrary(payload, auth, MODEL)
    assert value.status()['promoted'] == 0
    assert value.status()['additional_creators'] == 0
    assert value.status()['recognised_creators'] == 166
    assert value.status()['shortlisted'] == 71
    policy = LibraryPrearmedPolicy(MODEL, value)
    original = FrozenPrearmedPolicy(MODEL)
    creators = value.frozen | {r['creator'] for r in PAYLOAD['promoted'] + PAYLOAD['shortlisted']} | {'unknown'}
    for creator in creators:
        item = replace(facts(creator), **changes)
        assert policy.entry(item) == original.entry(item), creator
        assert policy.creator_known(creator) == original.creator_known(creator)
    assert policy.hold_ns == original.hold_ns == 2_000_000_000
    assert policy.fraction_of_available_cash == original.fraction_of_available_cash == 0.1
    assert policy.maximum_concurrent_positions == original.maximum_concurrent_positions == 2


@pytest.mark.parametrize('creator', [
    '8Wz4CQpTsuMtf71mNTJNP7Anm2E8pidZLRUj8taahYWF',
    '866SrsMfwKsErATZDCLAPbxH3NuqjKn7zQ1xXMqixoz1',
])
def test_both_creators_responsible_for_all_six_live_losses_have_no_entry_authority(creator):
    payload = json.loads((ROOT / LIBRARY_PATH).read_text())
    auth = json.loads((ROOT / ACTIVATION_PATH).read_text())
    value = PrearmedLibrary(payload, auth, MODEL)
    policy = LibraryPrearmedPolicy(MODEL, value)
    assert not value.recognises(creator)
    assert not policy.entry(facts(creator)).accepted
    assert creator not in value.promoted
    if creator.startswith('8Wz4'):
        assert creator not in value.shortlisted
        assert [row['creator'] for row in payload['excluded']] == [creator]


def test_research_promotion_cannot_restore_live_privileges_without_a_new_pinned_activation():
    payload = json.loads((ROOT / LIBRARY_PATH).read_text())
    auth = json.loads((ROOT / ACTIVATION_PATH).read_text())
    payload['promoted'] = PAYLOAD['promoted']
    with pytest.raises(ValueError, match='LIBRARY_ACTIVATION_CHECKSUM_MISMATCH'):
        PrearmedLibrary(payload, auth, MODEL)


def test_discord_explains_original_rules_and_disabled_promotions():
    payload = json.loads((ROOT / LIBRARY_PATH).read_text())
    auth = json.loads((ROOT / ACTIVATION_PATH).read_text())
    value = PrearmedLibrary(payload, auth, MODEL)
    text = library_text({'library': value.status(), 'loaded_by_live_bot': True})
    assert 'Original tested rules' in text
    assert '166 creators' in text and '71 research-only' in text
    assert 'Extra promotions disabled' in text
    assert 'Promotion passes' not in text
