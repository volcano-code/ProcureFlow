"""Actual graph/service + scripted transport. NO live LLM effect claims."""
import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'services/api'),str(ROOT)]
import httpx
import pytest
from procureflow.agent import ReadOnlyAgent
from procureflow.advice import AdviceService
from procureflow.config import Settings
from procureflow.contracts import Principal,RequestCreate,AdviceRunCommand
from procureflow.db import Database
from procureflow.erp import MockERP
from procureflow.service import ProcurementService
from procureflow.policy_documents import PolicyDocumentService,PublishDocument


def test_graph_reads_and_cites_references_plus_quote_without_writes(tmp_path):
    buyer=Principal(user_id='buyer-01',tenant_id='demo',role='buyer')
    approver=Principal(user_id='approver-01',tenant_id='demo',role='approver')
    settings=Settings(data_dir=tmp_path);db=Database(settings.database_url,create_schema=True)
    service=ProcurementService(db,settings,MockERP(tmp_path/'erp.sqlite'))
    from procureflow.policy_embeddings import configured_encoder
    library=PolicyDocumentService(service,configured_encoder());advice=AdviceService(service,library)
    try:
        published=library.publish(approver,'freight',PublishDocument(expected_version=0,title='运费条款',
            source_label='合成原文',content='运费未知时必须澄清，不能自行填零。',reason='验证图执行引用'))
        request=service.create_request(buyer,RequestCreate(title='Graph grounded reference',sku='STAND-01',quantity='20',budget='30000',max_delivery_days=14))
        quote=service.import_quote(buyer,request['id'],'supplier-a.txt',(ROOT/'evals/fixtures/supplier-a.txt').read_bytes())
        current=service.get_request(buyer,request['id'])
        run=advice.reserve(buyer,request['id'],AdviceRunCommand(expected_version=current['version'],idempotency_key='graph-reference-test'))
        quote_id=run['input_snapshot']['documents'][quote['document_id']]['fragments'][0]['id']
        ref_id=published['id']+':0';requests=[]
        def handler(req):
            payload=json.loads(req.content);requests.append(payload)
            if len(requests)==1:
                assert {'search_references','get_reference_fragment'} <= {t['function']['name'] for t in payload['tools']}
                tool=lambda i,n,a:{'id':i,'type':'function','function':{'name':n,'arguments':json.dumps(a)}}
                message={'role':'assistant','tool_calls':[tool('compare','get_comparison',{}),
                    tool('quote','get_evidence',{'document_id':quote['document_id']}),
                    tool('search','search_references',{'query':'运费'})]}
                finish='tool_calls'
            else:
                observed=[json.loads(m['content']) for m in payload['messages'] if m['role']=='tool']
                assert any(ref_id in json.dumps(x) for x in observed)
                message={'role':'assistant','content':json.dumps({'summary':'参考运费条款，未知费用需要澄清；建议不是审批。','evidence_ids':[quote_id,ref_id]})}
                finish='stop'
            return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':finish}],
                'usage':{'prompt_tokens':50,'completion_tokens':20,'total_tokens':70}})
        result=advice.process(buyer,run['id'],lambda:ReadOnlyAgent('http://127.0.0.1:19999/v1','synthetic','scripted',
            transport=httpx.MockTransport(handler),max_connection_retries=0))
        assert result['status']=='COMPLETED',result
        assert len(requests)==2 and result['output']['evidence_read_verified']
        assert result['output']['reference_citations'][0]['citation_id']==ref_id
        assert result['output']['semantic_factuality_verified'] is False
        assert result['output']['reference_retrievals'][0]['method']==('hybrid' if library.encoder else 'lexical')
        assert service.get_request(buyer,request['id'])['version']==current['version']
    finally:db.engine.dispose()


@pytest.mark.parametrize('read_reference',[False,True])
def test_graph_reference_ids_must_be_allowed_and_actually_read(read_reference):
    # Explicit synthetic protocol fixture, no source quality score.
    ref='pdr_'+'a'*32+':0';seen=[]
    def handler(req):
        seen.append(req)
        if len(seen)==1:
            calls=[{'id':'c','type':'function','function':{'name':'get_comparison','arguments':'{}'}}]
            if read_reference:calls.append({'id':'r','type':'function','function':{'name':'search_references','arguments':'{"query":"运费"}'}})
            message={'tool_calls':calls}
        else:message={'content':json.dumps({'summary':'参考条款','evidence_ids':[ref]})}
        return httpx.Response(200,json={'choices':[{'message':message}]})
    def invoke(name,args):return {'results':[{'citation_id':ref}]} if name=='search_references' else {}
    agent=ReadOnlyAgent('http://127.0.0.1:19999/v1','synthetic','scripted',transport=httpx.MockTransport(handler),require_evidence_reads=True)
    try:
        if read_reference:
            assert agent.run(invoke,{ref})['evidence_read_verified'] is True
        else:
            from procureflow.errors import DomainError
            with pytest.raises(DomainError) as e:agent.run(invoke,{ref})
            assert e.value.code=='EVIDENCE_NOT_READ'
    finally:agent.client.close()
