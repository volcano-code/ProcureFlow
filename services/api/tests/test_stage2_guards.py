import json
import httpx
import pytest
from procureflow.agent import ReadOnlyAgent
from procureflow.config import Settings
from procureflow.errors import DomainError
from conftest import BUYER, APPROVER


def test_demo_identity_cannot_target_real_erp(tmp_path):
    with pytest.raises(ValueError, match='ERPNext requires PF_MODE=private'):
        Settings(data_dir=tmp_path, mode='demo', erp_mode='erpnext', erp_url='https://example.invalid',
                 erp_api_key='test-key', erp_api_secret='test-secret', erp_company='Test')


def test_model_null_message_is_controlled_error():
    agent = ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(
        lambda _: httpx.Response(200,json={'choices':[{'message':None}]})))
    try:
        with pytest.raises(DomainError, match='malformed'):
            agent.run(lambda *_:{},set())
    finally:
        agent.client.close()


@pytest.mark.parametrize('message', [[], {'tool_calls':[None]}, {'tool_calls':'not-a-list'},
    {'tool_calls':[{'id':3,'function':{'name':'get_comparison','arguments':'{}'}}]}, {'content':{'summary':'x'}}])
def test_malformed_model_message_fails_closed(message):
    agent = ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(
        lambda _: httpx.Response(200,json={'choices':[{'message':message}]})))
    invoked=[]
    try:
        with pytest.raises(DomainError) as exc:
            agent.run(lambda *args:invoked.append(args),set())
        assert exc.value.code == 'MODEL_PROTOCOL_INVALID' and invoked == []
    finally:
        agent.client.close()


def test_valid_json_without_comparison_is_not_claimed_as_grounded():
    agent = ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(
        lambda _: httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'summary':'看起来可行','evidence_ids':[]})}}]})))
    try:
        with pytest.raises(DomainError) as exc:
            agent.run(lambda *_:{},set())
        assert exc.value.code == 'MODEL_GROUNDING_REQUIRED'
    finally:
        agent.client.close()


def test_duplicate_tool_call_ids_rejected_before_invocation():
    tool={'id':'duplicate','function':{'name':'get_comparison','arguments':'{}'}}
    agent = ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(
        lambda _: httpx.Response(200,json={'choices':[{'message':{'tool_calls':[tool,tool]}}]})))
    invoked=[]
    try:
        with pytest.raises(DomainError) as exc:
            agent.run(lambda *args:invoked.append(args),set())
        assert exc.value.code == 'MODEL_PROTOCOL_INVALID' and invoked == []
    finally:
        agent.client.close()


def test_model_truncation_is_not_success_even_with_valid_json():
    agent = ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(
        lambda _: httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{"summary":"x"}'}}]})))
    try:
        with pytest.raises(DomainError) as exc: agent.run(lambda *_:{},set())
        assert exc.value.code == 'MODEL_OUTPUT_INCOMPLETE'
    finally: agent.client.close()


def test_model_tool_events_are_observable_without_source_text():
    n=0
    def handle(_):
        nonlocal n
        n+=1
        msg=({'tool_calls':[{'id':'c1','function':{'name':'get_comparison','arguments':'{}'}}]} if n==1
             else {'content':json.dumps({'summary':'请补充运费','evidence_ids':[]})})
        return httpx.Response(200,json={'choices':[{'message':msg}]})
    agent=ReadOnlyAgent('https://model.test','synthetic-secret','test',transport=httpx.MockTransport(handle))
    observed=[]
    try: result=agent.run(lambda *_:{'source':'sensitive source not for audit trace'},set(),observer=observed.append)
    finally: agent.client.close()
    assert [x['type'] for x in observed] == ['model_completed','tool_started','tool_completed','model_completed']
    assert observed==result['trace']
    assert 'sensitive source' not in json.dumps(observed) and 'synthetic-secret' not in json.dumps(observed)


def test_demo_samples_are_authenticated_read_only_and_allowlisted(system):
    client, service, erp=system
    assert client.get('/api/v1/demo/samples').status_code==401
    assert client.get('/api/v1/demo/samples',headers=APPROVER).status_code==403
    result=client.get('/api/v1/demo/samples',headers=BUYER).json()
    assert result['synthetic'] and len(result['files'])==3
    for name in result['files']:
        response=client.get('/api/v1/demo/samples/'+name,headers=BUYER)
        assert response.status_code==200 and len(response.content)>0
    assert client.get('/api/v1/demo/samples/secret.env',headers=BUYER).status_code==404
    assert client.get('/api/v1/requests',headers=BUYER).json()==[] and erp.count()==0
    cap=client.get('/api/v1/capabilities',headers=BUYER).json()
    assert cap['demo_samples'] and cap['erp_mode']=='mock' and cap['production_ready'] is False


def test_demo_sample_routes_disabled_in_private_mode(tmp_path):
    from procureflow.app import create_app
    from procureflow.db import Database
    from fastapi.testclient import TestClient
    token='x'*40
    settings=Settings(data_dir=tmp_path,mode='private',auth_tokens={token:{'user_id':'private-buyer','tenant_id':'test','role':'buyer'}})
    db=Database(settings.database_url,create_schema=True)
    with TestClient(create_app(settings,database=db)) as client:
        headers={'Authorization':'Bearer '+token}
        assert client.get('/api/v1/demo/samples',headers=headers).status_code==404
        assert client.get('/api/v1/demo/samples/supplier-a.txt',headers=headers).status_code==404
        assert client.get('/api/v1/capabilities',headers=headers).json()['demo_samples'] is False


def test_private_cors_allowlist_does_not_authorize_other_origins(tmp_path):
    from procureflow.app import create_app
    from procureflow.db import Database
    from fastapi.testclient import TestClient
    settings=Settings(data_dir=tmp_path,web_origins=('http://127.0.0.1:32123',))
    db=Database(settings.database_url,create_schema=True)
    with TestClient(create_app(settings,database=db)) as client:
        allowed=client.options('/api/v1/requests',headers={'Origin':'http://127.0.0.1:32123',
            'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'authorization'})
        denied=client.options('/api/v1/requests',headers={'Origin':'https://other.invalid',
            'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'authorization'})
        assert allowed.status_code==200 and allowed.headers['access-control-allow-origin']=='http://127.0.0.1:32123'
        assert denied.status_code==400 and 'access-control-allow-origin' not in denied.headers


def test_cors_configuration_rejects_wildcard_and_paths(tmp_path):
    for origin in ('*','http://localhost:3000/','http://localhost:3000/page','https://user:pass@host.test'):
        with pytest.raises(ValueError,match='PF_WEB_ORIGINS'):
            Settings(data_dir=tmp_path,web_origins=(origin,))


def test_preflight_cli_defaults_offline_and_blocks_unconfigured_erp(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root=Path(__file__).resolve().parents[3]
    env={**os.environ,'ERP_API_KEY':'should-not-be-printed','ERP_API_SECRET':'also-never-printed',
         'PF_MODE':'demo','PF_ERP_MODE':'mock'}
    command=[sys.executable,str(root/'scripts/preflight.py')]
    offline=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
    result=json.loads(offline.stdout)
    assert offline.returncode==0 and result['status']=='inventory_only' and result['erp_probe_attempted'] is False
    assert 'should-not-be-printed' not in offline.stdout and 'also-never-printed' not in offline.stdout
    blocked=subprocess.run(command+['--erp-read-only'],env=env,capture_output=True,text=True,timeout=10)
    result=json.loads(blocked.stdout)
    assert blocked.returncode==2 and result['erp_probe_attempted'] is False and result['external_writes_attempted']==0
