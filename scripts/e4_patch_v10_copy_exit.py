#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch_pipelines() -> None:
    path = Path("src/memecoin_bot/e4_pipelines_v10.py")
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''@dataclass(frozen=True, slots=True)
class E4EntrySignal:
    mint: str
    creator: str
    observed_ns: int
    entry_price_sol: float
    entry_sol: float
    signature: str
    sold: bool = False
''',
        '''@dataclass(frozen=True, slots=True)
class E4EntrySignal:
    mint: str
    creator: str
    observed_ns: int
    entry_price_sol: float
    entry_sol: float
    signature: str
    entry_tokens: float = 0.0
    remaining_tokens: float = 0.0
    last_sell_fraction: float = 0.0
    last_sell_ns: int = 0
    sell_count: int = 0
    fully_exited: bool = False
    sold: bool = False
''',
        "E4 signal dataclass",
    )
    text = replace_once(
        text,
        '''            signature=str(payload.get("signature") or ""),
            sold=_truthy(payload.get("sold", False)),
        )
''',
        '''            signature=str(payload.get("signature") or ""),
            entry_tokens=max(0.0, _finite(payload.get("token_amount") or payload.get("tokens"))),
            remaining_tokens=max(0.0, _finite(payload.get("token_amount") or payload.get("tokens"))),
            last_sell_fraction=0.0,
            last_sell_ns=0,
            sell_count=0,
            fully_exited=_truthy(payload.get("fully_exited", False)),
            sold=_truthy(payload.get("sold", False)),
        )
''',
        "E4 entry construction",
    )
    start = text.index("    def observe_e4_exit(")
    end = text.index("    def queue_creator_discovery(", start)
    replacement = '''    def observe_e4_exit(
        self,
        mint: str,
        *,
        token_amount: float = 0.0,
        sell_fraction: float = 0.0,
        fully_exited: bool = False,
        observed_ns: int | None = None,
    ) -> None:
        existing = self._e4_entries.get(mint)
        if existing is None:
            return
        sold_tokens = max(0.0, _finite(token_amount))
        before = max(0.0, existing.remaining_tokens or existing.entry_tokens)
        fraction = min(1.0, max(0.0, _finite(sell_fraction)))
        if fraction <= 0 and sold_tokens > 0 and before > 0:
            fraction = min(1.0, sold_tokens / before)
        remaining = max(0.0, before - sold_tokens) if sold_tokens > 0 else before * (1.0 - fraction)
        complete = bool(
            fully_exited
            or fraction >= 0.985
            or (before > 0 and remaining <= max(1e-9, existing.entry_tokens * 1e-6))
        )
        with self._update_lock:
            self._e4_entry_store[mint] = E4EntrySignal(
                mint=existing.mint,
                creator=existing.creator,
                observed_ns=existing.observed_ns,
                entry_price_sol=existing.entry_price_sol,
                entry_sol=existing.entry_sol,
                signature=existing.signature,
                entry_tokens=existing.entry_tokens,
                remaining_tokens=0.0 if complete else remaining,
                last_sell_fraction=fraction,
                last_sell_ns=int(observed_ns or _now_ns()),
                sell_count=existing.sell_count + 1,
                fully_exited=complete,
                sold=True,
            )

    def e4_signal(self, mint: str) -> E4EntrySignal | None:
        return self._e4_entries.get(mint)

'''
    text = text[:start] + replacement + text[end:]
    text = text.replace(
        "if e4_signal and not e4_signal.sold:",
        "if e4_signal and not e4_signal.fully_exited and e4_signal.last_sell_ns == 0:",
    )
    path.write_text(text, encoding="utf-8")


def patch_runtime() -> None:
    path = Path("src/memecoin_bot/e4_pipeline_runtime_v10.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '''            self.pipelines.observe_e4_exit(str(payload.get("mint") or ""))
''',
        '''            self.pipelines.observe_e4_exit(
                str(payload.get("mint") or ""),
                token_amount=_float(payload.get("token_amount") or payload.get("tokens")),
                sell_fraction=_float(payload.get("sell_fraction") or payload.get("fraction")),
                fully_exited=bool(payload.get("fully_exited")),
                observed_ns=int(payload.get("observed_ns") or payload.get("received_ns") or time.time_ns()),
            )
''',
        1,
    )
    # Runtime previously had no local safe float helper.
    marker = '''def _token_totals(rows: list[Mapping[str, Any]], owner: str) -> dict[str, float]:
'''
    helper = '''def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


'''
    if helper not in text:
        text = text.replace(marker, helper + marker, 1)
    text = text.replace(
        '''                        "signature": signature,
                    }
                )
            else:
                self.pipelines.observe_e4_exit(mint)
''',
        '''                        "signature": signature,
                        "token_amount": delta,
                    }
                )
            else:
                self.pipelines.observe_e4_exit(
                    mint,
                    token_amount=abs(delta),
                    observed_ns=time.time_ns(),
                )
''',
        1,
    )
    path.write_text(text, encoding="utf-8")


def patch_hardening() -> None:
    path = Path("src/memecoin_bot/e4_hardening_v10.py")
    text = path.read_text(encoding="utf-8")
    # Patch direct state observer if present; final E2E applies it before this script.
    text = text.replace(
        '''                    "signature": event.signature,
                }
            )
        elif event.kind == core.EventKind.SELL.value:
            PIPELINES.observe_e4_exit(event.mint)
''',
        '''                    "signature": event.signature,
                    "token_amount": event.token_amount,
                }
            )
        elif event.kind == core.EventKind.SELL.value:
            PIPELINES.observe_e4_exit(
                event.mint,
                token_amount=event.token_amount,
                observed_ns=event.received_ns,
            )
''',
        1,
    )
    if "def _exit_v10" not in text:
        text = text.rstrip() + '''


_previous_exit_v10 = core.E4Policy.exit


def _exit_v10(self: core.E4Policy, position: core.Position, state: core.TokenState):
    profile = v6._PROFILE_BY_MINT.get(position.mint)
    if profile is not None and profile.family == "e4_confirmed_fast_copy":
        source = PIPELINES.e4_signal(position.mint)
        if source is not None:
            if source.fully_exited:
                return "SELL_FULL", 1.0, "E4 V10 copy source fully exited"
            if source.last_sell_ns > 0 and not position.first_partial_done:
                fraction = min(0.50, max(0.20, source.last_sell_fraction or profile.first_partial_fraction))
                return "SELL_PARTIAL", fraction, "E4 V10 copy source took first partial"
            if source.sell_count >= 2 and position.first_partial_done:
                return "SELL_FULL", 1.0, "E4 V10 copy source began second exit leg"
    return _previous_exit_v10(self, position, state)


core.E4Policy.exit = _exit_v10
'''
    path.write_text(text, encoding="utf-8")


def main() -> int:
    patch_pipelines()
    patch_runtime()
    patch_hardening()
    print("patched guarded E4 copy exits and source token accounting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
