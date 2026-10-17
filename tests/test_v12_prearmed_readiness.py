import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def cli(monkeypatch):
    if sys.platform == 'win32':
        module = ModuleType('fcntl')
        def unavailable(*a):
            raise AssertionError('No real Linux lock is exercised by these unit tests')
        module.flock = unavailable
        module.LOCK_EX, module.LOCK_NB, module.LOCK_UN = 2, 4, 8
        monkeypatch.setitem(sys.modules, 'fcntl', module)
    return importlib.import_module('memecoin_bot.v12_prearmed')


@pytest.mark.parametrize('first,second', [('false', 'true'), ('true', 'false'), ('false', 'false')])
def test_live_requires_both_enable_flags(cli, monkeypatch, tmp_path, first, second):
    monkeypatch.setenv('E4_LIVE', first)
    monkeypatch.setenv('V12_LIVE_TRADING_ENABLED', second)
    monkeypatch.setattr(cli, 'verify_source', lambda root: None)
    with pytest.raises(PermissionError, match='BOTH_LIVE_ENABLE_FLAGS_REQUIRED'):
        cli.local_readiness(SimpleNamespace(), tmp_path, tmp_path / 'model', live=True)


def test_read_only_probe_cannot_request_live_lock_or_signer_file(cli, monkeypatch, tmp_path):
    gate = importlib.import_module('memecoin_bot.v12_live_readiness')
    monkeypatch.setattr(cli, 'verify_source', lambda root: None)
    captured = {}
    def readiness(settings, **kwargs):
        captured.update(kwargs)
        return [], None
    monkeypatch.setattr(gate, 'run_live_readiness', readiness)
    cli.local_readiness(SimpleNamespace(), tmp_path, tmp_path / 'model', live=False)
    assert captured == dict(repository_root=tmp_path, frozen_model_path=tmp_path / 'model',
        acquire_lock=False, check_arming=False, inspect_signer_file=False)


def test_original_auto_gate_is_not_changed_by_manual_acceptance(cli):
    gate = importlib.import_module('memecoin_bot.v12_live_readiness')
    fixture = Path(__file__).parent / 'fixtures/v12-approved-fresh-100.json'
    assert gate._causal_gate(fixture)[0] is False


def test_incomplete_signed_manifest_cannot_skip_runtime_files(cli, monkeypatch, tmp_path):
    src = tmp_path / 'src/memecoin_bot'
    src.mkdir(parents=True)
    (src / 'critical.py').write_text('pass')
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({'files': {'something-else': '00'}}))
    monkeypatch.setenv('V12_INTEGRITY_MANIFEST', str(path))
    with pytest.raises(RuntimeError, match='INCOMPLETE_CODE_INTEGRITY_COVERAGE'):
        cli.verify_source(tmp_path)
