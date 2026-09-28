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
    assert {'procurement_requests', 'documents', 'quotes', 'quote_versions', 'approvals', 'external_operations', 'outbox', 'audit_events'} <= tables
    run('downgrade', 'base')
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables <= {'alembic_version'}
    run('upgrade', 'head')
