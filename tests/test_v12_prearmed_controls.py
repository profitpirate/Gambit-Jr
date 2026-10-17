import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest


@pytest.fixture
def control(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / 'deployment/prearmed-control.py'
    spec = importlib.util.spec_from_file_location('prearmed_control_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STAGE', tmp_path)
    monkeypatch.setattr(module, 'CONFIG', tmp_path / 'config.env')
    monkeypatch.setattr(module, 'DATA', tmp_path)
    monkeypatch.setattr(module, 'active', lambda: False)
    (tmp_path / 'prearmed-install.json').write_text(json.dumps(dict(offline_tests_passed=True, image_id='image')))
    (tmp_path / 'shadow-pass.json').write_text(json.dumps(dict(image_id='image', observed_ns=time.time_ns())))
    module.env_write(module.CONFIG, dict(E4_LIVE='false', V12_LIVE_TRADING_ENABLED='false'))
    (tmp_path / 'final-check-pass.json').write_text(json.dumps(dict(status='FINAL_TECHNICAL_CHECKS_PASS',
        image_id='image', observed_ns=time.time_ns(), config_sha256=module.config_fingerprint())))
    return module


def test_failed_probe_never_stops_legacy_or_enables_trading(control, monkeypatch):
    def fail():
        raise RuntimeError('probe failed')
    monkeypatch.setattr(control, 'probe', fail)
    monkeypatch.setattr(control, 'call', lambda *a, **k: pytest.fail('service operation attempted'))
    with pytest.raises(RuntimeError, match='probe failed'):
        control.start_live()
    assert control.env_read(control.CONFIG)['E4_LIVE'] == 'false'


def test_stale_shadow_check_cannot_start_live(control, monkeypatch):
    (control.DATA / 'shadow-pass.json').write_text(json.dumps(dict(image_id='old-image', observed_ns=time.time_ns())))
    monkeypatch.setattr(control, 'probe', lambda: pytest.fail('probe should not be reached'))
    with pytest.raises(RuntimeError, match='Run shadow again'):
        control.start_live()


def test_service_start_failure_restores_disabled_flags(control, monkeypatch):
    monkeypatch.setattr(control, 'probe', lambda: None)
    calls = []
    def invoke(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('sudo', 'systemctl', 'start'):
            assert control.env_read(control.CONFIG)['E4_LIVE'] == 'true'
            raise RuntimeError('service failed')
        return SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(control, 'call', invoke)
    with pytest.raises(RuntimeError, match='service failed'):
        control.start_live()
    assert control.env_read(control.CONFIG)['E4_LIVE'] == 'false'
    assert control.env_read(control.CONFIG)['V12_LIVE_TRADING_ENABLED'] == 'false'


def test_environment_values_cannot_inject_enable_flags(control):
    with pytest.raises(ValueError):
        control.env_write(control.CONFIG, {'TOKEN': 'value\nE4_LIVE=true'})
    assert control.env_read(control.CONFIG)['E4_LIVE'] == 'false'


@pytest.mark.parametrize('change', ['missing', 'stale', 'future', 'different_config'])
def test_final_check_must_be_fresh_and_match_configuration(control, monkeypatch, change):
    path = control.DATA / 'final-check-pass.json'
    if change == 'missing':
        path.unlink()
    else:
        report = json.loads(path.read_text())
        if change == 'different_config':
            report['config_sha256'] = 'wrong'
        else:
            report['observed_ns'] += (-4_000_000_000_000 if change == 'stale' else 4_000_000_000_000)
        path.write_text(json.dumps(report))
    monkeypatch.setattr(control, 'probe', lambda: pytest.fail('probe should not run'))
    monkeypatch.setattr(control, 'call', lambda *a, **k: pytest.fail('service operation attempted'))
    with pytest.raises(RuntimeError):
        control.start_live()
    assert control.env_read(control.CONFIG)['E4_LIVE'] == 'false'


def test_legacy_restart_is_disabled_before_live_activation(control, monkeypatch):
    monkeypatch.setattr(control, 'probe', lambda: None)
    calls = []
    def invoke(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='true' if args[:2] == ('docker', 'inspect') else '')
    monkeypatch.setattr(control, 'call', invoke)
    control.start_live()
    assert calls.index(('docker', 'update', '--restart=no', 'gambit-jr-bot-1')) < calls.index(('docker', 'stop', '--time', '30', 'gambit-jr-bot-1'))
    assert calls.index(('docker', 'stop', '--time', '30', 'gambit-jr-bot-1')) < calls.index(('sudo', 'systemctl', 'start', control.SERVICE))


def test_shadow_cannot_pass_using_old_status(control, monkeypatch):
    (control.DATA / 'shadow-status.json').write_text(json.dumps(dict(feed_connected=True,
        feed_events=10, safety_mode='ACTIVE', updated_ns=1)))
    monkeypatch.setattr(control, 'docker_command', lambda *a, **kw: ['test'])
    monkeypatch.setattr(control, 'call', lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match='observation did not pass'):
        control.shadow()


@pytest.mark.parametrize('network_failure', [False, True])
def test_final_check_separates_key_access_from_network_and_never_activates(control, monkeypatch, network_failure):
    release = Path(control.__file__).resolve().parents[1]
    (control.STAGE / 'prearmed-install.json').write_text(json.dumps(dict(offline_tests_passed=True,
        image_id='image', release=str(release))))
    monkeypatch.setattr(control, 'docker_command', lambda mode: ['docker', 'run', 'image', mode])
    commands = []
    def invoke(*args, **kwargs):
        commands.append(args)
        if args[0] == 'timedatectl':
            return SimpleNamespace(returncode=0, stdout='yes')
        if args[:2] == ('docker', 'inspect'):
            return SimpleNamespace(returncode=1, stdout='')
        assert args[:2] == ('docker', 'run')
        if '--offline' in args:
            assert '--network=none' in args
            assert any('trading-keypair.json' in str(a) for a in args)
            kind = 'offline'
        else:
            assert '--send-test-alert' in args
            assert not any('trading-keypair.json' in str(a) for a in args)
            if network_failure:
                raise RuntimeError('test alert delivery failed')
            kind = 'network'
        (control.DATA / ('final-' + kind + '.json')).write_text(json.dumps({'passed': True}))
    monkeypatch.setattr(control, 'call', invoke)
    def shadow(**kwargs):
        assert kwargs['seconds'] == 120
        (control.DATA / 'shadow-status.json').write_text(json.dumps({'mode': 'shadow'}))
    monkeypatch.setattr(control, 'shadow', shadow)
    if network_failure:
        with pytest.raises(RuntimeError, match='delivery failed'):
            control.final_check(send_test_alert=True)
        assert not (control.DATA / 'final-check-pass.json').exists()
    else:
        control.final_check(send_test_alert=True)
        report = json.loads((control.DATA / 'final-check-pass.json').read_text())
        assert report['status'] == 'FINAL_TECHNICAL_CHECKS_PASS'
    assert control.env_read(control.CONFIG)['E4_LIVE'] == 'false'
    assert not any('sudo' in args or 'start' in args or 'enable' in args for args in commands)
