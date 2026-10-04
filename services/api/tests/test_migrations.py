from __future__ import annotations
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

API = Path(__file__).resolve().parents[1]

def test_alembic_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / 'migrated.sqlite3'
    env = {**os.environ, 'PF_DATABASE_URL': f'sqlite:///{path}', 'PF_DATA_DIR': str(tmp_path)}
    def run(*args):
        result = subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=API, env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    run('upgrade', 'head')
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'procurement_requests', 'documents', 'quotes', 'quote_versions', 'approvals', 'external_operations', 'outbox', 'audit_events', 'table_imports'} <= tables
    run('downgrade', 'base')
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables <= {'alembic_version'}
    run('upgrade', 'head')


def test_advice_migration_preserves_existing_procurement_data(tmp_path):
    path = tmp_path / 'existing.sqlite3'
    env = {**os.environ, 'PF_DATABASE_URL': f'sqlite:///{path}', 'PF_DATA_DIR': str(tmp_path)}
    def run(*args):
        result = subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=API, env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    run('upgrade', '426d852ce82c')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO procurement_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ('req_existing', 'demo', 'buyer-01', '{"title":"Preserve"}', 7, 'APPROVED', None, '2026-01-01'))
    run('upgrade', 'head')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version, status, data FROM procurement_requests").fetchone() == (
            7, 'APPROVED', '{"title":"Preserve"}')
        assert db.execute("SELECT count(*) FROM advice_runs").fetchone() == (0,)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ('b9134d27c80f',)
    run('downgrade', '426d852ce82c')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM procurement_requests").fetchone() == ('req_existing',)
    run('upgrade', 'head')


def test_policy_migration_preserves_legacy_advice_and_approval_without_inventing_inputs(tmp_path):
    path = tmp_path / 'legacy-bound-inputs.sqlite3'
    env = {**os.environ, 'PF_DATABASE_URL': f'sqlite:///{path}', 'PF_DATA_DIR': str(tmp_path)}
    def run(*args):
        result = subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=API, env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    run('upgrade', '5ce3ab9b84a2')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO procurement_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ('req_old', 'demo', 'buyer-01', '{"title":"Preserve"}', 7, 'APPROVED',
                    '{"snapshot_hash":"legacy"}', '2026-01-01'))
        db.execute("INSERT INTO advice_runs (id, tenant_id, request_id, actor_id, idempotency_key, request_version, "
                   "input_hash, status, output, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ('adv_old', 'demo', 'req_old', 'buyer-01', 'legacy-key', 7, 'a' * 64,
                    'COMPLETED', '{"summary":"Original advisory text"}', '2026-01-01'))
        db.execute("INSERT INTO approvals (id, request_id, tenant_id, approver_id, snapshot_hash, status, note, "
                   "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ('ap_old', 'req_old', 'demo', 'approver-01', 'b' * 64, 'APPROVED', 'Original note',
                    '2026-01-02', '2026-01-01'))
    run('upgrade', 'head')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status, output, input_snapshot FROM advice_runs").fetchone() == (
            'COMPLETED', '{"summary":"Original advisory text"}', None)
        assert db.execute("SELECT status, note, snapshot FROM approvals").fetchone() == ('APPROVED', 'Original note', None)
        assert db.execute("SELECT proposal FROM procurement_requests").fetchone() == ('{"snapshot_hash":"legacy"}',)
        assert db.execute("SELECT count(*) FROM evaluations").fetchone() == (0,)
    run('downgrade', '5ce3ab9b84a2')
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT output FROM advice_runs").fetchone() == ('{"summary":"Original advisory text"}',)
        assert db.execute("SELECT note FROM approvals").fetchone() == ('Original note',)
