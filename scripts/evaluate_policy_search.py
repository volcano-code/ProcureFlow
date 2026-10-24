"""Real retriever/DB/local encoder evaluation; no gold sent to encoder."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/api'))
from procureflow.config import Settings
from procureflow.db import Database
from procureflow.erp import MockERP
from procureflow.service import ProcurementService
from procureflow.contracts import Principal
from procureflow.policy_documents import PolicyDocumentService,PublishDocument,SearchDocuments,sha
from procureflow.policy_embeddings import LocalSentenceEncoder


def evaluate(output,model_dir=None):
    corpus_path=ROOT/'evals/policy_search/dev20.json'
    corpus=json.loads(corpus_path.read_text())
    with tempfile.TemporaryDirectory(prefix='pf-reference-eval-') as tmp:
        settings=Settings(data_dir=Path(tmp),auth_tokens={
            'eval-only':{'user_id':'synthetic-evaluator','tenant_id':'reference-eval','role':'approver'}})
        db=Database(settings.database_url,create_schema=True)
        library=PolicyDocumentService(ProcurementService(db,settings,MockERP(Path(tmp)/'mock.sqlite')),
            LocalSentenceEncoder(model_dir) if model_dir else None)
        principal=Principal(user_id='synthetic-evaluator',tenant_id='reference-eval',role='approver')
        keys={}
        for doc in corpus['documents']:
            result=library.publish(principal,doc['key'],PublishDocument(expected_version=0,title=doc['title'],
                source_label='合成开发集，不是实际企业制度',content=doc['content'],reason='检索开发评测样本'))
            keys[result['document_id']]=doc['key']
        report={'scope':corpus['scope'],'corpus_sha256':sha(corpus_path.read_text()),
            'learned_encoder_used':bool(model_dir),'paid_api_calls':0,'llm_generation_used':False,'methods':{}}
        for method in (['lexical','hybrid'] if model_dir else ['lexical']):
            trials=[]
            for q in corpus['queries']:
                found=library.search(principal,SearchDocuments(query=q['query'],method=method,limit=5))
                retrieved=[keys[h['document_id']] for h in found['results']]
                trials.append({'id':q['id'],'query':q['query'],'expected':q['relevant_key'],
                    'retrieved':retrieved,'hit':q['relevant_key'] in retrieved})
                for hit in found['results']:
                    text=library.revision(principal,hit['revision_id'])['content'];f=hit['fragment']
                    assert text[f['start']:f['end']]==f['text'] and sha(text)==hit['source_hash']
            report['methods'][method]={'hits':sum(t['hit'] for t in trials),'queries':len(trials),
                'recall_at_5':sum(t['hit'] for t in trials)/len(trials),'trials':trials}
        report['encoder']=library.encoder.identity if library.encoder else None
        output.parent.mkdir(parents=True,exist_ok=True)
        if output.exists():raise ValueError('Refusing to overwrite earlier results')
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        db.engine.dispose()
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model-dir',type=Path)
    args=parser.parse_args()
    # Provisioning is separate. Runtime must work without Hub access or tokens.
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    result=evaluate(args.output,args.model_dir)
    print(json.dumps({m:{k:v for k,v in data.items() if k!='trials'} for m,data in result['methods'].items()}))
    raise SystemExit(0 if all(x['recall_at_5']>=0.9 for x in result['methods'].values()) else 1)
