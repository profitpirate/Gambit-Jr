#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> int:
    pipeline = Path("src/memecoin_bot/e4_pipelines_v10.py")
    text = pipeline.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''    last_sell_fraction: float = 0.0
    last_sell_ns: int = 0
    sell_count: int = 0
''',
        '''    last_sell_fraction: float = 0.0
    last_sell_ns: int = 0
    last_sell_signature: str = ""
    sell_count: int = 0
''',
        "sell signature field",
    )
    text = replace_once(
        text,
        '''            last_sell_fraction=0.0,
            last_sell_ns=0,
            sell_count=0,
''',
        '''            last_sell_fraction=0.0,
            last_sell_ns=0,
            last_sell_signature="",
            sell_count=0,
''',
        "entry signal sell signature initialization",
    )
    text = replace_once(
        text,
        '''        with self._update_lock:
            self._e4_entry_store[mint] = signal
''',
        '''        existing = self._e4_entries.get(mint)
        if existing is not None and signal.signature and existing.signature == signal.signature:
            return existing
        with self._update_lock:
            self._e4_entry_store[mint] = signal
''',
        "entry signature dedup",
    )
    text = replace_once(
        text,
        '''        observed_ns: int | None = None,
    ) -> None:
        existing = self._e4_entries.get(mint)
        if existing is None:
            return
''',
        '''        observed_ns: int | None = None,
        signature: str = "",
    ) -> None:
        existing = self._e4_entries.get(mint)
        if existing is None:
            return
        if signature and existing.last_sell_signature == signature:
            return
''',
        "sell signature argument and dedup",
    )
    text = replace_once(
        text,
        '''                last_sell_fraction=fraction,
                last_sell_ns=int(observed_ns or _now_ns()),
                sell_count=existing.sell_count + 1,
''',
        '''                last_sell_fraction=fraction,
                last_sell_ns=int(observed_ns or _now_ns()),
                last_sell_signature=str(signature or existing.last_sell_signature),
                sell_count=existing.sell_count + 1,
''',
        "sell signature persistence",
    )
    text = text.replace(
        '"active_e4_entries": sum(not item.sold for item in self._e4_entries.values()),',
        '"active_e4_entries": sum(not item.fully_exited for item in self._e4_entries.values()),',
    )
    pipeline.write_text(text, encoding="utf-8")

    runtime = Path("src/memecoin_bot/e4_pipeline_runtime_v10.py")
    rtext = runtime.read_text(encoding="utf-8")
    rtext = rtext.replace(
        '''                observed_ns=int(payload.get("observed_ns") or payload.get("received_ns") or time.time_ns()),
            )
''',
        '''                observed_ns=int(payload.get("observed_ns") or payload.get("received_ns") or time.time_ns()),
                signature=str(payload.get("signature") or ""),
            )
''',
        1,
    )
    rtext = rtext.replace(
        '''                    observed_ns=time.time_ns(),
                )
''',
        '''                    observed_ns=time.time_ns(),
                    signature=signature,
                )
''',
        1,
    )
    runtime.write_text(rtext, encoding="utf-8")

    hardening = Path("src/memecoin_bot/e4_hardening_v10.py")
    htext = hardening.read_text(encoding="utf-8")
    htext = htext.replace(
        '''                observed_ns=event.received_ns,
            )
''',
        '''                observed_ns=event.received_ns,
                signature=str(event.signature or ""),
            )
''',
        1,
    )
    hardening.write_text(htext, encoding="utf-8")
    print("patched E4 multi-provider signal idempotency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
