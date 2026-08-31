#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one match, found {text.count(old)}")
    return text.replace(old, new, 1)


def main() -> int:
    path = Path("src/memecoin_bot/e4_pipelines_v10.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''        self._intents_by_creator: Mapping[str, AuthorizedLaunchIntent] = MappingProxyType({})
        self._intents_by_mint: Mapping[str, AuthorizedLaunchIntent] = MappingProxyType({})
        self._narratives: Mapping[str, tuple[NarrativeSignal, ...]] = MappingProxyType({})
        self._e4_entries: Mapping[str, E4EntrySignal] = MappingProxyType({})
''',
        '''        self._intents_by_creator: Mapping[str, AuthorizedLaunchIntent] = MappingProxyType({})
        self._intents_by_mint: Mapping[str, AuthorizedLaunchIntent] = MappingProxyType({})
        self._narrative_store: dict[str, tuple[NarrativeSignal, ...]] = {}
        self._narratives: Mapping[str, tuple[NarrativeSignal, ...]] = MappingProxyType(self._narrative_store)
        self._e4_entry_store: dict[str, E4EntrySignal] = {}
        self._e4_entries: Mapping[str, E4EntrySignal] = MappingProxyType(self._e4_entry_store)
''',
        "store initialization",
    )
    text = replace_once(
        text,
        '''        now = _now_ns()
        with self._update_lock:
            narratives = {
                term: tuple(item for item in rows if item.expires_ns > now)
                for term, rows in self._narratives.items()
            }
            for term in terms:
                rows = list(narratives.get(term, ()))
                rows.append(signal)
                narratives[term] = tuple(rows[-32:])
            for address in addresses:
                key = f"address:{address}"
                rows = list(narratives.get(key, ()))
                rows.append(signal)
                narratives[key] = tuple(rows[-32:])
            self._narratives = MappingProxyType({key: value for key, value in narratives.items() if value})
''',
        '''        now = _now_ns()
        with self._update_lock:
            for term in terms:
                rows = [item for item in self._narrative_store.get(term, ()) if item.expires_ns > now]
                rows.append(signal)
                self._narrative_store[term] = tuple(rows[-32:])
            for address in addresses:
                key = f"address:{address}"
                rows = [item for item in self._narrative_store.get(key, ()) if item.expires_ns > now]
                rows.append(signal)
                self._narrative_store[key] = tuple(rows[-32:])
            # Bound memory without turning every social post into an O(n) copy.
            if len(self._narrative_store) > 250_000:
                stale = [
                    key
                    for key, rows in list(self._narrative_store.items())[:50_000]
                    if not rows or max(item.expires_ns for item in rows) <= now
                ]
                for key in stale:
                    self._narrative_store.pop(key, None)
''',
        "social store update",
    )
    text = replace_once(
        text,
        '''        with self._update_lock:
            entries = dict(self._e4_entries)
            entries[mint] = signal
            if len(entries) > 20_000:
                entries = dict(sorted(entries.items(), key=lambda pair: pair[1].observed_ns)[-10_000:])
            self._e4_entries = MappingProxyType(entries)
''',
        '''        with self._update_lock:
            self._e4_entry_store[mint] = signal
            if len(self._e4_entry_store) > 20_000:
                oldest = sorted(
                    self._e4_entry_store.items(),
                    key=lambda pair: pair[1].observed_ns,
                )[:10_000]
                for old_mint, _value in oldest:
                    self._e4_entry_store.pop(old_mint, None)
''',
        "E4 entry store update",
    )
    text = replace_once(
        text,
        '''        with self._update_lock:
            entries = dict(self._e4_entries)
            entries[mint] = E4EntrySignal(
                mint=existing.mint,
                creator=existing.creator,
                observed_ns=existing.observed_ns,
                entry_price_sol=existing.entry_price_sol,
                entry_sol=existing.entry_sol,
                signature=existing.signature,
                sold=True,
            )
            self._e4_entries = MappingProxyType(entries)
''',
        '''        with self._update_lock:
            self._e4_entry_store[mint] = E4EntrySignal(
                mint=existing.mint,
                creator=existing.creator,
                observed_ns=existing.observed_ns,
                entry_price_sol=existing.entry_price_sol,
                entry_sol=existing.entry_sol,
                signature=existing.signature,
                sold=True,
            )
''',
        "E4 exit store update",
    )
    text = replace_once(
        text,
        '''            "counters": {
                key: value
                for key, value in vars(self.counters).items()
                if key != "latency_samples_us"
            },
''',
        '''            "counters": {
                "decisions": self.counters.decisions,
                "accepted": self.counters.accepted,
                "rejected": self.counters.rejected,
                "social_posts": self.counters.social_posts,
                "social_matches": self.counters.social_matches,
                "e4_entries": self.counters.e4_entries,
                "e4_unknown_creators_queued": self.counters.e4_unknown_creators_queued,
                "creator_promotions": self.counters.creator_promotions,
                "creator_demotions": self.counters.creator_demotions,
            },
''',
        "slot counter snapshot",
    )
    path.write_text(text, encoding="utf-8")
    print("patched V10 hot signal stores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
