"""Operator-run technical checks. Never constructs execution or signs transactions.

The key is mounted only in the separate network-disabled offline invocation.
The network invocation sends alerts only with explicit --send-test-alert.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .v12_prearmed import Notices, network_probe, settings, verify_source
from .v12_prearmed_io import Builder, Rpc
from .realtime.pumpfun import PUMP_PROGRAM_ID, anchor_events_from_logs


def live_database_check(path, wallet):
    if not path.exists():
        return True
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            return False
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'frozen_state' in tables:
            row = connection.execute('SELECT data FROM frozen_state WHERE id=1').fetchone()
            if row:
                state = json.loads(row[0])
                if state.get('wallet') != wallet or state.get('positions') or state.get('pending'):
                    return False
        if 'v12_execution_journal' in tables:
            if connection.execute("SELECT COUNT(*) FROM v12_execution_journal WHERE state IN ('SIGNED','SUBMITTED','UNCERTAIN')").fetchone()[0]:
                return False
    return True


def offline_checks(root, data):
    from solders.keypair import Keypair
    from .v12_security import ensure_private_file, SingleInstanceLock, TamperEvidentAuditLog, decode_secret_key
    from .v12_backup import EncryptedBackupManager, decode_aes_key
    checks = {}
    verify_source(root)
    checks['signed_release_integrity'] = True
    wallet = os.environ['E4_WALLET_PUBLIC_KEY']
    key_path = Path(os.environ['E4_KEYPAIR_PATH'])
    ensure_private_file(key_path)
    checks['signing_key_owned_by_runtime_user'] = key_path.stat().st_uid == os.getuid()
    raw = key_path.read_text().strip()
    key = Keypair.from_bytes(bytes(json.loads(raw))) if raw.startswith('[') else Keypair.from_base58_string(raw)
    checks['protected_signing_key_matches_wallet'] = str(key.pubkey()) == wallet
    del key, raw
    checks['disk_free_at_least_512_mib'] = shutil.disk_usage(data).free >= 512 * 1024 * 1024
    checks['no_live_positions_or_unresolved_orders'] = live_database_check(data / 'live.sqlite', wallet)
    checks['operator_stop_clear'] = not (data / 'STOP').exists()
    first = SingleInstanceLock(data / 'live.lock')
    second = SingleInstanceLock(data / 'live.lock')
    first.acquire()
    try:
        try:
            second.acquire()
        except RuntimeError:
            checks['duplicate_instance_lock'] = True
        else:
            second.release()
            checks['duplicate_instance_lock'] = False
    finally:
        first.release()
    audit_path = data / 'live-audit.jsonl'
    checks['existing_live_audit_chain'] = not audit_path.exists() or TamperEvidentAuditLog(
        audit_path, decode_secret_key(os.environ['V12_AUDIT_HMAC_KEY'])).verify()
    with tempfile.TemporaryDirectory(prefix='gambit-restore-') as folder:
        temp = Path(folder)
        backup = EncryptedBackupManager(temp / 'backups', decode_aes_key(os.environ['V12_BACKUP_AES_KEY']))
        result = backup.backup(data / 'shadow.sqlite', label='verification')
        restored = temp / 'restored.sqlite'
        backup.decrypt_to(Path(result.destination), restored)
        with sqlite3.connect(restored) as connection:
            checks['encrypted_backup_restore'] = connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    return dict(passed=all(checks.values()), checks=checks, wallet_key_opened_locally=True,
                network_enabled=False, transaction_signed_or_sent=False)


async def observe_endpoint(session, url, *, seconds=30):
    endpoint = urlsplit(url)
    websocket = urlunsplit(('wss', endpoint.netloc, endpoint.path, endpoint.query, ''))
    start = time.monotonic()
    byte_count = event_count = 0
    last_event = start
    max_gap = 0.0
    try:
        async with session.ws_connect(websocket, heartbeat=10, max_msg_size=4_194_304) as ws:
            await ws.send_json({'jsonrpc': '2.0', 'id': 1, 'method': 'logsSubscribe',
                'params': [{'mentions': [PUMP_PROGRAM_ID]}, {'commitment': 'confirmed'}]})
            reply = await ws.receive_json(timeout=10)
            if 'error' in reply or type(reply.get('result')) is not int:
                raise RuntimeError('FEED_SUBSCRIPTION_REJECTED')
            start = last_event = time.monotonic()
            deadline = start + seconds
            while time.monotonic() < deadline:
                try:
                    message = await ws.receive(timeout=max(.001, deadline - time.monotonic()))
                except TimeoutError:
                    break
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError('FEED_CLOSED')
                byte_count += len(message.data.encode())
                value = (json.loads(message.data).get('params', {}).get('result', {}).get('value') or {})
                if value.get('err') is None:
                    events = anchor_events_from_logs(value.get('logs', []), PUMP_PROGRAM_ID)
                    if events:
                        now = time.monotonic()
                        max_gap = max(max_gap, now - last_event)
                        last_event = now
                        event_count += len(events)
            duration = time.monotonic() - start
            max_gap = max(max_gap, time.monotonic() - last_event)
            report = dict(passed=event_count > 0 and max_gap < 5, seconds=round(duration, 2),
                decoded_events=event_count, bytes_received=byte_count,
                bytes_per_second=round(byte_count / max(1, duration)),
                longest_decoded_event_gap_seconds=round(max_gap, 3))
            if endpoint.hostname and (endpoint.hostname == 'helius-rpc.com' or endpoint.hostname.endswith('.helius-rpc.com')):
                # Helius standard WebSocket data: 2 credits per 0.1 MB (Sept 2026).
                report['estimated_30_day_helius_stream_credits'] = round(byte_count / max(1, duration) * 2592000 * 2 / 100000)
            return report
    except Exception:
        return dict(passed=False, reason='FEED_UNAVAILABLE_OR_INVALID', decoded_events=event_count)


async def network_checks(root, data, *, send_test_alert):
    if not send_test_alert:
        raise RuntimeError('EXPLICIT_TEST_ALERT_OPTION_REQUIRED')
    config = settings(root, data, 'shadow')
    builder = Builder(config.builder_command)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            rpc = Rpc(session, (config.rpc_url, *config.fallback_rpcs))
            connectivity = await network_probe(config, builder, rpc, session)
            # Each configured provider must independently support the live stream.
            feeds = await asyncio.gather(*(observe_endpoint(session, url) for url in rpc.urls))
            notices = Notices(session)
            message = 'Gambit-Jr final readiness TEST. Trading is DISABLED. No transaction has been signed or sent by this check.'
            deliveries = await asyncio.gather(notices.send_discord(message), notices.send_pushover(message), return_exceptions=True)
            alerts = {name: not isinstance(value, BaseException) for name, value in zip(('discord', 'pushover'), deliveries)}
            return dict(passed=all(f['passed'] for f in feeds) and all(alerts.values()),
                connectivity=connectivity, provider_feed_checks=feeds,
                test_alerts_accepted=alerts, alerts_received_by_human_unverified=True,
                transaction_signed_or_sent=False, wallet_key_mounted=False)
    finally:
        await builder.close()


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--offline', action='store_true')
    group.add_argument('--send-test-alert', action='store_true')
    args = parser.parse_args()
    root, data = Path('/app'), Path('/data')
    kind = 'offline' if args.offline else 'network'
    destination = data / ('final-' + kind + '.json')
    try:
        if any(os.getenv(k, '').lower() != 'false' for k in ('E4_LIVE', 'V12_LIVE_TRADING_ENABLED')):
            raise RuntimeError('BOTH_LIVE_FLAGS_MUST_BE_FALSE')
        report = offline_checks(root, data) if args.offline else asyncio.run(network_checks(root, data, send_test_alert=args.send_test_alert))
    except Exception as error:
        # No provider URLs, keys, exception messages or transaction contents leave the process.
        report = dict(passed=False, reason='CHECK_FAILED', error_type=type(error).__name__,
                      transaction_signed_or_sent=False)
    report['checked_at_ns'] = time.time_ns()
    destination.write_text(json.dumps(report, indent=2))
    os.chmod(destination, 0o600)
    print(json.dumps({kind: report}, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
