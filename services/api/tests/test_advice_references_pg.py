"""The bound advice/reference reads against an actual migrated PostgreSQL schema."""
import pytest
from sqlalchemy import text
from procureflow.advice import AdviceService
from procureflow.contracts import Principal, RequestCreate, AdviceRunCommand
from procureflow.policy_documents import PolicyDocumentService, PublishDocument
from procureflow.errors import DomainError
pytestmark=pytest.mark.postgres


def test_postgres_advice_reference_binding_and_withdrawal(system):
    client,procurement,_=system
    assert not procurement.db.sqlite
    buyer=Principal(user_id='buyer-01',tenant_id='demo',role='buyer')
    approver=Principal(user_id='approver-01',tenant_id='demo',role='approver')
    library=PolicyDocumentService(procurement);advice=AdviceService(procurement,library)
    body=dict(expected_version=0,title='PG运费条款',source_label='合成',content='运费未知必须澄清。',reason='真实PostgreSQL引用检查')
    published=library.publish(approver,'reference-pg',PublishDocument(**body))
    request=procurement.create_request(buyer,RequestCreate(title='PG reference advice',sku='SKU-1',quantity='2',budget='1000',max_delivery_days=10))
    run=advice.reserve(buyer,request['id'],AdviceRunCommand(expected_version=request['version'],idempotency_key='pg-reference-advice-01'))
    path=f'/api/v1/policy-library/advice-runs/{run["id"]}/search'
    response=client.get(path,headers={'Authorization':'Bearer demo-buyer'},params={'query':'运费'})
    assert response.status_code==200,response.text
    hit=response.json()['result']['results'][0]
    assert hit['revision_id']==published['id']
    library.publish(approver,'reference-pg',PublishDocument(**{**body,'expected_version':1,'withdrawn':True,'content':''}))
    assert advice.get(buyer,run['id'])['current'] is False
    assert client.get(path,headers={'Authorization':'Bearer demo-buyer'},params={'query':'运费'}).status_code==409
    with procurement.db.transaction() as session:
        for table in ('quotes','approvals','external_operations'):
            assert session.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()==0
