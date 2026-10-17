"""Prove an ordinary recent-blockhash transaction expired without landing.

Read-only RPCs only. An outage, non-null status, receipt, malformed response or
durable nonce can never authorize clearing an unresolved financial operation.
"""
import asyncio
import base64
import time
from urllib.parse import urlsplit


async def expired_unseen(entry, rpc, *, now_ns=None):
    now_ns = time.time_ns() if now_ns is None else now_ns
    submitted = entry.payload.get("submitted_ns")
    if (type(submitted) is not int or now_ns - submitted < 180_000_000_000
            or not entry.signature or not entry.signed_tx_b64):
        return None
    try:
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(entry.signed_tx_b64, validate=True))
        if str(tx.signatures[0]) != entry.signature or not all(tx.verify_with_results()):
            return None
        message = tx.message
        # This builder emits a ComputeBudget instruction first. A durable nonce
        # transaction must advance its nonce first and is explicitly unsupported.
        if (not message.instructions or
                str(message.account_keys[message.instructions[0].program_id_index]) !=
                "ComputeBudget111111111111111111111111111111"):
            return None
        blockhash = str(message.recent_blockhash)
    except (ValueError, TypeError, IndexError):
        return None
    endpoints = []
    hosts = set()
    for url in rpc.urls:
        host = urlsplit(url).hostname
        if host and host not in hosts:
            endpoints.append(url)
            hosts.add(host)
    if len(endpoints) < 2:
        return None

    async def check(index, url):
        try:
            expiry = await rpc.call("isBlockhashValid", [blockhash, {"commitment": "finalized"}], url=url)
            slot = expiry.get("context", {}).get("slot")
            if expiry.get("value") is not False or type(slot) is not int or slot <= 0:
                return None
            # Query history AFTER the finalized expiry observation. Absence
            # before expiry alone says nothing about a delayed broadcast.
            status = await rpc.call("getSignatureStatuses", [[entry.signature], {"searchTransactionHistory": True}], url=url)
            status_slot = status.get("context", {}).get("slot")
            if status.get("value") != [None] or type(status_slot) is not int or status_slot < slot:
                return None
            receipt = await rpc.call("getTransaction", [entry.signature, {
                "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 1}], url=url)
            if receipt is not None:
                return None
            return {"provider_index": index, "finalized_expiry_slot": slot, "history_slot": status_slot}
        except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, TimeoutError):
            return None

    checks = await asyncio.gather(*(check(i, url) for i, url in enumerate(endpoints)))
    verified = [c for c in checks if c is not None]
    # A third provider disagreeing is not silently outvoted. Unavailable readers
    # also leave the order unresolved until they can agree.
    if len(verified) != len(endpoints):
        return None
    return {"signature": entry.signature, "blockhash": blockhash,
            "observed_ns": now_ns, "checks": verified}
