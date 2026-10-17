"""Exercise both public launch paths with all operational dependencies mocked."""
from __future__ import annotations

import importlib
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def harness(monkeypatch):
    if sys.platform == 'win32':
        # The launcher contract does not exercise Linux file locks. Any accidental
        # lock call must fail rather than becoming a permissive production shim.
        fcntl = ModuleType('fcntl')
        def unavailable(*args, **kwargs):
            raise AssertionError('Linux flock is outside this mocked launcher test')
        fcntl.flock = unavailable
        fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN = 2, 4, 8
        monkeypatch.setitem(sys.modules, 'fcntl', fcntl)
    for name in ('E4_LIVE', 'V12_LIVE_TRADING_ENABLED', 'E4_PIPELINES_BACKGROUND',
                 'E4_V10_SERVICES_ENABLED', 'V12_NOTIFICATIONS_ENABLED'):
        monkeypatch.setenv(name, 'false')
    launcher = importlib.import_module('memecoin_bot.e4_exec.__main__')
    final = importlib.import_module('memecoin_bot.e4_final')
    readiness = importlib.import_module('memecoin_bot.v12_live_readiness')
    pipelines = importlib.import_module('memecoin_bot.e4_pipeline_runtime_v10')
    services = importlib.import_module('memecoin_bot.e4_runtime_services_v10')
    calls = []
    control = {'fail_readiness': False}
    settings = SimpleNamespace(live=False, validate=lambda: calls.append('settings'))
    monkeypatch.setattr(launcher.e4_hardening_v12.core.Settings, 'from_env', lambda: settings)
    monkeypatch.setattr(launcher.atexit, 'register', lambda callback: None)

    def gate(*args, **kwargs):
        calls.append('readiness')
        if control['fail_readiness']:
            raise RuntimeError('readiness blocked')
        return [], None

    def command():
        calls.append('command')

    for obj, name, fn in (
        (launcher, '_run_command', command), (final, 'main', command),
        (launcher, 'run_live_readiness', gate), (readiness, 'run_live_readiness', gate),
        (launcher, 'start_runtime_services', lambda: calls.append('services')),
        (services, 'start_runtime_services', lambda: calls.append('services')),
        (launcher, 'start_background_supervisor', lambda: calls.append('pipelines')),
        (pipelines, 'start_background_supervisor', lambda: calls.append('pipelines')),
    ):
        monkeypatch.setattr(obj, name, fn)

    def invoke(path, args, enabled=False):
        monkeypatch.setenv('E4_LIVE', 'true' if enabled else 'false')
        monkeypatch.setattr(sys, 'argv', ['gambit-e4', *args])
        if path == 'console':
            launcher.main()
        else:
            runpy.run_module('memecoin_bot.e4_exec.__main__', run_name='__main__')

    return calls, invoke, control


@pytest.mark.parametrize('path', ['console', 'module'])
@pytest.mark.parametrize('flag', ['--live', '--li'])
def test_readiness_precedes_all_startup(harness, path, flag):
    calls, invoke, _ = harness
    invoke(path, ['run', flag], enabled=True)
    assert calls == ['settings', 'readiness', 'services', 'pipelines', 'command']


@pytest.mark.parametrize('path', ['console', 'module'])
def test_failed_readiness_starts_nothing(harness, path):
    calls, invoke, control = harness
    control['fail_readiness'] = True
    with pytest.raises(RuntimeError, match='readiness blocked'):
        invoke(path, ['run', '--live'], enabled=True)
    assert calls == ['settings', 'readiness']


@pytest.mark.parametrize('path', ['console', 'module'])
@pytest.mark.parametrize(('args', 'enabled'), [(['run'], True), (['run', '--live'], False)])
def test_both_enable_requirements_are_mandatory(harness, path, args, enabled):
    calls, invoke, _ = harness
    with pytest.raises(SystemExit, match='requires both'):
        invoke(path, args, enabled)
    assert calls == []


@pytest.mark.parametrize('path', ['console', 'module'])
@pytest.mark.parametrize('command', ['status', 'migrate'])
def test_nontrading_commands_start_no_services(harness, path, command):
    calls, invoke, _ = harness
    invoke(path, [command])
    assert calls == ['command']
