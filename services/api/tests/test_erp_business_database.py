"""Fail-closed storage configuration tests; no live ERP claims."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import erp_business_database as storage


@pytest.mark.parametrize('url', [
    '', 'SECRET_CANARY', 'sqlite:///SECRET_CANARY',
    'postgresql://u:SECRET_CANARY@erp.example.com/procureflow_test',
    'postgresql://u:SECRET_CANARY@127.0.0.1/production',
    'postgresql://u:SECRET_CANARY@127.0.0.1/procureflow_test?options=-csearch_path=public',
    'postgresql://u:SECRET_CANARY@127.0.0.1/procureflow_test?host=production',
    'postgresql://u:SECRET_CANARY@/procureflow_test',
    'postgresql+asyncpg://u:SECRET_CANARY@127.0.0.1/procureflow_test',
])
def test_nonlocal_non_test_and_ambiguous_postgres_urls_fail_without_echo(url):
    with pytest.raises(ValueError) as error:
        storage.postgres_test_url({'PF_ALLOW_DATABASE_TESTS': '1', 'PF_TEST_DATABASE_URL': url})
    assert str(error.value) == 'POSTGRES_LAB_CONFIGURATION_REQUIRED'


def test_postgres_opt_in_does_not_inherit_application_database():
    with pytest.raises(ValueError, match='POSTGRES_LAB_CONFIGURATION_REQUIRED'):
        storage.postgres_test_url({'PF_DATABASE_URL': 'postgresql://localhost/production'})
    with pytest.raises(ValueError, match='POSTGRES_LAB_CONFIGURATION_REQUIRED'):
        storage.postgres_test_url({'PF_TEST_DATABASE_URL': 'postgresql://127.0.0.1/procureflow_test'})
    value = storage.postgres_test_url({'PF_ALLOW_DATABASE_TESTS': '1',
        'PF_TEST_DATABASE_URL': 'postgresql://127.0.0.1/procureflow_test'})
    assert value.drivername == 'postgresql+psycopg'


def test_sqlite_remains_disposable_and_never_opens_postgres(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'create_engine', lambda *a, **k: pytest.fail('network'))
    with storage.business_database('sqlite', str(tmp_path)) as url:
        assert make_url(url).database == str(tmp_path / 'business.sqlite3')


@pytest.mark.parametrize('fail_inside', [False, True])
def test_postgres_owns_only_random_schema_and_cleans_on_failure(monkeypatch, fail_inside):
    monkeypatch.setenv('PF_ALLOW_DATABASE_TESTS', '1')
    monkeypatch.setenv('PF_TEST_DATABASE_URL', 'postgresql://127.0.0.1/procureflow_test')
    statements = []
    class Engine:
        def begin(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalar(self, query): return 'procureflow_test'
        def execute(self, query): statements.append(str(query))
        def dispose(self): statements.append('disposed')
    monkeypatch.setattr(storage, 'create_engine', lambda *a, **k: Engine())
    try:
        with storage.business_database('postgresql', '/unused') as url:
            parsed = make_url(url)
            schema = parsed.query['options'].split('=', 1)[1]
            assert schema.startswith('pf_erp_test_') and len(schema) == 44
            if fail_inside:
                raise RuntimeError('test failure')
    except RuntimeError:
        assert fail_inside
    assert statements == [f'CREATE SCHEMA "{schema}"', f'DROP SCHEMA "{schema}" CASCADE', 'disposed']


def test_failed_schema_creation_never_drops_anything(monkeypatch):
    monkeypatch.setenv('PF_ALLOW_DATABASE_TESTS', '1')
    monkeypatch.setenv('PF_TEST_DATABASE_URL', 'postgresql://127.0.0.1/procureflow_test')
    statements = []
    class Engine:
        def begin(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalar(self, query): return 'procureflow_test'
        def execute(self, query):
            statements.append(str(query))
            raise RuntimeError('schema create failed')
        def dispose(self): statements.append('disposed')
    monkeypatch.setattr(storage, 'create_engine', lambda *a, **k: Engine())
    with pytest.raises(RuntimeError):
        with storage.business_database('postgresql', '/unused'):
            pytest.fail('must not yield')
    assert len(statements) == 2 and statements[0].startswith('CREATE SCHEMA ') and statements[1] == 'disposed'


@pytest.mark.parametrize('url', ['sqlite:///unused', 'postgresql+psycopg://127.0.0.1/procureflow_test',
    'postgresql+psycopg://127.0.0.1/procureflow_test?options=-csearch_path=public'])
def test_audit_refuses_unisolated_schema_before_network(monkeypatch, url):
    monkeypatch.setattr(storage, 'create_engine', lambda *a, **k: pytest.fail('network'))
    with pytest.raises(ValueError, match='POSTGRES_LAB_SCHEMA_REQUIRED'):
        storage.audit_business_database(url, [])


def test_postgres_cli_configuration_failure_is_blocked_before_network(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('combined_gate_test', ROOT / 'scripts/verify_erp_sandbox.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'validate_lab', lambda *_: {})
    monkeypatch.delenv('PF_ALLOW_DATABASE_TESTS', raising=False)
    monkeypatch.setattr(runner, 'exercise', lambda *_: pytest.fail('network'))
    out = tmp_path / 'out.json'
    assert runner.main(['--ephemeral-test', '--business-database', 'postgresql', '--output', str(out)]) == 2
    assert json.loads(out.read_text())['network_attempted'] is False


@pytest.mark.parametrize('key', ['PGHOSTADDR', 'PGHOST', 'PGPORT', 'PGSERVICE', 'PGSERVICEFILE',
    'PGSYSCONFDIR', 'PGOPTIONS', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGPASSFILE', 'PGSSLMODE'])
def test_ambient_libpq_overrides_rejected_before_engine_creation(monkeypatch, key):
    monkeypatch.setenv('PF_ALLOW_DATABASE_TESTS', '1')
    monkeypatch.setenv('PF_TEST_DATABASE_URL', 'postgresql://127.0.0.1/procureflow_test')
    monkeypatch.setenv(key, 'SECRET_CANARY')
    monkeypatch.setattr(storage, 'create_engine', lambda *a, **k: pytest.fail('network'))
    with pytest.raises(ValueError) as error:
        with storage.business_database('postgresql', '/unused'):
            pytest.fail('must not yield')
    assert str(error.value) == 'POSTGRES_LAB_CONFIGURATION_REQUIRED'


def test_optimized_gate_rejects_before_validating_credentials_or_networking(tmp_path):
    import subprocess
    output = tmp_path / 'optimized.json'
    result = subprocess.run([sys.executable, '-O', str(ROOT / 'scripts/verify_erp_sandbox.py'),
        '--ephemeral-test', '--output', str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert json.loads(output.read_text()) == {'status': 'blocked', 'network_attempted': False,
        'reason': 'OPTIMIZED_PYTHON_NOT_SUPPORTED'}


def test_optimized_business_audit_require_remains_fail_closed():
    import subprocess
    result = subprocess.run([sys.executable, '-O', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); import erp_business_database as s; s.require(False)',
        str(ROOT / 'scripts')], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'POSTGRES_BUSINESS_AUDIT_FAILED' in result.stderr
