"""Actual PostgreSQL source/authority read-only AG-UI acceptance."""
import pytest
from sqlalchemy import text
from conftest import BUYER, request
pytestmark=pytest.mark.postgres


def test_agui_observer_real_postgres_no_business_side_effects(system):
    from ag_ui.core import Event
    from pydantic import TypeAdapter
    import json
    client,service,_=system
    assert not service.db.sqlite
    req=request(client)
    receipt=client.post(f'/api/v1/requests/{req["id"]}/advice-runs',headers=BUYER,json={
      'expected_version':req['version'],'idempotency_key':'pg-agui-001'})
    assert receipt.status_code==201,receipt.text
    run=receipt.json()
    body={'threadId':req['id'],'runId':run['id'],'messages':[],
          'state':{},'tools':[],'context':[],'protocolVersion':'1.0'}
    p=f'/api/v1/ag-ui/advice-runs/{run["id"]}/observe'
    r=client.post(p,headers=BUYER,json=body)
    assert r.status_code==200,r.text
    events=[json.loads(line[5:].lstrip()) for line in r.text.splitlines() if line.startswith('data:')]
    for ev in events: TypeAdapter(Event).validate_python(ev)
    assert events[1]['snapshot']['agentStatus']=='PENDING'
    assert events[1]['snapshot']['externalWritePermitted'] is False
    with service.db.transaction() as session:
        assert all(session.execute(text(f'SELECT count(*) FROM {t}')).scalar_one()==0 for t in
                   ('quotes','approvals','external_operations'))
