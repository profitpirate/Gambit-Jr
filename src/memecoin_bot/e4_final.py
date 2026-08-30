from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import e4_live as core
from . import e4_production as production  # applies V1.5 normalization and restart reconciliation

LOGGER = logging.getLogger("gambit.e4.final")


class BuilderWorker:
    def __init__(self, command: tuple[str, ...], timeout: float = 2.0):
        self.command = command
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.stderr_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            LOGGER.warning("E4 builder stderr: %s", line.decode(errors="replace").rstrip())

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                process.kill()
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None

    async def build(self, request: Mapping[str, Any]) -> str:
        async with self.lock:
            for attempt in range(2):
                await self.start()
                assert self.process and self.process.stdin and self.process.stdout
                try:
                    self.process.stdin.write(json.dumps(dict(request), separators=(",", ":")).encode() + b"\n")
                    await self.process.stdin.drain()
                    line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout)
                    if not line:
                        raise RuntimeError("persistent E4 builder closed stdout")
                    result = json.loads(line)
                    if result.get("error"):
                        raise RuntimeError(str(result["error"]))
                    if result.get("request_id") not in {None, request.get("request_id")}:
                        raise RuntimeError("persistent E4 builder response ID mismatch")
                    encoded = str(result["transaction_base64"])
                    base64.b64decode(encoded, validate=True)
                    return encoded
                except Exception:
                    await self.stop()
                    if attempt:
                        raise
            raise RuntimeError("E4 builder exhausted retries")


class BuilderPool:
    """Two persistent local builders remove per-order Node startup from the hot path."""

    def __init__(self, command: tuple[str, ...]):
        if not command:
            raise ValueError("E4 builder command is empty")
        count = max(1, min(2, int(os.getenv("E4_BUILDER_WORKERS", "2"))))
        timeout = float(os.getenv("E4_BUILDER_RESPONSE_TIMEOUT_SECONDS", "2"))
        self.workers = [BuilderWorker(command, timeout) for _ in range(count)]
        self.available: asyncio.Queue[BuilderWorker] = asyncio.Queue()
        for worker in self.workers:
            self.available.put_nowait(worker)

    async def build(self, request: Mapping[str, Any]) -> str:
        worker = await self.available.get()
        try:
            return await worker.build(request)
        finally:
            self.available.put_nowait(worker)

    async def close(self) -> None:
        await asyncio.gather(*(worker.stop() for worker in self.workers), return_exceptions=True)


# The final runtime always uses persistent builders.
core.Builder = BuilderPool


# Live startup must tail only future canonical events. Replaying an existing operational DB into a
# funded wallet is never allowed unless an operator explicitly opts into it for an isolated test.
_previous_discover = core.SQLiteEventSource._discover


def _tail_discover(self: core.SQLiteEventSource) -> tuple[str, str]:
    table, column = _previous_discover(self)
    if not getattr(self, "_e4_tail_initialized", False):
        if not core._bool("E4_CONSUME_EXISTING_EVENTS", False):
            row = self._connect().execute(f'SELECT COALESCE(MAX("{column}"),0) FROM "{table}"').fetchone()
            self.last_id = max(self.last_id, int(row[0] or 0))
        self._e4_tail_initialized = True
    return table, column


core.SQLiteEventSource._discover = _tail_discover


_original_engine_init = core.Engine.__init__


def _engine_init(self: core.Engine, settings: core.Settings) -> None:
    _original_engine_init(self, settings)
    self.allocation_lock = asyncio.Lock()
    self.reserved_sol = 0.0


core.Engine.__init__ = _engine_init


async def _token_balance_after_change(
    rpc: core.Rpc,
    wallet: str,
    mint: str,
    previous: float,
    direction: str,
    timeout: float = 1.5,
) -> float:
    deadline = time.monotonic() + timeout
    latest = previous
    while time.monotonic() < deadline:
        latest = await rpc.token_balance(wallet, mint)
        if (direction == "up" and latest > previous) or (direction == "down" and latest < previous):
            return latest
        await asyncio.sleep(0.025)
    return latest


