"""One explicitly confirmed wallet operation, using the live lock and durable journal.

The signer remains inside the VPS. An interrupted/unknown submission is never rebuilt.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import aiohttp

from .v12_execution_journal import ExecutionJournal
from .v12_prearmed import Notices, local_readiness, settings
from .v12_prearmed_diagnostics import failure
from .v12_prearmed_io import BuilderLanes, LiveExecution, Rpc, native_feed
from .v12_prearmed_runtime import Runtime, State, reserve_price


def assert_flat(state, journal):
    if state.data["positions"] or state.data["pending"] or journal.recoverable():
        raise RuntimeError("WALLET_HAS_POSITIONS_OR_UNRESOLVED_TRANSACTIONS")


async def confirm_transfer(execution, entry, destination, amount):
    for _ in range(90):
        transaction = await execution.confirmed_transaction(entry.signature)
        if transaction:
            meta = transaction.get("meta") or {}
            if meta.get("err") is not None:
                execution.journal.mark_failed(
                    entry.idempotency_key, "CHAIN_REJECTED", terminal=True
                )
                return {
                    "state": "FAILED",
                    "message": "The chain rejected the transfer. No replacement transaction was sent.",
                    "evidence": {"signature": entry.signature, "fee_lamports": meta.get("fee")},
                }
            keys = [
                key["pubkey"] if isinstance(key, dict) else key
                for key in transaction["transaction"]["message"]["accountKeys"]
            ]
            if keys[0] != execution.wallet or destination not in keys:
                raise RuntimeError("TRANSFER_RECEIPT_ACCOUNT_MISMATCH")
            index = keys.index(destination)
            if meta["postBalances"][index] - meta["preBalances"][index] != amount:
                raise RuntimeError("TRANSFER_RECEIPT_AMOUNT_MISMATCH")
            execution.journal.mark_confirmed(
                entry.idempotency_key, route="confirmed_rpc", slot=transaction["slot"]
            )
            return {
                "state": "COMPLETE",
                "message": "SOL transfer confirmed on chain. Trading remains stopped.",
                "evidence": {
                    "destination": destination,
                    "amount_lamports": amount,
                    "signature": entry.signature,
                    "fee_lamports": meta.get("fee"),
                    "slot": transaction["slot"],
                },
            }
        await asyncio.sleep(1)
    return {
        "state": "UNKNOWN",
        "message": "Transfer was submitted but not confirmed. Do not retry.",
        "evidence": {"signature": entry.signature},
    }


async def transfer(operation, execution, state, kill_path):
    from solders.hash import Hash
    from solders.message import MessageV0, to_bytes_versioned
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.system_program import TransferParams
    from solders.system_program import transfer as instruction
    from solders.transaction import VersionedTransaction

    payload = operation["payload"]
    destination = Pubkey.from_string(payload["destination"])
    amount = payload["amount_lamports"]
    if (
        not destination.is_on_curve()
        or str(destination) == execution.wallet
        or type(amount) is not int
        or amount <= 0
    ):
        raise RuntimeError("INVALID_TRANSFER")
    prior = execution.journal.by_request(operation["id"])
    if prior:
        if prior.payload != {
            "side": "TRANSFER",
            "destination": str(destination),
            "amount_lamports": amount,
        }:
            raise RuntimeError("TRANSFER_INTENT_MISMATCH")
        if prior.signature:
            return await confirm_transfer(execution, prior, str(destination), amount)
        raise RuntimeError("TRANSFER_INTENT_ALREADY_CLAIMED")
    assert_flat(state, execution.journal)
    balance = await execution.rpc.balance_lamports(execution.wallet)
    block = await execution.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
    message = MessageV0.try_compile(
        Pubkey.from_string(execution.wallet),
        [
            instruction(
                TransferParams(
                    from_pubkey=Pubkey.from_string(execution.wallet),
                    to_pubkey=destination,
                    lamports=amount,
                )
            )
        ],
        [],
        Hash.from_string(block["value"]["blockhash"]),
    )
    fee = (
        await execution.rpc.call(
            "getFeeForMessage",
            [base64.b64encode(to_bytes_versioned(message)).decode(), {"commitment": "confirmed"}],
        )
    )["value"]
    if type(fee) is not int or fee > 50_000 or balance - amount - fee < 30_000_000:
        raise RuntimeError("TRANSFER_WOULD_VIOLATE_FEE_CAP_OR_RESERVE")
    unsigned = base64.b64encode(
        bytes(VersionedTransaction.populate(message, [Signature.default()]))
    ).decode()
    simulated = await execution.rpc.call(
        "simulateTransaction",
        [unsigned, {"encoding": "base64", "sigVerify": False, "commitment": "confirmed"}],
    )
    if simulated["value"].get("err") is not None:
        raise RuntimeError("TRANSFER_SIMULATION_FAILED")
    if kill_path.exists():
        raise RuntimeError("OPERATOR_STOP_REQUESTED")
    entry = execution.journal.prepare(
        operation["id"],
        {"side": "TRANSFER", "destination": str(destination), "amount_lamports": amount},
    )
    signed, signature = await execution.signer.sign(unsigned)
    if kill_path.exists():
        execution.journal.mark_failed(
            entry.idempotency_key, "OPERATOR_STOP_REQUESTED", terminal=True
        )
        raise RuntimeError("OPERATOR_STOP_REQUESTED")
    execution.journal.mark_signed(entry.idempotency_key, signed_tx_b64=signed, signature=signature)
    execution.journal.mark_submitted(entry.idempotency_key)
    for endpoint in execution.rpc.urls:
        try:
            await execution.rpc.call(
                "sendTransaction",
                [signed, {"encoding": "base64", "skipPreflight": False, "maxRetries": 2}],
                url=endpoint,
            )
        except RuntimeError:
            pass  # Rpc records each failed route; exactly the same signed bytes are reused.
    return await confirm_transfer(
        execution, execution.journal.by_request(operation["id"]), str(destination), amount
    )


async def live_test(operation, execution, runtime, session, kill_path):
    assert_flat(runtime.state, execution.journal)
    maximum = operation["payload"].get("maximum_total_lamports")
    if maximum != 10_000_000:
        raise RuntimeError("INVALID_TEST_BUDGET")
    # A durable once-only intent prevents restart from selecting a second token.
    row = runtime.state.data.setdefault("execution_tests", {}).get(operation["id"])
    if row:
        raise RuntimeError("EXECUTION_TEST_ALREADY_CLAIMED")
    runtime.state.data["execution_tests"][operation["id"]] = {
        "started_ns": time.time_ns(),
        "state": "SELECTING",
    }
    runtime.state.save()
    candidate = asyncio.Future()
    stop = asyncio.Event()

    async def observe(signature, events, observed_ns):
        if candidate.done():
            return
        for event in events:
            if (
                event.get("anchor_event") == "CreateEvent"
                and event.get("is_mayhem_mode") is False
                and event.get("quote_mint")
                in (
                    None,
                    "11111111111111111111111111111111",
                    "So11111111111111111111111111111111111111112",
                )
            ):
                candidate.set_result(event)
                return

    feed = asyncio.create_task(
        native_feed(
            session,
            execution.rpc.urls,
            observe,
            lambda value: None,
            stop,
            runtime.diagnostics.record,
        )
    )
    try:
        event = await asyncio.wait_for(candidate, 60)
    finally:
        stop.set()
        feed.cancel()
        await asyncio.gather(feed, return_exceptions=True)
    mint = event["mint"]
    launch = reserve_price(event)
    payload = {
        "side": "BUY",
        "mint": mint,
        "creator": event["creator"],
        "budget_lamports": "7005000",
        "public_key": execution.wallet,
        "launch_price": launch,
        "submitted_ns": time.time_ns(),
    }
    quote = await execution.builder.request({**payload, "action": "QUOTE"})
    payload["expected_tokens_raw"] = quote["quoted_tokens_raw"]
    payload["expires_ns"] = time.time_ns() + 15_000_000_000
    execution.allow_buy = lambda: not kill_path.exists() and time.time_ns() <= payload["expires_ns"]
    runtime.diagnostics.record(
        "LIVE_TEST_STARTED",
        stage="execution_test",
        reason="OWNER_AUTHORIZED_FIXED_BUDGET",
        mint=mint,
        operation_id=operation["id"],
        budget_lamports=maximum,
    )
    request = runtime.order(
        payload,
        {
            "reference_price": quote["curve_price"],
            "launch_price": launch,
            "creator": event["creator"],
            "test_operation_id": operation["id"],
        },
        reason="OWNER_EXECUTION_TEST",
    )
    await asyncio.gather(*list(runtime.tasks))
    deadline = time.monotonic() + 90
    while runtime.state.data["pending"] and time.monotonic() < deadline:
        await runtime.reconcile()
        await asyncio.sleep(1)
    buy = execution.journal.by_request(request)
    if runtime.state.data["pending"]:
        return {
            "state": "UNKNOWN",
            "message": "Buy outcome is unresolved. Its durable intent is retained; no second buy was sent.",
            "evidence": {"mint": mint, "buy_signature": buy.signature},
        }
    if mint not in runtime.state.data["positions"]:
        return {
            "state": "FAILED",
            "message": "The test buy did not complete. No second buy was attempted.",
            "evidence": {"mint": mint, "buy_signature": buy.signature, "reason": buy.error},
        }
    position = runtime.state.data["positions"][mint]
    position["forced_exit"] = True
    runtime.state.save()
    sell_id = runtime.order(
        {
            "side": "SELL",
            "mint": mint,
            "creator": position["creator"],
            "tokens_raw": str(position["remaining"]),
            "maximum_debit_lamports": "2995000",
            "submitted_ns": time.time_ns(),
        },
        {},
        reason="OWNER_EXECUTION_TEST_EXIT",
    )
    await asyncio.gather(*list(runtime.tasks))
    deadline = time.monotonic() + 90
    while runtime.state.data["pending"] and time.monotonic() < deadline:
        await runtime.reconcile()
        await asyncio.sleep(1)
    sell = execution.journal.by_request(sell_id)
    evidence = {
        "mint": mint,
        "wallet": execution.wallet,
        "maximum_total_lamports": maximum,
        "buy_signature": buy.signature,
        "sell_signature": sell.signature,
        "pending_orders": len(runtime.state.data["pending"]),
        "open_positions": len(runtime.state.data["positions"]),
    }
    if runtime.state.data["positions"] or runtime.state.data["pending"]:
        return {
            "state": "UNKNOWN",
            "message": "The buy landed but a complete exit is not confirmed. Position and receipts remain in the live recovery database. Immediate attention required.",
            "evidence": evidence,
        }
    balances = await execution.rpc.call(
        "getTokenAccountsByOwner",
        [execution.wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    residual = sum(
        int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        for row in balances["value"]
    )
    evidence["residual_tokens_raw"] = residual
    receipts = []
    for entry in (buy, sell):
        transaction = await execution.confirmed_transaction(entry.signature)
        if not transaction or transaction["meta"].get("err") is not None:
            raise RuntimeError("TEST_RECEIPT_RECHECK_FAILED")
        meta = transaction["meta"]
        receipts.append(
            {
                "signature": entry.signature,
                "slot": transaction["slot"],
                "fee_lamports": meta["fee"],
                "wallet_sol_delta_lamports": meta["postBalances"][0] - meta["preBalances"][0],
            }
        )
    evidence["receipts"] = receipts
    evidence["net_wallet_cost_lamports"] = -sum(r["wallet_sol_delta_lamports"] for r in receipts)
    passed = residual == 0 and evidence["net_wallet_cost_lamports"] <= maximum
    runtime.state.data["execution_tests"][operation["id"]] = {
        "state": "COMPLETE" if passed else "FAILED",
        **evidence,
    }
    runtime.state.save()
    return {
        "state": "COMPLETE" if passed else "FAILED",
        "message": "One real buy and full sell confirmed; residual token balance checked. Organic trading remains stopped."
        if passed
        else "Execution receipts failed the residual-balance or total-budget check.",
        "evidence": evidence,
    }


async def run(operation_id):
    from .v12_security import TamperEvidentAuditLog, decode_secret_key

    if not re.fullmatch("[a-f0-9]{32}", operation_id):
        raise ValueError("INVALID_OPERATION_ID")
    root, data, ops = Path("/app"), Path("/data"), Path("/operations")
    request = json.loads((ops / (operation_id + ".request.json")).read_text())
    if request.get("id") != operation_id or request.get("action") not in {"transfer", "live_test"}:
        raise ValueError("INVALID_OPERATION")
    config = settings(root, data, "live")
    _, lock = local_readiness(config, root, Path(os.environ["V12_FROZEN_MODEL_PATH"]), live=True)
    state = None
    builder = BuilderLanes(config.builder_command)
    kill_path = Path(os.environ["V12_KILL_SWITCH_PATH"])
    try:
        state = State(config.execution_db, wallet=config.wallet, mode="live")
        journal = ExecutionJournal(state.conn)
        audit = TamperEvidentAuditLog(
            data / "live-audit.jsonl", decode_secret_key(os.environ["V12_AUDIT_HMAC_KEY"])
        )
        if not audit.verify():
            raise RuntimeError("AUDIT_CHAIN_INVALID")
        async with aiohttp.ClientSession() as session:
            rpc = Rpc(session, (config.rpc_url, *config.fallback_rpcs))
            execution = LiveExecution(settings=config, rpc=rpc, builder=builder, journal=journal)
            runtime = Runtime(
                model=json.loads(Path(os.environ["V12_FROZEN_MODEL_PATH"]).read_text()),
                state=state,
                rpc=rpc,
                builder=builder,
                execution=execution,
                wallet=config.wallet,
                metadata_session=None,
                emit=audit.append,
                kill_path=kill_path,
            )
            rpc.trace = runtime.diagnostics.record
            genesis = await rpc.call("getGenesisHash", [])
            if genesis != "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d":
                raise RuntimeError("RPC_IS_NOT_MAINNET")
            notices = Notices(session)
            await notices.send(
                "[TEST] Gambit owner-authorized execution test starting. Maximum total budget: 0.01 SOL."
                if request["action"] == "live_test"
                else "[ACTION] Owner-confirmed SOL transfer starting."
            )
            result = await (
                live_test(request, execution, runtime, session, kill_path)
                if request["action"] == "live_test"
                else transfer(request, execution, state, kill_path)
            )
            audit.append(
                "WALLET_OPERATION_RESULT",
                {
                    "operation_id": operation_id,
                    "action": request["action"],
                    "state": result["state"],
                },
            )
            try:
                await notices.send(
                    ("[SUCCESS] " if result["state"] == "COMPLETE" else "[URGENT] ")
                    + result["message"]
                )
            except Exception:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
                audit.append("NOTIFICATION_DELIVERY_FAILED", {"operation_id": operation_id})
    except Exception as error:  # noqa: BLE001 - Boundary records failure and preserves recovery state.
        outstanding = bool(
            state
            and (
                state.data["positions"]
                or state.data["pending"]
                or ExecutionJournal(state.conn).recoverable()
            )
        )
        result = {
            "state": "UNKNOWN" if outstanding else "FAILED",
            "message": "Wallet operation did not produce complete proof. Inspect recorded orders before retrying.",
            "evidence": failure("wallet_operation", error),
        }
    finally:
        kill_path.touch(mode=0o600)
        await builder.close()
        if state:
            state.conn.close()
        lock.release()
    temp = ops / (operation_id + ".result.tmp")
    temp.write_text(json.dumps(result, indent=2))
    temp.replace(ops / (operation_id + ".result.json"))
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation_id")
    args = parser.parse_args()
    asyncio.run(run(args.operation_id))


if __name__ == "__main__":
    main()
