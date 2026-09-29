"""Local runtime regressions; these do NOT substitute for real ERP acceptance."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def runtime():
    spec = importlib.util.spec_from_file_location('lab_runtime_test',
        ROOT / 'integrations/erpnext/sandbox/lab_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lab(tmp_path, monkeypatch):
    module = runtime()
    bench = tmp_path / 'bench'
    sites = bench / 'sites'
    (sites / module.SITE / 'logs').mkdir(parents=True)
    (bench / 'logs').mkdir()
    calls = []
    def connect():
        calls.append('connect')
        # Reproduce Frappe's real relative log paths, not a mocked path assertion.
        Path('../logs/database.log').touch()
        Path(module.SITE, 'logs/database.log').touch()
    frappe = SimpleNamespace(conf={'procureflow_test_nonce': 'a' * 64},
        init=lambda **kwargs: calls.append(('init', kwargs)), connect=connect,
        set_user=lambda user: calls.append(('user', user)), destroy=lambda: calls.append('destroy'))
    monkeypatch.setitem(sys.modules, 'frappe', frappe)
    monkeypatch.setattr(module, 'SITES', sites)
    monkeypatch.setenv('PF_EPHEMERAL_ERP', '1')
    monkeypatch.setenv('PF_EPHEMERAL_NONCE', 'a' * 64)
    monkeypatch.chdir(bench)
    return module, frappe, calls, bench, sites


def test_sites_working_directory_fixes_both_log_paths_and_restores_caller(lab):
    module, frappe, calls, bench, sites = lab
    with pytest.raises(FileNotFoundError):
        Path('../logs/database.log').touch()  # Previous launch directory fails.
    with module.lab_session():
        assert Path.cwd() == sites
        assert (bench / 'logs/database.log').is_file()
        assert (sites / module.SITE / 'logs/database.log').is_file()
    assert Path.cwd() == bench
    assert ('user', 'Administrator') in calls and calls[-1] == 'destroy'
    assert not (bench.parent / 'logs').exists()


@pytest.mark.parametrize('nonce', ['', 'a'*63, 'a'*65, 'g'*64])
def test_invalid_nonce_never_initializes_or_connects(lab, monkeypatch, nonce):
    module, _, calls, bench, _ = lab
    monkeypatch.setenv('PF_EPHEMERAL_NONCE', nonce)
    with pytest.raises(RuntimeError, match='EPHEMERAL_LAB_ONLY'):
        with module.lab_session():
            pytest.fail('must not enter')
    assert calls == [] and Path.cwd() == bench


def test_missing_opt_in_never_initializes_or_connects(lab, monkeypatch):
    module, _, calls, _, _ = lab
    monkeypatch.delenv('PF_EPHEMERAL_ERP')
    with pytest.raises(RuntimeError, match='EPHEMERAL_LAB_ONLY'):
        with module.lab_session():
            pytest.fail('must not enter')
    assert not calls


def test_marker_checked_before_database_connect(lab):
    module, frappe, calls, bench, _ = lab
    frappe.conf['procureflow_test_nonce'] = 'b' * 64
    with pytest.raises(RuntimeError, match='LAB_MARKER_MISMATCH'):
        with module.lab_session():
            pytest.fail('must not enter')
    assert 'connect' not in calls and calls[-1] == 'destroy'
    assert ('user', 'Administrator') not in calls and Path.cwd() == bench


@pytest.mark.parametrize('where', ['init', 'connect', 'body'])
def test_failed_initialization_or_body_always_restores_context(lab, where):
    module, frappe, calls, bench, _ = lab
    def fail(**kwargs):
        raise ValueError('synthetic failure')
    if where != 'body':
        setattr(frappe, where, fail)
    with pytest.raises(ValueError):
        with module.lab_session():
            fail()
    assert calls[-1] == 'destroy' and Path.cwd() == bench


def test_diagnostics_discard_large_output_and_do_not_serialize_exception_secrets(capsys):
    module = runtime()
    canary = 'CANARY_SECRET_DO_NOT_PUBLISH'
    def fail():
        module.phase('integration-user')
        print(canary * 100_000)
        print(canary, file=sys.stderr)
        raise ValueError(canary)
    assert module.run_stage('seed', fail) == 1
    capture = capsys.readouterr()
    assert canary not in capture.out + capture.err and len(capture.out) < 2048
    report = json.loads(capture.out)
    assert report['status'] == 'failed' and report['phase'] == 'integration-user'
    assert report['error_type'] == 'ValueError' and report['reason'] == 'STAGE_FAILED'
    assert all(set(f) == {'file', 'line'} for f in report['frames'])


def test_guard_code_preserved_and_success_machine_readable(capsys):
    module = runtime()
    def fail():
        raise RuntimeError('LAB_MARKER_MISMATCH')
    assert module.run_stage('seed', fail) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'LAB_MARKER_MISMATCH'
    assert module.run_stage('database-audit', lambda: {'draft_count': 2}) == 0
    assert json.loads(capsys.readouterr().out) == {
        'stage': 'database-audit', 'status': 'passed', 'draft_count': 2}