class RebroadcastRouteSender(core.RouteSender):
    """Rebroadcasts the exact same signature; it never creates an independent duplicate order."""

    async def _status(self, signature: str) -> tuple[bool, int | None, str | None]:
        try:
            result = await self.rpc.call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": False}],
            )
            status = (result.get("value") or [None])[0]
            if not status:
                return False, None, None
            if status.get("err") is not None:
                return False, status.get("slot"), json.dumps(status["err"], default=str)
            if status.get("confirmationStatus") in {"processed", "confirmed", "finalized"}:
                return True, status.get("slot"), None
        except Exception as exc:
            return False, None, str(exc)
        return False, None, None

    async def submit(
        self,
        tx: str,
        signature: str,
    ) -> tuple[str, bool, int | None, str | None, list[core.RouteResult]]:
        rounds = max(1, min(8, int(os.getenv("E4_REBROADCAST_ROUNDS", "4"))))
        interval = max(0.025, float(os.getenv("E4_REBROADCAST_INTERVAL_SECONDS", "0.12")))
        deadline = time.monotonic() + self.settings.confirmation_timeout_seconds
        all_results: list[core.RouteResult] = []
        first_route = "NONE"
        last_error: str | None = None

        for round_index in range(rounds):
            batch = await asyncio.gather(
                *(
                    self._send(index, f"{name}#{round_index + 1}", url, tx, signature)
                    for index, (name, url) in enumerate(self.routes)
                )
            )
            all_results.extend(batch)
            accepted = [item for item in batch if item.accepted]
            if accepted and first_route == "NONE":
                first_route = min(accepted, key=lambda item: item.completed_ns).name.split("#", 1)[0]
            confirmed, slot, error = await self._status(signature)
            last_error = error or last_error
            if confirmed:
                return first_route, True, slot, None, all_results
            if error and slot is not None:
                return first_route, False, slot, error, all_results
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(interval)

        remaining = max(0.0, deadline - time.monotonic())
        if remaining:
            confirmed, slot, error = await self.rpc.confirm(signature, remaining)
            return first_route, confirmed, slot, error or last_error, all_results
        return first_route, False, None, last_error or "confirmation timeout", all_results


core.RouteSender = RebroadcastRouteSender


async def _execute_buy(self: core.Engine, state: core.TokenState, score: float, fraction: float, reason: str) -> None:
    mint = state.mint
    reserved = 0.0
    try:
        if self.store.has_entered(mint):
            return
        before_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
        async with self.allocation_lock:
            balance = await self.rpc.balance(self.signer.wallet)
            priority, tip = self.fee_bid(balance * fraction, score)
            deployable = balance - self.settings.reserve_sol - self.reserved_sol - priority - tip
            amount = min(deployable * min(fraction, self.settings.max_position_fraction), self.settings.max_position_sol)
            if amount < self.settings.min_position_sol:
                return
            reserved = amount + priority + tip
            self.reserved_sol += reserved
            if not self.store.mark_entry(mint, score, reason):
                self.reserved_sol -= reserved
                reserved = 0.0
                return

        request_id = str(uuid.uuid4())
        request = {
            "request_id": request_id,
            "side": "BUY",
            "mint": mint,
            "public_key": self.signer.wallet,
            "amount": amount,
            "denominated_in_sol": True,
            "slippage_bps": self.settings.buy_slippage_bps,
            "priority_fee_sol": priority,
            "tip_sol": tip,
            "pool": "pump",
            "metadata": {"score": score, "reason": reason, "fdv_usd": state.fdv_usd},
        }
        self.store.order(request_id, mint, "BUY", amount, None, reason)
        signature, confirmed, _, error = await self.execute(request_id, request)
        if not confirmed:
            LOGGER.error("E4 buy failed mint=%s signature=%s error=%s", mint, signature, error)
            return

        after_tokens = await _token_balance_after_change(
            self.rpc, self.signer.wallet, mint, before_tokens, "up"
        )
        received = max(0.0, after_tokens - before_tokens)
        if received <= 0:
            raise RuntimeError("E4 buy landed but token balance did not become observable")
        entry_price = amount / received
        position = core.Position(
            position_id=str(uuid.uuid4()),
            mint=mint,
            status=core.PositionStatus.OPEN,
            opened_ns=time.time_ns(),
            entry_sol=amount,
            tokens=received,
            remaining=received,
            entry_price=entry_price,
            max_price=state.price_sol or entry_price,
            last_price=state.price_sol or entry_price,
            entry_signature=signature,
        )
        self.positions[mint] = position
        self.store.save_position(position)
        LOGGER.info("E4 position opened mint=%s amount_sol=%.9f signature=%s", mint, amount, signature)
    except Exception:
        LOGGER.exception("E4 buy execution error mint=%s", mint)
    finally:
        if reserved:
            async with self.allocation_lock:
                self.reserved_sol = max(0.0, self.reserved_sol - reserved)
        self.pending_entries.discard(mint)


core.Engine.execute_buy = _execute_buy


async def _retry_residual(self: core.Engine, position: core.Position) -> None:
    await asyncio.sleep(0.025)
    if position.mint not in self.positions or position.mint in self.pending_exits:
        return
    self.pending_exits.add(position.mint)
    self.spawn(self.execute_sell(position, 1.0, "E4 residual liquidation"))


