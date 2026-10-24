"""Full parent application, authentication/middleware and schema integration."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'services/api'))
from fastapi.testclient import TestClient
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.db import Database
from procureflow.policy_documents import PolicyDocumentService,PublishDocument


def test_full_api_mount_and_maintenance(tmp_path):
    settings=Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        headers={'Authorization':'Bearer demo-approver'}
        body={'expected_version':0,'title':'验收文档','source_label':'synthetic','content':'运费需要确认。','reason':'完整应用测试'}
        assert client.get('/api/v1/policy-library/documents').status_code==401
        assert client.post('/api/v1/policy-library/documents/test/versions',headers=headers,json=body).status_code==201
        response=client.post('/api/v1/policy-library/search',headers=headers,json={'query':'运费'})
        assert response.status_code==200 and response.json()['results']
        assert response.headers['cache-control']=='no-store'
        schema=client.get('/api/v1/policy-library/openapi.json',headers=headers)
        assert schema.status_code==200
        import json
        expected=json.loads((Path(__file__).resolve().parents[3]/'packages/contracts/policy-library.openapi.json').read_text())
        assert schema.json()==expected
        assert schema.json()['components']['securitySchemes']['HTTPBearer']['scheme']=='bearer'
        from sqlalchemy import text
        with client.app.state.service.db.operator_transaction() as s:
            s.execute(text("UPDATE system_state SET state='PAUSED' WHERE id=1"))
        assert client.post('/api/v1/policy-library/documents/test/versions',headers=headers,json=body).status_code==503
