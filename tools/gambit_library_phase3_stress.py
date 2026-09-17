#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import random
import tempfile

from gambit_library_policy import AppendStore, DeveloperLibrary, MODEL, RuleConfig


def main() -> int:
    # Empty historical dev library must not invent trust.
    lib=DeveloperLibrary({"devs":{}},RuleConfig())
    assert lib.decide("new-dev")["qualifies"] is False
    assert lib.decide("")["qualifies"] is False

    # Causal forward learning: three resolved prior coins, 2/3 wins and positive mean.
    assert lib.update(mint="a",creator="dev-A",return_fraction=.40)
    assert lib.update(mint="b",creator="dev-A",return_fraction=.20)
    assert lib.decide("dev-A")["qualifies"] is False
    assert lib.update(mint="c",creator="dev-A",return_fraction=-.10)
    decision=lib.decide("dev-A")
    assert decision["qualifies"] is True, decision
    assert decision["developer_stats_before_decision"]["resolved"]==3
    # Duplicate mint must never inflate a developer.
    assert lib.update(mint="c",creator="dev-A",return_fraction=99) is False
    assert lib.stats("dev-A")["resolved"]==3

    # Bad repeated developer remains rejected.
    for i,r in enumerate((-.1,-.2,.01)):
        assert lib.update(mint=f"bad-{i}",creator="dev-B",return_fraction=r)
    assert lib.decide("dev-B")["qualifies"] is False

    # Historical rows are used only when explicit resolved/win/loss fields exist.
    hist=DeveloperLibrary({"devs":{"good":{"unique_resolved_coins":5,"unique_coin_wins":4,"unique_coin_losses":1,"return_sum":1.2},
                                         "unknown":{"foo":"bar"}}})
    assert hist.decide("good")["qualifies"] is True
    assert hist.decide("unknown")["qualifies"] is False

    # Append store is durable and idempotent.
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/"events.jsonl"
        s=AppendStore(p)
        assert s.append({"event_id":"1","kind":"TEST","mint":"x"})
        assert not s.append({"event_id":"1","kind":"TEST","mint":"x"})
        s2=AppendStore(p)
        assert not s2.append({"event_id":"1","kind":"TEST","mint":"x"})
        assert len(p.read_text().splitlines())==1

    # Stress: arbitrary ordering/duplicates cannot make resolved count exceed unique mints.
    rng=random.Random(12)
    stress=DeveloperLibrary({"devs":{}})
    seen=set()
    for _ in range(20000):
        creator=f"d{rng.randrange(50)}"; mint=f"m{rng.randrange(4000)}"; ret=rng.uniform(-1,3)
        accepted=stress.update(mint=mint,creator=creator,return_fraction=ret)
        if accepted:
            seen.add(mint)
    assert sum(v["resolved"] for v in stress.forward.values())==len(seen)
    for creator in stress.forward:
        d=stress.decide(creator)
        assert d["model"]==MODEL
        assert d["developer_stats_before_decision"]["resolved"]>=0

    # Safety regression: the new live runner contains no transaction-submission or signer path.
    live=Path("tools/gambit_library_competitor.py").read_text()
    forbidden=("sendRawTransaction","sendTransaction","PRIVATE_KEY","Keypair.from","wallet.sign","secret_key")
    for term in forbidden:
        assert term not in live, term
    assert "READ_METHODS" in live and "paper_only" in live

    # Frozen top-four implementation remains byte-for-byte accepted by its own manifest verifier.
    from golden_top4_live import verify_manifest
    manifest=verify_manifest(Path("config/golden-top4-live.json"))
    assert manifest["models"]==["FULL_3S","FULL_4S","FULL_7S","FULL_3S_V2"]
    assert manifest["execution"]=="PAPER_ONLY"

    print(json.dumps({"status":"PASS","model":MODEL,"random_updates":20000,"unique_mints":len(seen),
                      "frozen_four_untouched":True,"real_execution":False},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
