#!/usr/bin/env python3
"""Operator controls. Installation/probe/shadow never enable trading."""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit

STAGE = Path('/opt/gambit-v12-staging')
CONFIG = STAGE / 'prearmed.env'
DATA = STAGE / 'prearmed-data'
SERVICE = 'gambit-prearmed-live'


def call(*args, capture=False, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def env_read(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                    result[key] = value.strip().strip('"').strip("'")
    return result


def env_write(path, values):
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or any(c in str(value) for c in '\n\r\0'):
            raise ValueError('Invalid configuration characters')
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        os.chmod(temp, 0o600)
        f.write(''.join(f'{key}={values[key]}\n' for key in sorted(values)))
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def active():
    value = call('systemctl', 'show', '-p', 'ActiveState', '--value', SERVICE, check=False, capture=True).stdout.strip()
    return value in {'active', 'activating', 'deactivating', 'reloading'}


def installed():
    doc = json.loads((STAGE / 'prearmed-install.json').read_text())
    if doc.get('offline_tests_passed') is not True:
        raise RuntimeError('Install and offline checks must finish first')
    return doc


def configure():
    if active():
        raise RuntimeError('Stop the live service before changing configuration')
    doc = installed()
    old, current = env_read(STAGE / 'v12.env'), env_read(CONFIG)
    permitted = ('E4_WALLET_PUBLIC_KEY', 'E4_VAULT_PUBLIC_KEY', 'E4_PRIMARY_RPC_URL',
        'E4_FALLBACK_RPC_URLS', 'DISCORD_TOKEN', 'V12_DISCORD_USER_ID', 'V12_DISCORD_NOTIFICATION_CHANNEL_ID',
        'PUSHOVER_APP_TOKEN', 'PUSHOVER_USER_KEY', 'V12_AUDIT_HMAC_KEY', 'V12_BACKUP_AES_KEY')
    values = {k: current.get(k) or old.get(k, '') for k in permitted}
    if not values.get('DISCORD_TOKEN'):
        try:
            values['DISCORD_TOKEN'] = env_read(Path('/opt/gambit-jr/.env')).get('DISCORD_TOKEN', '')
        except OSError:
            pass
    for name in ('E4_WALLET_PUBLIC_KEY', 'E4_VAULT_PUBLIC_KEY'):
        if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', values[name]):
            raise RuntimeError('Complete the existing public-wallet setup first')
    keyfile = STAGE / 'secrets/trading-keypair.json'
    if not keyfile.is_file() or keyfile.stat().st_mode & 0o077:
        raise RuntimeError('The existing signer file must exist with private permissions')
    print('Wallet setup found. No wallet key needs to be entered again.')
    print('Paste provider URLs and alert credentials only at these private prompts.')
    def prompt(name, label, secret=True):
        if values.get(name):
            print(label + ': already configured')
            return
        value = getpass.getpass(label + ': ') if secret else input(label + ': ')
        if not value.strip():
            raise RuntimeError(label + ' is required')
        values[name] = value.strip()
        # Save progress with trading disabled, so interrupted setup can resume.
        env_write(CONFIG, {**values, 'E4_LIVE': 'false', 'V12_LIVE_TRADING_ENABLED': 'false'})
    prompt('E4_PRIMARY_RPC_URL', 'Helius Solana MAINNET HTTPS RPC URL')
    prompt('E4_FALLBACK_RPC_URLS', 'QuickNode Solana MAINNET HTTPS RPC URL')
    urls = [values['E4_PRIMARY_RPC_URL'], *values['E4_FALLBACK_RPC_URLS'].split(',')]
    if len(set(urls)) < 2 or any(urlsplit(u).scheme != 'https' or not urlsplit(u).hostname for u in urls):
        raise RuntimeError('Two distinct HTTPS Solana RPC URLs are required')
    prompt('DISCORD_TOKEN', 'Discord bot token')
    if not values.get('V12_DISCORD_USER_ID') and not values.get('V12_DISCORD_NOTIFICATION_CHANNEL_ID'):
        prompt('V12_DISCORD_NOTIFICATION_CHANNEL_ID', 'Discord alert channel ID', secret=False)
    prompt('PUSHOVER_APP_TOKEN', 'Pushover application API token')
    prompt('PUSHOVER_USER_KEY', 'Pushover user key')
    for key in ('V12_AUDIT_HMAC_KEY', 'V12_BACKUP_AES_KEY'):
        if not values.get(key):
            values[key] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    signature = json.loads((Path(doc['release']) / 'release-signature.json').read_text())
    values.update(E4_LIVE='false', V12_LIVE_TRADING_ENABLED='false', V12_ALLOW_LOCAL_KEYPAIR='true',
        E4_KEYPAIR_PATH='/secrets/trading-keypair.json', E4_SIGNER_COMMAND='', V12_NOTIFICATIONS_ENABLED='true',
        V12_FROZEN_MODEL_PATH='/app/evidence/v12-pre-armed-100-trade-frozen-model.json',
        V12_CAUSAL_CERTIFICATION_STATE_PATH='/app/evidence/v12-pre-armed-100-causal-paper-live.json',
        V12_INTEGRITY_MANIFEST='/app/release-manifest.json',
        V12_INTEGRITY_PUBLIC_KEY=signature['public_key'], V12_INTEGRITY_SIGNATURE=signature['signature'],
        V12_KILL_SWITCH_PATH='/data/STOP', V12_INSTANCE_LOCK='/data/live.lock')
    env_write(CONFIG, values)
    print('Configuration saved privately. Trading is disabled.')


def docker_command(mode, *, seconds=0):
    doc = installed()
    args = ['docker', 'run', '--rm', '--read-only', '--init', '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--pids-limit=256', '--memory=1536m',
        '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,nosuid,size=256m,mode=1777',
        '--env-file', str(CONFIG), '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONUNBUFFERED=1',
        '--mount', f'type=bind,source={DATA},target=/data']
    if mode == 'live':
        args += ['--name', 'gambit-prearmed-live', '--mount',
            f'type=bind,source={STAGE}/secrets/trading-keypair.json,target=/secrets/trading-keypair.json,readonly']
    else:
        args += ['-e', 'E4_LIVE=false', '-e', 'V12_LIVE_TRADING_ENABLED=false']
    args += [doc['image_id'], mode]
    if seconds:
        args += ['--seconds', str(seconds)]
    return args


def probe():
    if active():
        raise RuntimeError('Use status while live trading is running')
    call(*docker_command('probe'))


def shadow(seconds=30):
    if active():
        raise RuntimeError('Use status while live trading is running')
    started = time.time_ns()
    call(*docker_command('shadow', seconds=seconds))
    state = json.loads((DATA / 'shadow-status.json').read_text())
    if (not state.get('feed_connected') or not state.get('feed_events') or state.get('safety_mode') != 'ACTIVE'
            or state.get('updated_ns', 0) < started or time.time_ns() - state.get('updated_ns', 0) > 10_000_000_000):
        raise RuntimeError('Live-data observation did not pass; inspect shadow status')
    (DATA / 'shadow-pass.json').write_text(json.dumps({'observed_ns': time.time_ns(), 'image_id': installed()['image_id']}))
    print('LIVE DATA CHECK PASSED. No transaction was signed or sent.')


def config_fingerprint():
    values = env_read(CONFIG)
    for name in ('E4_LIVE', 'V12_LIVE_TRADING_ENABLED', 'V12_LIVE_ARM_TOKEN', 'V12_LIVE_ARM_TOKEN_SHA256'):
        values.pop(name, None)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def final_report():
    result = {'status': 'FINAL_CHECKS_NOT_PASSED', 'reports': {}}
    for kind in ('offline', 'network'):
        path = DATA / ('final-' + kind + '.json')
        if path.exists():
            result['reports'][kind] = json.loads(path.read_text())
    proof = DATA / 'final-check-pass.json'
    if proof.exists():
        result = json.loads(proof.read_text())
        age = time.time_ns() - result.get('observed_ns', 0)
        if (result.get('image_id') != installed()['image_id'] or result.get('config_sha256') != config_fingerprint()
                or not 0 <= age <= 3_600_000_000_000):
            result['status'] = 'FINAL_CHECK_STALE_OR_CONFIGURATION_CHANGED'
        result.pop('config_sha256', None)
    result['live_service_active_now'] = active()
    values = env_read(CONFIG)
    result['live_flags_enabled_now'] = any(values.get(k, '').lower() == 'true' for k in ('E4_LIVE', 'V12_LIVE_TRADING_ENABLED'))
    print(json.dumps(result, indent=2))


def final_check(*, send_test_alert=False):
    if active():
        raise RuntimeError('Final checks require the live service to be inactive; no service was changed')
    values = env_read(CONFIG)
    if any(values.get(k, '').lower() == 'true' for k in ('E4_LIVE', 'V12_LIVE_TRADING_ENABLED')):
        raise RuntimeError('Final checks require trading flags to remain disabled')
    if not send_test_alert:
        raise RuntimeError('Use final-check --send-test-alert to test delivery to your configured Discord and phone')
    proof = DATA / 'final-check-pass.json'
    proof.unlink(missing_ok=True)
    clock = call('timedatectl', 'show', '-p', 'NTPSynchronized', '--value', capture=True, check=False)
    if clock.returncode != 0 or clock.stdout.strip() != 'yes':
        raise RuntimeError('Server clock synchronization could not be confirmed')
    running = call('docker', 'inspect', '--format', '{{.State.Running}}', SERVICE, capture=True, check=False)
    if running.returncode == 0 and running.stdout.strip() == 'true':
        raise RuntimeError('A live container is running outside the reported service state')
    doc = installed()
    release = Path(doc['release'])
    if hashlib.sha256(Path(__file__).read_bytes()).digest() != hashlib.sha256((release / 'deployment/prearmed-control.py').read_bytes()).digest():
        raise RuntimeError('Installed control script differs from the release')
    for offline in (True, False):
        args = docker_command('probe')
        at = args.index(doc['image_id'])
        extra = ['--entrypoint', 'python']
        if offline:
            extra += ['--network=none', '--mount',
                f'type=bind,source={STAGE}/secrets/trading-keypair.json,target=/secrets/trading-keypair.json,readonly']
        args[at:at] = extra
        args[-1:] = ['-m', 'memecoin_bot.v12_prearmed_preflight', '--offline' if offline else '--send-test-alert']
        call(*args)
    shadow(seconds=120)
    reports = {name: json.loads((DATA / ('final-' + name + '.json')).read_text()) for name in ('offline', 'network')}
    if any(report.get('passed') is not True for report in reports.values()):
        raise RuntimeError('Final readiness report contains failed checks')
    report = dict(status='FINAL_TECHNICAL_CHECKS_PASS', observed_ns=time.time_ns(), image_id=doc['image_id'],
        config_sha256=config_fingerprint(), reports=reports,
        shadow=json.loads((DATA / 'shadow-status.json').read_text()),
        trading_enabled=False, transactions_signed_or_sent=False, server_clock_synchronized=True,
        limitations=['No real trade was executed; transaction landing and exit timing are unproven.',
            'Provider billing limits and remaining credits require dashboard confirmation.',
            'Unknown submitted transaction outcomes block entries until reconciled; do not blindly resend.'])
    proof.write_text(json.dumps(report, indent=2))
    os.chmod(proof, 0o600)
    print('FINAL TECHNICAL CHECKS PASSED. Trading remains disabled.')


def start_live():
    if active():
        print('Live service is already active; use status.')
        return
    if (DATA / 'STOP').exists():
        raise RuntimeError('Stop marker is set. Use clear-stop only when you intend to restart.')
    final_path = DATA / 'final-check-pass.json'
    if not final_path.exists():
        raise RuntimeError('Run final-check --send-test-alert before live activation')
    final = json.loads(final_path.read_text())
    final_age = time.time_ns() - final.get('observed_ns', 0)
    if (final.get('status') != 'FINAL_TECHNICAL_CHECKS_PASS' or final.get('image_id') != installed()['image_id']
            or final.get('config_sha256') != config_fingerprint() or not 0 <= final_age <= 3_600_000_000_000):
        raise RuntimeError('Final check is missing, stale, or belongs to a different configuration; rerun final-check --send-test-alert')
    observation = json.loads((DATA / 'shadow-pass.json').read_text())
    if observation.get('image_id') != installed()['image_id'] or not 0 <= time.time_ns() - observation['observed_ns'] <= 3_600_000_000_000:
        raise RuntimeError('Run shadow again; its check must be from this release within the last hour')
    probe()
    # This operation happens only when the operator explicitly runs start-live.
    prior = call('docker', 'inspect', '--format', '{{.State.Running}}', 'gambit-jr-bot-1', capture=True, check=False)
    if prior.returncode == 0:
        # Prevent Docker from bringing the retired strategy back after a reboot.
        call('docker', 'update', '--restart=no', 'gambit-jr-bot-1', capture=True)
        if prior.stdout.strip() == 'true':
            print('Stopping the old gambit-jr-bot-1 container before starting the frozen strategy.')
            call('docker', 'stop', '--time', '30', 'gambit-jr-bot-1', capture=True)
    values = env_read(CONFIG)
    token = secrets.token_urlsafe(32)
    values.update(E4_LIVE='true', V12_LIVE_TRADING_ENABLED='true', V12_LIVE_ARM_TOKEN=token,
                  V12_LIVE_ARM_TOKEN_SHA256=hashlib.sha256(token.encode()).hexdigest())
    env_write(CONFIG, values)
    try:
        call('sudo', 'systemctl', 'enable', SERVICE)
        call('sudo', 'systemctl', 'start', SERVICE)
    except Exception:
        values.update(E4_LIVE='false', V12_LIVE_TRADING_ENABLED='false')
        env_write(CONFIG, values)
        raise
    print('Start requested. Use status to verify the live feed and trading state.')


def status():
    print('Service:', 'active' if active() else 'inactive')
    path = DATA / 'live-status.json'
    if path.exists():
        value = json.loads(path.read_text())
        value['status_age_seconds'] = round((time.time_ns() - value['updated_ns']) / 1e9, 1)
        if value['status_age_seconds'] > 10 or not active():
            value['entries_allowed'] = False
            value['status_stale_or_service_inactive'] = True
        print(json.dumps(value, indent=2))
    else:
        print('No live status yet. Run logs if startup is blocked.')


def stop():
    (DATA / 'STOP').touch(mode=0o600)
    values = env_read(CONFIG)
    values.update(E4_LIVE='false', V12_LIVE_TRADING_ENABLED='false')
    env_write(CONFIG, values)
    call('sudo', 'systemctl', 'disable', '--now', SERVICE)
    print('Stopped new entries and requested exits before service shutdown. Check status for unresolved positions.')
    status()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('command', choices=['configure', 'probe', 'shadow', 'final-check', 'final-report', 'start-live', 'status', 'logs', 'stop', 'clear-stop', '_run-live'])
    parser.add_argument('--send-test-alert', action='store_true')
    args = parser.parse_args()
    if args.command == '_run-live':
        os.execvp('docker', docker_command('live'))
    elif args.command == 'logs':
        call('journalctl', '-u', SERVICE, '-n', '60', '--no-pager')
    elif args.command == 'clear-stop':
        if active():
            raise RuntimeError('Stop the service first')
        (DATA / 'STOP').unlink(missing_ok=True)
        print('Operator stop cleared; trading remains disabled until start-live.')
    elif args.command == 'final-check':
        final_check(send_test_alert=args.send_test_alert)
    else:
        globals()[args.command.replace('-', '_')]()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, FileNotFoundError, subprocess.CalledProcessError) as error:
        # Child errors print only their exit status; never echo credential-bearing environment.
        print('BLOCKED:', str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise SystemExit(1) from None
