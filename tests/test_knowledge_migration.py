"""Real Alembic upgrade/data-preserving downgrade safety on disposable SQLite."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
API=Path(__file__).resolve().parents[1]/'services/api'

def migration(tmp_path,*args):
    env={k:v for k,v in os.environ.items() if not k.startswith(('PF_','LLM_','ERP_'))}
    env.update(PYTHONPATH=str(API),PF_DATA_DIR=str(tmp_path),PF_DATABASE_URL=f'sqlite:///{tmp_path}/migration.sqlite3')
    return subprocess.run([sys.executable,'-m','alembic',*args],cwd=API,env=env,capture_output=True,timeout=30)

def test_upgrade_preserves_prior_request_and_empty_downgrade(tmp_path):
    assert migration(tmp_path,'upgrade','a40c72de1902').returncode==0
    with sqlite3.connect(tmp_path/'migration.sqlite3') as db:
        db.execute('INSERT INTO procurement_requests VALUES(?,?,?,?,?,?,?,?)',('r','demo','buyer','{}',7,'DRAFT',None,'2026-01-01'))
    assert migration(tmp_path,'upgrade','head').returncode==0
    with sqlite3.connect(tmp_path/'migration.sqlite3') as db:
        assert db.execute('SELECT version FROM procurement_requests').fetchone()==(7,)
        assert db.execute('SELECT COUNT(*) FROM knowledge_versions').fetchone()==(0,)
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()==('b51d09e37a24',)
    assert migration(tmp_path,'downgrade','a40c72de1902').returncode==0
    assert migration(tmp_path,'upgrade','head').returncode==0

def test_nonempty_knowledge_downgrade_refuses_data_loss(tmp_path):
    assert migration(tmp_path,'upgrade','head').returncode==0
    with sqlite3.connect(tmp_path/'migration.sqlite3') as db:
        db.execute('INSERT INTO knowledge_documents VALUES(?,?,?,?)',('d','demo','doc',1))
        db.execute('INSERT INTO knowledge_versions (id,tenant_id,document_id,version,title,scope,text,content_hash,record_hash,chunks,state,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('v','demo','d',1,'title','procurement','text','a'*64,'b'*64,'[]','DRAFT','2026-01-01','author'))
    result=migration(tmp_path,'downgrade','a40c72de1902')
    assert result.returncode!=0 and b'KNOWLEDGE_DATA_DOWNGRADE_REFUSED' in result.stderr
    with sqlite3.connect(tmp_path/'migration.sqlite3') as db:
        assert db.execute('SELECT text FROM knowledge_versions').fetchone()==('text',)
