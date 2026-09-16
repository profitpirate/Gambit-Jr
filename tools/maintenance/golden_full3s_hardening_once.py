"""Apply once during OFFLINE build; successful CI persists the changed sources.

This is not a runtime monkeypatch. Narrow anchors fail rather than silently
altering unexpected code. No strategy/cost/hold parameter is changed here.
"""
from pathlib import Path

MARK='FULL3S_HARDENING_20260916_1'
def edit(name, changes):
    p=Path(name);s=p.read_text()
    if MARK in s:return
    for old,new in changes:
        if s.count(old)!=1:raise RuntimeError(f'{name}: expected one patch anchor {old[:70]!r}, got {s.count(old)}')
        s=s.replace(old,new)
    p.write_text(s+'\n# '+MARK+'\n')

edit('tools/golden_full3s_policy.py',[
('        self.c = evidence.c\n','        self.c = evidence.c\n        self.policy_hash = self.c.digest()\n'),
('"policy_hash": self.c.digest(),','"policy_hash": self.policy_hash,')])
edit('tools/golden_full3s_v2.py',[
('Config = base.Config\n','''Config = base.Config
IMPLEMENTATION_DIGEST = hashlib.sha256(b"".join(
    name.encode() + hashlib.sha256(Path(__file__).with_name(name).read_bytes()).digest()
    for name in ("golden_full3s_v2.py", "golden_full3s_policy.py", "golden_full3s_store.py",
                 "golden_full3s_service.py", "golden_full3s_diagnostics.py", "golden_horizon_engine.py")
)).hexdigest()
'''),
('return copy.deepcopy({"model": MODEL, "config": asdict(self.c), "policy": asdict(self.pc),','return copy.deepcopy({"model": MODEL, "implementation_digest": IMPLEMENTATION_DIGEST, "config": asdict(self.c), "policy": asdict(self.pc),'),
('        if data["model"] != MODEL: raise ValueError("wrong model snapshot")\n','''        if data["model"] != MODEL: raise ValueError("wrong model snapshot")
        if data.get("implementation_digest") != IMPLEMENTATION_DIGEST:
            raise ValueError("changed implementation on resume; explicit migration required")
'''),
('            self.bundles = copy.deepcopy(raw["bundles"])\n','''            self.bundles = copy.deepcopy(raw["bundles"])
            # JSON cannot preserve object aliases. A closed position may still
            # be in the post-exit bundle AND the ledger; reconnect both views.
            ledger_index = {p["signal_id"]: p for p in self.accounts[ARM]["ledger"]}
            for bundle in self.bundles.values():
                p = bundle["positions"][ARM]
                if p["status"] == "CLOSED":
                    if p["signal_id"] not in ledger_index:
                        raise ValueError("closed bundle missing ledger position")
                    bundle["positions"][ARM] = ledger_index[p["signal_id"]]
'''),
('        self.labelled: set[str] = set()\n','''        self.labelled: set[str] = set()
        self.label_cursor = 0
        self.next_prune_ns = 0
'''),
('        for p in self.observer.accounts[ARM]["ledger"]:\n','''        rows = self.observer.accounts[ARM]["ledger"]
        fresh_rows = rows[self.label_cursor:]
        self.label_cursor = len(rows)
        for p in fresh_rows:
'''),
('        if self.candidate.errors or self.observer.errors: self.halt_new_entries = True\n','''        if now >= self.next_prune_ns:
            self.evidence.prune(now)
            self.next_prune_ns = now + 3_600_000_000_000
        if self.candidate.errors or self.observer.errors: self.halt_new_entries = True
'''),
('            "labelled": sorted(self.labelled), "last_ns": self.last_ns,\n','''            "labelled": sorted(self.labelled), "last_ns": self.last_ns,
            "label_cursor": self.label_cursor, "next_prune_ns": self.next_prune_ns,
'''),
('        r.labelled = set(data["labelled"]); r.last_ns = data["last_ns"]\n','''        r.labelled = set(data["labelled"]); r.last_ns = data["last_ns"]
        r.label_cursor = data.get("label_cursor", len(r.observer.accounts[ARM]["ledger"]))
        r.next_prune_ns = data.get("next_prune_ns", 0)
        if not 0 <= r.label_cursor <= len(r.observer.accounts[ARM]["ledger"]):
            raise ValueError("invalid label cursor")
        if r.evidence.cost_hash != r.c.fingerprint() or r.evidence.fixture_mode != r.fixture_mode or r.evidence.c.digest() != r.pc.digest():
            raise ValueError("snapshot evidence/cost/policy mode mismatch")
''')])
edit('tools/golden_full3s_certify.py',[
('range(0,24501,250)', 'range(0,30001,250)'),
('scenario=i%8','scenario=i%10'),
('if scenario==3 and dt==3250:r=-.45','if scenario==3 and dt==3500:r=-.45'),
('if scenario==4:r=0','if scenario==4:r=.65 if dt==250 else 0\n                if scenario==8 and 3500<=dt<=5000:r=-.40'),
('            s=state(ns,x,y)\n','            s=state(ns,x,y)\n            if scenario==9 and dt==250:s["rtok"]=1.0\n'),
('    return {"measurement":"LOCAL_CPU_AND_LOCAL_DURABLE_COMMIT_ONLY",','''    active_commit=[]
    from golden_full3s_service import PaperService
    with tempfile.TemporaryDirectory() as tmp:
        svc=PaperService(Path(tmp)/"active.db",Runtime(fixture_mode=True))
        svc.process("submit",{"kind":"SUBMIT","decision":decision(),"state":state(),"now":T0})
        for i in range(100):
            t=T0+250_000_000+i*1000
            ans=svc.process(str(i),{"kind":"TICK","now":t,"last_feed_ns":t,"states":{"fixture-coin":state(t)}})
            active_commit.append(ans["local_processing_and_durable_commit_ms"])
        svc.store.verify();svc.close()
    return {"measurement":"LOCAL_CPU_AND_LOCAL_DURABLE_COMMIT_ONLY",
            "durable_active_runtime_tick_ms":base.quantiles(active_commit),''')])
