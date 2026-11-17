"""Actual Alembic upgrade/downgrade against fresh temporary SQLite databases."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
API=Path(__file__).resolve().parents[1]/'services/api'


def test_upgrade_preserves_rows_and_downgrade_empty(tmp_path):
    path=tmp_path/'test.sqlite'
    env={k:v for k,v in os.environ.items() if not k.startswith(('PF_','ERP_','LLM_'))}
    env.update(PF_DATABASE_URL=f'sqlite:///{path}',PF_DATA_DIR=str(tmp_path),PYTHONPATH=str(API))
    def migrate(*args):
        result=subprocess.run([sys.executable,'-m','alembic',*args],cwd=API,env=env,capture_output=True,timeout=30)
        assert result.returncode==0,result.stderr.decode()
    migrate('upgrade','a40c72de1902')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO procurement_requests(id,tenant_id,owner_id,data,version,status,created_at) VALUES ('old','demo','buyer','{}',1,'DRAFT','2026-01-01')")
    migrate('upgrade','head')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id FROM procurement_requests').fetchone()==('old',)
        assert db.execute('SELECT COUNT(*) FROM policy_documents').fetchone()==(0,)
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()==('b719aa310001',)
    migrate('downgrade','a40c72de1902')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id FROM procurement_requests').fetchone()==('old',)
    migrate('upgrade','head')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO policy_documents(id,tenant_id,document_key,latest_version) VALUES ('pd1','demo','old',1)")
    result=subprocess.run([sys.executable,'-m','alembic','downgrade','a40c72de1902'],cwd=API,env=env,capture_output=True,timeout=30)
    assert result.returncode!=0 and b'POLICY_DOCUMENT_HISTORY_DOWNGRADE_REFUSED' in result.stderr
