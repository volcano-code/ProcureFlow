"""Migration preserves legacy candidates and refuses dropping active queue state."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest

API=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('status',['QUEUED','RUNNING'])
def test_candidate_dispatch_upgrade_and_active_downgrade_guard(tmp_path,status):
    path=tmp_path/'migration.sqlite3'
    env={k:v for k,v in os.environ.items() if not k.startswith(('PF_','ERP_','LLM_'))}
    env.update(PF_DATABASE_URL=f'sqlite:///{path}',PF_DATA_DIR=str(tmp_path),PYTHONPATH=str(API))
    def migrate(*args,success=True):
        result=subprocess.run([sys.executable,'-m','alembic',*args],cwd=API,env=env,capture_output=True,timeout=30)
        assert (result.returncode==0)==success
        return result
    migrate('upgrade','f192c4ad8601')
    with sqlite3.connect(path) as db:
        # SQLite fixture only; no production foreign-key bypass or data mutation.
        db.execute("INSERT INTO quote_candidate_runs (id,tenant_id,request_id,document_id,creator_id,idempotency_key,revision,"
                   "input_snapshot,input_hash,status,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('can_legacy','test','req_legacy','doc_legacy','buyer','migration-key',1,'{}','a'*64,'PENDING','2030-01-01','2026-01-01'))
    migrate('upgrade','head')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT revision,status,queued_at,dispatch_authority FROM quote_candidate_runs').fetchone()==(1,'PENDING',None,None)
        db.execute('UPDATE quote_candidate_runs SET status=?',(status,))
    result=migrate('downgrade','f192c4ad8601',success=False)
    assert b'CANDIDATE_DISPATCH_ACTIVE_DOWNGRADE_REFUSED' in result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()==('a40c72de1902',)
        db.execute("UPDATE quote_candidate_runs SET status='CANCELLED'")
    migrate('downgrade','f192c4ad8601')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT status,input_hash FROM quote_candidate_runs').fetchone()==('CANCELLED','a'*64)
    migrate('upgrade','head')
