"""Run the exact reference service on a migrated disposable PostgreSQL schema."""
import pytest
from sqlalchemy import text
from procureflow.policy_documents import PolicyDocumentService, PublishDocument, SearchDocuments
from procureflow.contracts import Principal
from procureflow.errors import DomainError

pytestmark=pytest.mark.postgres


def test_reference_postgres_version_search_and_tenant_isolation(system):
    _,procurement,_=system
    assert not procurement.db.sqlite
    service=PolicyDocumentService(procurement)
    principal=Principal(user_id='approver-01',tenant_id='demo',role='approver')
    first=service.publish(principal,'test-reference',PublishDocument(expected_version=0,
        title='PostgreSQL 运费',source_label='synthetic',content='运费必须逐项核对。',reason='真实数据库检索测试'))
    result=service.search(principal,SearchDocuments(query='运费'))
    assert result['results'][0]['revision_id']==first['id']
    other=Principal(user_id='other-01',tenant_id='other',role='buyer')
    assert service.search(other,SearchDocuments(query='运费'))['results']==[]
    second=service.publish(principal,'test-reference',PublishDocument(expected_version=1,
        title='撤回',source_label='synthetic',content='',withdrawn=True,reason='测试历史保留与撤回'))
    assert service.search(principal,SearchDocuments(query='运费'))['results']==[]
    assert service.revision(principal,first['id'])['status']=='superseded'
    assert second['version']==2
    with procurement.db.transaction() as session:
        assert session.execute(text('SELECT COUNT(*) FROM external_operations')).scalar_one()==0
        assert session.execute(text('SELECT COUNT(*) FROM approvals')).scalar_one()==0
