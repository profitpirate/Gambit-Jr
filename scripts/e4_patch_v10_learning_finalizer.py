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
    marker = '''    def _reclassify_learned_creator(
'''
    method = '''    def finalize_stale_learning(
        self,
        *,
        now_ns: int | None = None,
        max_age_seconds: float = 300.0,
        quiet_seconds: float = 30.0,
    ) -> int:
        now = int(now_ns or _now_ns())
        max_age_ns = int(max_age_seconds * 1_000_000_000)
        quiet_ns = int(quiet_seconds * 1_000_000_000)
        candidates = [
            mint
            for mint, state in list(self._learning.items())
            if now - state.created_ns >= max_age_ns
            or (state.last_event_ns > 0 and now - state.last_event_ns >= quiet_ns)
        ]
        for mint in candidates:
            self.finalize_launch_learning(mint)
        return len(candidates)

'''
    if method not in text:
        text = replace_once(text, marker, method + marker, "learning finalizer method")
    pipeline.write_text(text, encoding="utf-8")

    runtime = Path("src/memecoin_bot/e4_pipeline_runtime_v10.py")
    rtext = runtime.read_text(encoding="utf-8")
    marker = '''    async def model_reload_worker(self) -> None:
'''
    method = '''    async def learning_finalize_worker(self) -> None:
        interval = max(1.0, float(os.getenv("E4_PIPELINE_LEARNING_SWEEP_SECONDS", "5")))
        max_age = max(30.0, float(os.getenv("E4_PIPELINE_LEARNING_MAX_AGE_SECONDS", "300")))
        quiet = max(5.0, float(os.getenv("E4_PIPELINE_LEARNING_QUIET_SECONDS", "30")))
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    self.pipelines.finalize_stale_learning(
                        max_age_seconds=max_age,
                        quiet_seconds=quiet,
                    )
                except Exception as exc:
                    self.record_error(exc)

'''
    if method not in rtext:
        rtext = replace_once(rtext, marker, method + marker, "learning runtime worker")
    rtext = replace_once(
        rtext,
        '''            tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.model_reload_worker(), name="e4-v10-model-reload")
            ]
''',
        '''            tasks: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.model_reload_worker(), name="e4-v10-model-reload"),
                asyncio.create_task(self.learning_finalize_worker(), name="e4-v10-learning-finalize"),
            ]
''',
        "learning worker startup",
    )
    runtime.write_text(rtext, encoding="utf-8")

    hardening = Path("src/memecoin_bot/e4_hardening_v10.py")
    htext = hardening.read_text(encoding="utf-8")
    htext = replace_once(
        htext,
        '''    elif event.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value}:
        PIPELINES.observe_trade_event(
            mint=event.mint,
            received_ns=event.received_ns,
            price_sol=event.price_sol or 0.0,
            is_buy=event.kind == core.EventKind.BUY.value,
        )
''',
        '''    elif event.kind in {core.EventKind.BUY.value, core.EventKind.SELL.value}:
        PIPELINES.observe_trade_event(
            mint=event.mint,
            received_ns=event.received_ns,
            price_sol=event.price_sol or 0.0,
            is_buy=event.kind == core.EventKind.BUY.value,
        )
    elif "COMPLETE" in str(event.kind).upper() or "MIGRAT" in str(event.kind).upper():
        PIPELINES.finalize_launch_learning(event.mint, completed=True)
''',
        "complete/migration finalization",
    )
    hardening.write_text(htext, encoding="utf-8")
    print("patched V10 continuous launch-learning finalization")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
