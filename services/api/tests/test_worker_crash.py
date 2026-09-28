"""Actual SIGKILL before/after simulated external commit; lease expiry is injected."""
import json, os, signal, subprocess, sys, time
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.db import Database, OperationRow
from procureflow.erp import MockERP
from conftest import approved, enqueue, BUYER

@pytest.mark.parametrize('point,expected,count',[('before-create','NEEDS_HUMAN',0),('after-commit','COMPLETED',1)])
def test_sigkill_worker_recovery_is_reconciliation_only(tmp_path,point,expected,count):
    if os.name!='posix': pytest.skip('SIGKILL validation requires POSIX; not equivalent to Windows terminate')
    settings=Settings(data_dir=tmp_path,database_url=f'sqlite:///{tmp_path}/business.sqlite3')
    db=Database(settings.database_url,create_schema=True);erp=MockERP(tmp_path/'mock-erp.sqlite3')
    root=Path(__file__).resolve().parents[3]
    marker=tmp_path/'kill-marker'
    env={**os.environ,'PF_DATA_DIR':str(tmp_path),'PF_DATABASE_URL':settings.database_url,'PF_MODE':'demo','PF_ERP_MODE':'mock',
         'ERP_ALLOW_DRAFT_WRITES':'false','PYTHONPATH':str(root/'services/api'),
         'PF_TEST_KILL_MARKER':str(marker),'PF_TEST_KILL_POINT':point}
    env.pop('PF_AUTH_TOKENS',None)
    with TestClient(create_app(settings,database=db,erp=erp)) as client:
        request,quote,proposal=approved(client);operation=enqueue(client,request,proposal)
        with (tmp_path/'worker-before-kill.log').open('w') as log:
            worker=subprocess.Popen([sys.executable,str(Path(__file__).parent/'helpers/killpoint_worker.py')],env=env,cwd=root,stdout=log,stderr=log)
            try:
                for _ in range(150):
                    if marker.exists(): break
                    if worker.poll() is not None: raise AssertionError((tmp_path/'worker-before-kill.log').read_text())
                    time.sleep(.02)
                else: raise AssertionError('worker did not reach the test killpoint')
                os.kill(worker.pid,signal.SIGKILL);worker.wait(timeout=5)
                assert worker.returncode==-signal.SIGKILL
            finally:
                if worker.poll() is None: worker.kill();worker.wait()
        interrupted=client.get(f'/api/v1/operations/{operation["id"]}',headers=BUYER).json()
        assert interrupted['status']=='IN_FLIGHT' and erp.count()==count
        # Advance the lease by controlled DB injection; do not claim real elapsed-time testing.
        with db.transaction(write=True) as session:
            session.get(OperationRow,operation['id']).lease_until='2000-01-01T00:00:00+00:00'
        recovery=subprocess.run([sys.executable,'-m','procureflow.worker','--once'],env=env,cwd=root,capture_output=True,text=True,timeout=15)
        assert recovery.returncode==0,recovery.stdout+recovery.stderr
        final=client.get(f'/api/v1/operations/{operation["id"]}',headers=BUYER).json()
        assert final['status']==expected and erp.count()==count
        assert final['attempts']==2
        events=client.get(f'/api/v1/requests/{request["id"]}/events',headers=BUYER).json()
        dispatches=[e for e in events if e['type']=='ERP_DISPATCH_STARTED']
        assert dispatches[-1]['payload']['mode']=='read_only_reconciliation'
        if point=='before-create': assert final['error']=='REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT'
        report={'point':point,'signal':'SIGKILL','child_returncode':worker.returncode,'lease_expiry_injected':True,
                'status':final['status'],'mock_draft_count':count,'live_erp':False,'attempts':final['attempts']}
        output=os.getenv('PF_CRASH_REPORT_DIR')
        if output:
            path=Path(output);path.mkdir(parents=True,exist_ok=True)
            (path/(point+'.json')).write_text(json.dumps(report,indent=2)+'\n')
