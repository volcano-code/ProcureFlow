"""Full application integration; the shared system fixture also runs on real PG."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from procureflow.db import KnowledgeVersionRow,OperationRow,ApprovalRow
from procureflow.knowledge import KnowledgeService,NewDocumentVersion
from procureflow.contracts import Principal
from conftest import BUYER,APPROVER,OTHER,AUDITOR,request

TEXT='运费制度\n未知运费不能填零，附加费必须确认。\n'

def create(client,key='freight',expected=0,text=TEXT):
    result=client.post('/api/v1/knowledge/versions',headers=APPROVER,json={
        'document_key':key,'expected_version':expected,'title':'采购运费','text':text})
    assert result.status_code==201,result.text
    return result.json()

def publish(client,row):
    result=client.post(f'/api/v1/knowledge/versions/{row["id"]}/publish',headers=APPROVER,
        json={'expected_document_version':row['latest_version']})
    assert result.status_code==200,result.text


def test_full_app_versioned_search_and_stale_read(system):
    client,service,_=system;row=create(client);publish(client,row)
    response=client.get('/api/v1/knowledge/search?q=运费',headers=BUYER)
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    body=response.json();assert body['advisory_only'] and body['hits'][0]['text']==TEXT
    path=f'/api/v1/knowledge/citations/{body["hits"][0]["citation_id"]}?corpus_hash={body["corpus_hash"]}'
    assert client.get(path,headers=BUYER).status_code==200
    publish(client,create(client,expected=1,text='新版运费要求按合同约定确认。'))
    assert client.get(path,headers=BUYER).status_code==409
    with service.db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(OperationRow))==0
        assert s.scalar(select(func.count()).select_from(ApprovalRow))==0

@pytest.mark.parametrize('headers',[{},OTHER,BUYER,AUDITOR])
def test_full_app_no_unauthorized_publication(system,headers):
    client,_,_=system
    response=client.post('/api/v1/knowledge/versions',headers=headers,json={
        'document_key':'denied','expected_version':0,'title':'title','text':TEXT})
    assert response.status_code in (401,403)


def test_full_app_read_scope_and_no_ambient_authority(system):
    client,_,_=system;row=create(client);publish(client,row)
    assert client.get('/api/v1/knowledge/search?q=运费',headers=OTHER).json()['hits']==[]
    result=client.post('/api/v1/knowledge/versions',headers=BUYER,json={
        'document_key':'denied','expected_version':0,'title':'title','text':TEXT,'role':'approver','tenant_id':'demo'})
    assert result.status_code==422


def test_full_app_advice_is_invalidated_by_document_publication(system):
    client,_,_=system;r=request(client)
    def reserve():
        response=client.post(f'/api/v1/requests/{r["id"]}/advice-runs',headers=BUYER,
            json={'expected_version':r['version'],'idempotency_key':str(uuid4())})
        assert response.status_code==201,response.text
        return response.json()
    first=reserve();assert 'knowledge' not in first['input_snapshot']
    row=create(client);publish(client,row)
    assert not client.get(f'/api/v1/advice-runs/{first["id"]}',headers=BUYER).json()['current']
    second=reserve();assert second['input_snapshot']['knowledge']['documents']
    assert client.get(f'/api/v1/advice-runs/{second["id"]}',headers=BUYER).json()['current']
    policy=client.get('/api/v1/policy',headers=BUYER).json()
    client.post(f'/api/v1/knowledge/versions/{row["id"]}/retire',headers=APPROVER,json={'expected_document_version':1})
    assert not client.get(f'/api/v1/advice-runs/{second["id"]}',headers=BUYER).json()['current']
    assert client.get('/api/v1/policy',headers=BUYER).json()==policy


def test_full_backend_concurrent_revision_compare_and_swap(system):
    _,procurement,_=system;k=KnowledgeService(procurement)
    principal=Principal(user_id='approver-01',tenant_id='demo',role='approver')
    command=NewDocumentVersion(document_key='race',expected_version=0,title='race',text=TEXT)
    def write(_):
        try:return k.create(principal,command)['version']
        except Exception as error:return getattr(error,'code',type(error).__name__)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,range(2)))
    assert sorted(map(str,results))==['1','VERSION_CONFLICT']
    with procurement.db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(KnowledgeVersionRow))==1
