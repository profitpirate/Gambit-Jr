"""Operator entry point: probe, shadow, live. Never enables live flags itself."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path

import aiohttp

from .v12_prearmed_evidence import read_evidence, evaluate_research_acceptance
from .v12_prearmed_io import Rpc, Builder, LiveExecution, PublicResolver, native_feed
from .v12_prearmed_runtime import Runtime, State


def true(name):
    return os.getenv(name, '').lower() == 'true'


def settings(root, data, mode):
    from .e4_live import Settings
    value = Settings.from_env()
    value.live = mode == 'live'
    value.execution_db = data / (mode + '.sqlite')
    value.builder_command = ('node', str(root / 'tools/e4-builder/frozen-prearmed.mjs'))
    # The new runner broadcasts identical bytes through its two JSON-RPC providers.
    # No remote transaction builder or non-RPC route is used.
    value.route_urls = {f'rpc_{n}': url for n, url in enumerate(value.fallback_rpcs)}
    value.direct_rpc_route = True
    return value


def verify_source(root):
    from .v12_security import IntegrityVerifier
    path = Path(os.environ['V12_INTEGRITY_MANIFEST'])
    document = json.loads(path.read_text())
    required = {p.relative_to(root).as_posix() for p in (root / 'src/memecoin_bot').rglob('*.py')}
    required |= {'tools/e4-builder/frozen-prearmed.mjs', 'tools/e4-builder/package-lock.json',
                 'tools/e4-builder/package.json', 'tools/e4-builder/patch-anchor-bn-import.mjs'}
    if not required.issubset(document.get('files', {})):
        raise RuntimeError('INCOMPLETE_CODE_INTEGRITY_COVERAGE')
    return IntegrityVerifier(root, path, public_key=os.environ['V12_INTEGRITY_PUBLIC_KEY'],
        signature=os.environ['V12_INTEGRITY_SIGNATURE'], require_signature=True).verify()


def local_readiness(config, root, model_path, *, live):
    from .v12_live_readiness import run_live_readiness
    verify_source(root)
    if live and not (true('E4_LIVE') and true('V12_LIVE_TRADING_ENABLED')):
        raise PermissionError('BOTH_LIVE_ENABLE_FLAGS_REQUIRED')
    return run_live_readiness(config, repository_root=root, frozen_model_path=model_path,
                              acquire_lock=live, check_arming=live, inspect_signer_file=live)


class Notices:
    def __init__(self, session):
        self.session = session

    async def validate(self):
        headers = {'Authorization': 'Bot ' + os.environ['DISCORD_TOKEN']}
        channel = os.getenv('V12_DISCORD_NOTIFICATION_CHANNEL_ID')
        target = ('channels/' + channel) if channel else 'users/@me'
        async with self.session.get('https://discord.com/api/v10/' + target, headers=headers,
                timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200:
                raise RuntimeError('DISCORD_CONFIGURATION_REJECTED')
        async with self.session.post('https://api.pushover.net/1/users/validate.json',
                data={'token': os.environ['PUSHOVER_APP_TOKEN'], 'user': os.environ['PUSHOVER_USER_KEY']},
                timeout=aiohttp.ClientTimeout(total=8)) as response:
            value = await response.json()
            if response.status != 200 or value.get('status') != 1:
                raise RuntimeError('PUSHOVER_CONFIGURATION_REJECTED')

    async def send_discord(self, message):
        headers = {'Authorization': 'Bot ' + os.environ['DISCORD_TOKEN']}
        channel = os.getenv('V12_DISCORD_NOTIFICATION_CHANNEL_ID')
        if not channel:
            async with self.session.post('https://discord.com/api/v10/users/@me/channels', headers=headers,
                    json={'recipient_id': os.environ['V12_DISCORD_USER_ID']},
                    timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status not in (200, 201):
                    raise RuntimeError('DISCORD_DELIVERY_FAILED')
                channel = (await response.json())['id']
        async with self.session.post('https://discord.com/api/v10/channels/' + channel + '/messages',
                headers=headers, json={'content': message, 'allowed_mentions': {'parse': []}},
                timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status not in (200, 201):
                raise RuntimeError('DISCORD_DELIVERY_FAILED')

    async def send_pushover(self, message):
        async with self.session.post('https://api.pushover.net/1/messages.json',
                data={'token': os.environ['PUSHOVER_APP_TOKEN'], 'user': os.environ['PUSHOVER_USER_KEY'],
                      'message': message}, timeout=aiohttp.ClientTimeout(total=8)) as response:
            if response.status != 200 or (await response.json()).get('status') != 1:
                raise RuntimeError('PUSHOVER_DELIVERY_FAILED')

    async def send(self, message):
        results = await asyncio.gather(self.send_discord(message), self.send_pushover(message),
                                       return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError('NOTIFICATION_DELIVERY_FAILED')


async def startup_network(config, builder, rpc, session, *, live):
    try:
        return await network_probe(config, builder, rpc, session, check_feed=not live), True
    except Exception:
        if not live:
            raise
        # A degraded provider must not prevent journal recovery and exit attempts.
        # New entries remain disabled until an operator investigates and resets.
        return {}, False


async def startup_balance(runtime, *, live):
    try:
        await runtime.refresh_balance()
    except Exception:
        if not live:
            raise
        runtime.balance_ns = 0
        runtime.breaker.exit_only('startup_balance_unavailable')


async def network_probe(config, builder, rpc, session, *, check_feed=True):
    slots = []
    for url in rpc.urls:
        genesis = await rpc.call('getGenesisHash', [], url=url)
        if genesis != '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d':
            raise RuntimeError('RPC_IS_NOT_SOLANA_MAINNET')
        slots.append(await rpc.call('getSlot', [{'commitment': 'confirmed'}], url=url))
    if len(slots) < 2 or max(slots) - min(slots) > 64:
        raise RuntimeError('RPC_REDUNDANCY_OR_SLOT_DIVERGENCE')
    balance = await rpc.balance_lamports(config.wallet)
    if check_feed and balance < 100_100_000:
        raise RuntimeError('WALLET_BELOW_TRANSACTION_AND_RESERVE_MINIMUM')
    await builder.request({'action': 'HEALTH'})
    if not check_feed:
        return {}
    stop, received = asyncio.Event(), asyncio.Event()
    count = 0
    async def emit(_signature, events, _time):
        nonlocal count
        count += len(events)
        if events:
            received.set()
    feed = asyncio.create_task(native_feed(session, rpc.urls, emit, lambda _: None, stop))
    try:
        await asyncio.wait_for(received.wait(), 40)
    except TimeoutError:
        raise RuntimeError('LIVE_FEED_NOT_RECEIVING_DECODABLE_PUMP_EVENTS') from None
    finally:
        stop.set()
        feed.cancel()
        await asyncio.gather(feed, return_exceptions=True)
    return dict(mainnet_rpc_endpoints=len(slots), feed_events=count, wallet_balance_sol=balance / 1e9,
                builder_health='PASS', transaction_sent=False, signer_opened=False)


async def run(args):
    from .v12_security import TamperEvidentAuditLog, decode_secret_key
    from .v12_backup import EncryptedBackupManager, decode_aes_key
    from .v12_execution_journal import ExecutionJournal
    root, data = Path(args.root).resolve(), Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    model_path = Path(os.environ['V12_FROZEN_MODEL_PATH'])
    model = read_evidence(model_path)
    evidence = read_evidence(Path(os.environ['V12_CAUSAL_CERTIFICATION_STATE_PATH']))
    if not evaluate_research_acceptance(model, evidence).accepted:
        raise RuntimeError('FROZEN_RESEARCH_EVIDENCE_REJECTED')
    mode = args.mode
    config = settings(root, data, 'shadow' if mode == 'probe' else mode)
    checks, lock = local_readiness(config, root, model_path, live=mode == 'live')
    builder = Builder(config.builder_command)
    runtime, journal, state, feed = None, None, None, None
    stop = asyncio.Event()
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    try:
        async with aiohttp.ClientSession() as session, aiohttp.ClientSession(connector=connector, trust_env=False) as meta_session:
            rpc = Rpc(session, (config.rpc_url, *config.fallback_rpcs))
            notices = Notices(session)
            notices_ok = True
            try:
                await notices.validate()
            except Exception:
                if mode != 'live':
                    raise
                notices_ok = False
            report, network_ok = await startup_network(config, builder, rpc, session, live=mode == 'live')
            if mode == 'probe':
                print(json.dumps({'status': 'READ_ONLY_CONNECTIVITY_PASS', **report}, indent=2))
                return
            audit = TamperEvidentAuditLog(data / (mode + '-audit.jsonl'), decode_secret_key(os.environ['V12_AUDIT_HMAC_KEY']))
            if not audit.verify():
                raise RuntimeError('AUDIT_CHAIN_INVALID')
            queue = asyncio.Queue(maxsize=100)
            def emit(event, payload):
                audit.append(event, payload)
                print(json.dumps({'event': event, **payload}), flush=True)
                if event in {'FILL_CONFIRMED', 'ORDER_FAILED', 'EXECUTION_OUTCOME_UNKNOWN', 'RUNTIME_TASK_FAILED', 'SAFETY_MODE_CHANGED'}:
                    if queue.full():
                        raise RuntimeError('NOTIFICATION_BACKLOG')
                    queue.put_nowait(event + ': ' + json.dumps(payload))
            state = State(config.execution_db, wallet=config.wallet, mode=mode)
            journal = ExecutionJournal(state.conn)
            execution = LiveExecution(settings=config, rpc=rpc, builder=builder, journal=journal) if mode == 'live' else None
            runtime = Runtime(model=model, state=state, rpc=rpc, builder=builder, execution=execution,
                wallet=config.wallet, metadata_session=meta_session, emit=emit,
                kill_path=os.environ['V12_KILL_SWITCH_PATH'])
            if not notices_ok:
                runtime.breaker.exit_only('notification_validation_failed')
            if not network_ok:
                runtime.breaker.exit_only('startup_rpc_validation_failed')
            if execution:
                await runtime.reconcile()
            await startup_balance(runtime, live=mode == 'live')
            if execution:
                await runtime.tick()
                try:
                    await notices.send('Gambit frozen Pre-Armed live process starting. '
                        f'Open positions: {len(state.data["positions"])}; unresolved orders: {len(state.data["pending"])}.')
                except Exception:
                    runtime.breaker.exit_only('notification_delivery_failed')
            feed = asyncio.create_task(native_feed(session, rpc.urls, runtime.on_batch, runtime.feed_health, stop))
            def stopping(*_):
                runtime.stopping = True
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, stopping)
            async def balances():
                while not stop.is_set():
                    try:
                        await runtime.refresh_balance()
                    except (RuntimeError, KeyError, ValueError):
                        runtime.balance_ns = 0
                    await asyncio.sleep(1)
            async def notifications():
                while not stop.is_set():
                    text = await queue.get()
                    try:
                        await notices.send(text)
                    except Exception:
                        runtime.breaker.exit_only('notification_delivery_failed')
                        audit.append('NOTIFICATION_DELIVERY_FAILED', {})
            balance_task = asyncio.create_task(balances())
            notice_task = asyncio.create_task(notifications())
            backup = EncryptedBackupManager(data / 'backups', decode_aes_key(os.environ['V12_BACKUP_AES_KEY']))
            next_report = next_backup = 0
            backup_task = None
            started = time.monotonic()
            emit('RUNNER_STARTED', {'mode': mode, 'strategy': runtime.policy.family})
            try:
                while True:
                    await runtime.tick()
                    now = time.monotonic()
                    if now >= next_report:
                        status = runtime.status()
                        temp = data / (mode + '-status.tmp')
                        temp.write_text(json.dumps(status, indent=2))
                        temp.replace(data / (mode + '-status.json'))
                        next_report = now + 1
                    if now >= next_backup:
                        if backup_task is None or backup_task.done():
                            backup_task = runtime.spawn(asyncio.to_thread(backup.backup, config.execution_db, label=mode))
                        next_backup = now + 60
                    if args.seconds and now - started >= args.seconds:
                        runtime.stopping = True
                    if runtime.stopping and not state.data['pending'] and not state.data['positions']:
                        break
                    await asyncio.sleep(.025)
            finally:
                stop.set()
                for task in (balance_task, notice_task, feed):
                    task.cancel()
                await asyncio.gather(balance_task, notice_task, feed, return_exceptions=True)
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
                backup.backup(config.execution_db, label=mode)
    finally:
        await builder.close()
        if state:
            state.conn.close()
        if lock:
            lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('mode', choices=('probe', 'shadow', 'live'))
    parser.add_argument('--root', default='/app')
    parser.add_argument('--data', default='/data')
    parser.add_argument('--seconds', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        from .v12_live_readiness import LiveReadinessError
        if isinstance(exc, LiveReadinessError):
            print(json.dumps({'status': 'BLOCKED', 'failed_checks': [c.name for c in exc.checks if not c.passed]}))
        else:
            # Exceptions from RPC/SDK/HTTP can embed credentials. Print only the type.
            import re
            message = str(exc)
            safe = message if re.fullmatch(r'[A-Z][A-Z0-9_:]{0,120}', message) else type(exc).__name__
            print(json.dumps({'status': 'BLOCKED', 'reason': safe}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
