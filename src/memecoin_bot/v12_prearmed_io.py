"""Read-only feed, unsigned builder, and journaled execution for the frozen runner.

Constructing LiveExecution requires explicit live settings. Shadow mode never
constructs it and never opens the signing key. Network errors are redacted.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs
from .v12_execution_journal import ExecutionJournal


class Rpc:
    def __init__(self, session, urls):
        self.session, self.urls = session, tuple(dict.fromkeys(urls))
        if not self.urls or any(urlsplit(u).scheme != 'https' for u in self.urls):
            raise ValueError('HTTPS RPC endpoints required')

    async def call(self, method, params, *, url=None):
        for endpoint in ([url] if url else self.urls):
            try:
                async with self.session.post(endpoint, json={'jsonrpc': '2.0', 'id': 1,
                        'method': method, 'params': params}, timeout=aiohttp.ClientTimeout(total=8)) as response:
                    response.raise_for_status()
                    result = await response.json()
                    if 'error' not in result and 'result' in result:
                        return result['result']
            except (aiohttp.ClientError, TimeoutError, ValueError):
                continue
        raise RuntimeError('RPC_UNAVAILABLE:' + method.upper())

    async def balance_lamports(self, wallet):
        result = await self.call('getBalance', [wallet, {'commitment': 'confirmed'}])
        value = result['value']
        if type(value) is not int or value < 0:
            raise RuntimeError('INVALID_WALLET_BALANCE')
        return value


class Builder:
    def __init__(self, command):
        self.command = tuple(command)
        self.process = None
        self.lock = asyncio.Lock()

    async def request(self, payload):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(*self.command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            request_id = str(uuid.uuid4())
            self.process.stdin.write((json.dumps({**payload, 'request_id': request_id}) + '\n').encode())
            try:
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), 20)
                result = json.loads(line)
                if result.get('request_id') != request_id:
                    raise RuntimeError('BUILDER_RESPONSE_MISMATCH')
                if result.get('error'):
                    raise RuntimeError('BUILDER:' + str(result['error']))
                return result['result']
            except (TimeoutError, ValueError, BrokenPipeError):
                await self.close()
                raise RuntimeError('BUILDER_UNAVAILABLE') from None

    async def close(self):
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


def receipt(transaction, *, wallet, mint, side, submitted_ns):
    """Use one confirmed transaction, never differences between unrelated RPC reads."""
    meta = transaction.get('meta') or {}
    if meta.get('err') is not None:
        return {'status': 'FAILED', 'reason': 'CHAIN_REJECTED'}
    keys = transaction['transaction']['message']['accountKeys']
    keys = [k['pubkey'] if isinstance(k, dict) else k for k in keys]
    if keys[0] != wallet:
        raise RuntimeError('UNEXPECTED_TRANSACTION_PAYER')
    def tokens(rows):
        total = 0
        for row in rows:
            if row.get('owner') == wallet and row.get('mint') == mint:
                amount = row['uiTokenAmount']
                if amount['decimals'] != 6:
                    raise RuntimeError('UNEXPECTED_TOKEN_DECIMALS')
                total += int(amount['amount'])
        return total
    before = tokens(meta.get('preTokenBalances', []))
    after = tokens(meta.get('postTokenBalances', []))
    delta = after - before
    if (side == 'BUY' and delta <= 0) or (side == 'SELL' and delta >= 0):
        raise RuntimeError('CONFIRMED_TRANSACTION_WITHOUT_EXPECTED_TOKEN_DELTA')
    reference = None
    if side == 'BUY':
        matches = [e for e in anchor_events_from_logs(meta.get('logMessages', []), PUMP_PROGRAM_ID)
                   if e.get('anchor_event') == 'TradeEvent' and e.get('mint') == mint
                   and e.get('user') == wallet and e.get('is_buy') is True]
        if len(matches) == 1:
            event = matches[0]
            pre_sol = event['virtual_sol_reserves'] - event['sol_amount']
            pre_tokens = event['virtual_token_reserves'] + event['token_amount']
            if pre_sol > 0 and pre_tokens > 0:
                reference = pre_sol / pre_tokens / 1000
    def wrapped(rows):
        return sum(int(r['uiTokenAmount']['amount']) for r in rows if r.get('owner') == wallet
                   and r.get('mint') == 'So11111111111111111111111111111111111111112')
    sol_delta = (int(meta['postBalances'][0]) - int(meta['preBalances'][0])
                 + wrapped(meta.get('postTokenBalances', [])) - wrapped(meta.get('preTokenBalances', [])))
    return {'status': 'CONFIRMED', 'tokens_raw': abs(delta),
            'sol_delta_lamports': sol_delta,
            'reference_price': reference, 'submitted_ns': submitted_ns,
            'confirmed_ns': time.time_ns(), 'slot': transaction['slot']}


class LiveExecution:
    def __init__(self, *, settings, rpc, builder, journal):
        if settings.live is not True:
            raise PermissionError('LIVE_EXECUTION_DISABLED')
        from .e4_live import Signer
        self.signer = Signer(settings)
        self.rpc, self.builder, self.journal = rpc, builder, journal
        self.wallet = settings.wallet
        self.allow_buy = lambda: False  # The runner must attach its current stop/feed/safety checks.

    async def execute(self, request_id, payload):
        entry = self.journal.prepare(request_id, payload)
        if entry.state == 'FAILED_TERMINAL':
            return {'status': 'FAILED', 'reason': 'PREVIOUS_FAILURE'}
        if entry.signature:
            return await self.resolve(entry, wait_seconds=1)
        try:
            if payload['side'] == 'BUY' and not self.allow_buy():
                raise RuntimeError('ENTRY_NO_LONGER_ALLOWED')
            built = await self.builder.request({**payload, 'action': 'BUILD', 'public_key': self.wallet})
            if built.get('simulation_passed') is not True:
                raise RuntimeError('SIMULATION_REQUIRED')
            if payload['side'] == 'BUY' and not self.allow_buy():
                raise RuntimeError('ENTRY_NO_LONGER_ALLOWED')
            # Key bytes stay inside Signer on the VPS; this path is never used by shadow.
            signed, signature = await self.signer.sign(built['transaction_base64'])
            if payload['side'] == 'BUY' and not self.allow_buy():
                raise RuntimeError('ENTRY_NO_LONGER_ALLOWED')
            self.journal.mark_signed(entry.idempotency_key, signed_tx_b64=signed, signature=signature)
        except Exception as error:
            allowed = {'ENTRY_NO_LONGER_ALLOWED', 'SIMULATION_REQUIRED', 'BUILDER:CURVE_MIGRATED',
                'BUILDER:ENTRY_CHASE_GUARD_REJECTED', 'BUILDER:ENTRY_OUTPUT_GUARD_REJECTED',
                'BUILDER:SIMULATION_FAILED', 'BUILDER:SIMULATION_BUDGET_EXCEEDED'}
            reason = str(error) if str(error) in allowed else 'BUILD_OR_SIGN_FAILED'
            self.journal.mark_failed(entry.idempotency_key, reason, terminal=True)
            return {'status': 'FAILED', 'reason': reason}
        self.journal.mark_submitted(entry.idempotency_key)
        # Every route receives identical bytes; a transport timeout never creates a new transaction.
        async def send(url):
            try:
                result = await self.rpc.call('sendTransaction', [signed,
                    {'encoding': 'base64', 'skipPreflight': False, 'preflightCommitment': 'confirmed', 'maxRetries': 2}], url=url)
                if result != signature:
                    raise RuntimeError('SIGNATURE_MISMATCH')
            except RuntimeError:
                return
        routes = [asyncio.create_task(send(url)) for url in self.rpc.urls]
        try:
            # A slow secondary provider must not delay observing a landed fill.
            return await self.resolve(self.journal.get(entry.idempotency_key), wait_seconds=15)
        finally:
            for route in routes:
                if not route.done():
                    route.cancel()
            await asyncio.gather(*routes, return_exceptions=True)

    async def resolve(self, entry, *, wait_seconds=0):
        if entry.state == 'FAILED_TERMINAL':
            return {'status': 'FAILED', 'reason': 'PREVIOUS_FAILURE'}
        if not entry.signature:
            self.journal.mark_failed(entry.idempotency_key, 'NO_SIGNED_TRANSACTION', terminal=True)
            return {'status': 'FAILED', 'reason': 'NO_SIGNED_TRANSACTION'}
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                tx = await self.confirmed_transaction(entry.signature)
                if tx:
                    result = receipt(tx, wallet=self.wallet, mint=entry.mint, side=entry.side,
                                     submitted_ns=entry.payload['submitted_ns'])
                    if result['status'] == 'FAILED':
                        self.journal.mark_failed(entry.idempotency_key, result['reason'], terminal=True)
                    else:
                        self.journal.mark_confirmed(entry.idempotency_key, route='confirmed_rpc', slot=result['slot'])
                    return {**result, 'signature': entry.signature}
            except (RuntimeError, KeyError, TypeError, ValueError):
                pass
            if time.monotonic() >= deadline:
                return {'status': 'UNKNOWN', 'signature': entry.signature}
            await asyncio.sleep(.1)

    async def confirmed_transaction(self, signature):
        async def query(url):
            return await self.rpc.call('getTransaction', [signature,
                {'encoding': 'jsonParsed', 'commitment': 'confirmed', 'maxSupportedTransactionVersion': 0}], url=url)
        tasks = [asyncio.create_task(query(url)) for url in self.rpc.urls]
        try:
            for ready in asyncio.as_completed(tasks):
                try:
                    value = await ready
                    if value:
                        return value
                except RuntimeError:
                    continue
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Metadata never gets RPC credentials or access to private/link-local networks."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        result = []
        for af, _, proto, _, address in rows:
            if not ipaddress.ip_address(address[0]).is_global:
                raise OSError('non-public metadata address')
            result.append({'hostname': host, 'host': address[0], 'port': port,
                           'family': af, 'proto': proto, 'flags': socket.AI_NUMERICHOST})
        return result
    async def close(self):
        pass


async def metadata(session, uri):
    if uri.startswith('ipfs://'):
        uri = 'https://ipfs.io/ipfs/' + uri[7:].removeprefix('ipfs/')
    parsed = urlsplit(uri)
    if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('unsupported metadata URI')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('non-public metadata address')
    async with session.get(uri, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=3)) as response:
        if response.status != 200:
            raise ValueError('metadata unavailable')
        body = await response.content.read(262_145)
        if len(body) > 262_144:
            raise ValueError('metadata too large')
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError('metadata must be an object')
        return document, time.time_ns()


async def native_feed(session, urls, emit, health, stop):
    index = 0
    while not stop.is_set():
        endpoint = urlsplit(urls[index % len(urls)])
        websocket = urlunsplit(('wss', endpoint.netloc, endpoint.path, endpoint.query, ''))
        index += 1
        try:
            async with session.ws_connect(websocket, heartbeat=15, receive_timeout=30, max_msg_size=4_194_304) as ws:
                await ws.send_json({'jsonrpc': '2.0', 'id': 1, 'method': 'logsSubscribe',
                    'params': [{'mentions': [PUMP_PROGRAM_ID]}, {'commitment': 'confirmed'}]})
                reply = await ws.receive_json(timeout=10)
                if 'error' in reply or type(reply.get('result')) is not int:
                    raise RuntimeError('FEED_SUBSCRIPTION_REJECTED')
                health(True)
                async for message in ws:
                    if stop.is_set():
                        break
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    value = payload.get('params', {}).get('result', {}).get('value') or {}
                    if value.get('err') is not None or not value.get('signature'):
                        continue
                    observed = time.time_ns()
                    events = anchor_events_from_logs(value.get('logs', []), PUMP_PROGRAM_ID)
                    await emit(value['signature'], events, observed)
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError):
            pass
        finally:
            health(False)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
