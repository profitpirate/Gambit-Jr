"""Durable offline/paper runtime adapter. No network or wallet functions.

Inputs must come from an explicitly authorized future adapter. An acknowledged
input has committed its resulting state. Recovery preserves open inventory and
marks interrupted evidence; it never synthesizes a fill during missing time.
"""
from __future__ import annotations
import copy
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from golden_full3s_v2 import Runtime
from golden_full3s_store import Store


class PaperService:
    def __init__(self, path: str | Path, initial: Runtime | None = None):
        self.store = Store(path, initial.snapshot() if initial is not None else None)
        current = self.store.read()
        # This commit is a provenance event, not a price/trade event.
        if current["candidate"]["engine"]["bundles"] or current["observer"]["engine"]["bundles"]:
            self.store.apply("restart:" + str(uuid.uuid4()), {"kind": "PROCESS_RESTART", "wall_ns": time.time_ns()},
                             lambda data: Runtime.restore(data, process_restart=True).snapshot())

    def process(self, event_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        event = copy.deepcopy(dict(event))
        if event.get("kind") not in ("SUBMIT", "TICK"):
            raise ValueError("only explicit paper SUBMIT/TICK inputs accepted")
        t0 = time.perf_counter_ns()
        def transition(raw):
            r = Runtime.restore(raw, process_restart=False)
            if event["kind"] == "SUBMIT":
                r.submit(event["decision"], event["state"], event["now"], queue_depth=event.get("queue_depth", 0))
            else:
                r.tick(event["now"], event["states"], event["last_feed_ns"])
            r.candidate.validate(); r.observer.validate()
            return r.snapshot()
        committed = self.store.apply(event_id, event, transition)
        return {"committed_new_input": committed,
                "local_processing_and_durable_commit_ms": (time.perf_counter_ns()-t0)/1e6,
                "actual_chain_acknowledgement_ms": None}

    def runtime(self) -> Runtime:
        return Runtime.restore(self.store.read(), process_restart=False)

    def close(self) -> None:
        self.store.close()