async def _execute_sell(self: core.Engine, position: core.Position, fraction: float, reason: str) -> None:
    mint = position.mint
    retry_residual = False
    try:
        live_tokens = await self.rpc.token_balance(self.signer.wallet, mint)
        amount = min(live_tokens, position.remaining) * min(1.0, max(0.0, fraction))
        dust = max(1e-9, position.tokens * 1e-8)
        if live_tokens <= dust or amount <= 0:
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            self.positions.pop(mint, None)
            self.store.save_position(position)
            self.spawn(self.sweep())
            return

        urgent = fraction >= 0.999 or any(term in reason.lower() for term in ("failure", "broke", "liquidation"))
        priority, tip = self.fee_bid(position.entry_sol * fraction, 1.0, urgent)
        request_id = str(uuid.uuid4())
        request = {
            "request_id": request_id,
            "side": "SELL",
            "mint": mint,
            "public_key": self.signer.wallet,
            "amount": amount,
            "denominated_in_sol": False,
            "slippage_bps": self.settings.sell_slippage_bps,
            "priority_fee_sol": priority,
            "tip_sol": tip,
            "pool": "auto",
            "metadata": {"fraction": fraction, "reason": reason, "urgent": urgent},
        }
        self.store.order(request_id, mint, "SELL", amount, fraction, reason)
        before_sol = await self.rpc.balance(self.signer.wallet)
        position.status = core.PositionStatus.EXITING
        self.store.save_position(position)
        signature, confirmed, _, error = await self.execute(request_id, request)
        if not confirmed:
            position.status = core.PositionStatus.PARTIAL if position.first_partial_done else core.PositionStatus.OPEN
            self.store.save_position(position)
            LOGGER.error("E4 sell failed mint=%s signature=%s error=%s", mint, signature, error)
            return

        after_tokens = await _token_balance_after_change(
            self.rpc, self.signer.wallet, mint, live_tokens, "down"
        )
        after_sol = await self.rpc.balance(self.signer.wallet)
        sold = max(0.0, live_tokens - after_tokens)
        if sold <= 0:
            raise RuntimeError("E4 sell landed but no token balance reduction became observable")
        position.remaining = min(max(0.0, position.remaining - sold), after_tokens)
        position.realized_sol += after_sol - before_sol
        position.close_signature = signature
        if not position.first_partial_done and fraction < 0.999:
            position.first_partial_done = True
            position.first_partial_fraction = sold / live_tokens if live_tokens else fraction

        if after_tokens <= dust:
            position.remaining = 0.0
            position.status = core.PositionStatus.CLOSED
            self.positions.pop(mint, None)
            self.store.save_position(position)
            self.spawn(self.sweep())
        else:
            position.status = core.PositionStatus.PARTIAL
            self.store.save_position(position)
            retry_residual = fraction >= 0.999
        LOGGER.info("E4 exit executed mint=%s fraction=%.4f signature=%s", mint, fraction, signature)
    except Exception:
        LOGGER.exception("E4 sell execution error mint=%s", mint)
        position.status = core.PositionStatus.PARTIAL if position.first_partial_done else core.PositionStatus.OPEN
        self.store.save_position(position)
    finally:
        self.pending_exits.discard(mint)

    if retry_residual:
        self.spawn(_retry_residual(self, position))


core.Engine.execute_sell = _execute_sell


async def _guardian(self: core.Engine) -> None:
    interval = max(0.005, float(os.getenv("E4_GUARDIAN_INTERVAL_SECONDS", "0.01")))
    while not self.stop_event.is_set():
        for mint, position in tuple(self.positions.items()):
            if mint in self.pending_exits:
                continue
            if position.age_ms >= self.settings.max_hold_ms:
                self.pending_exits.add(mint)
                self.store.decision(
                    mint,
                    None,
                    "SELL_ALL",
                    None,
                    "E4 observed absolute hold horizon",
                    {"fraction": 1.0, "guardian": True},
                )
                self.spawn(self.execute_sell(position, 1.0, "E4 observed absolute hold horizon"))
        await asyncio.sleep(interval)


async def _final_run(self: core.Engine) -> None:
    await production._reconcile(self)
    guardian = asyncio.create_task(_guardian(self))
    try:
        async for event in self.source.events():
            if self.stop_event.is_set():
                break
            try:
                await self.on_event(event)
            except Exception:
                LOGGER.exception("E4 event failure mint=%s event_id=%s", event.mint, event.event_id)
    finally:
        self.stop_event.set()
        guardian.cancel()
        await asyncio.gather(guardian, return_exceptions=True)
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        close = getattr(self.builder, "close", None)
        if close is not None:
            await close()


core.Engine.run = _final_run


def build_parser() -> argparse.ArgumentParser:
    return core.build_parser()


def main() -> None:
    args = build_parser().parse_args()
    settings = core.Settings.from_env()
    logging.basicConfig(
        level=os.getenv("E4_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.getenv("E4_BUILDER_COMMAND"):
        settings.builder_command = ("node", "tools/e4-builder/daemon.mjs")

    if args.command == "migrate":
        core.Store(settings.execution_db).close()
        print(json.dumps({"migrated": True, "database": str(settings.execution_db)}))
        return
    if args.command == "status":
        store = core.Store(settings.execution_db)
        try:
            print(json.dumps(store.status(), indent=2))
        finally:
            store.close()
        return
    if args.command != "run":
        raise SystemExit(f"unsupported E4 command: {args.command}")
    if not args.live or not settings.live:
        raise SystemExit("live E4 execution requires both E4_LIVE=true and --live")
    settings.validate()
    asyncio.run(core.run_engine(settings))


if __name__ == "__main__":
    main()
