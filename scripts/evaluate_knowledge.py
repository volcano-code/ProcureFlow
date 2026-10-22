"""Run the frozen synthetic retrieval development set; no LLM or external calls."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/api'))
from procureflow.config import Settings
from procureflow.contracts import Principal
from procureflow.db import Database
from procureflow.erp import MockERP
from procureflow.knowledge import KnowledgeService,NewDocumentVersion,PublishDocument,DocumentTransition
from procureflow.service import ProcurementService


def evaluate():
    docs_path=ROOT/'evals/knowledge/documents.json';queries_path=ROOT/'evals/knowledge/queries.json'
    documents=json.loads(docs_path.read_text());queries=json.loads(queries_path.read_text())
    with tempfile.TemporaryDirectory(prefix='pf-document-eval-') as directory:
        path=Path(directory);settings=Settings(data_dir=path,database_url=f'sqlite:///{path}/test.sqlite3',mode='demo')
        db=Database(settings.database_url,create_schema=True)
        try:
            service=KnowledgeService(ProcurementService(db,settings,MockERP(path/'erp.sqlite3')))
            author=Principal(user_id='approver-01',tenant_id='demo',role='approver')
            reader=Principal(user_id='buyer-01',tenant_id='demo',role='buyer')
            versions={}
            for doc in documents:
                key=doc['document_key']
                row=service.create(author,NewDocumentVersion(document_key=key,title=doc['title'],text=doc['text'],expected_version=versions.get(key,0)))
                versions[key]=row['version']
                service.transition(author,row['id'],PublishDocument(expected_document_version=row['version']),publish=True)
                if doc['retire']:service.transition(author,row['id'],DocumentTransition(expected_document_version=row['version']))
            records=[]
            for query in queries:
                result=service.search(reader,query['query'],top_k=5)
                found=[hit['document_key'] for hit in result['hits']]
                # The retrieval function never receives expected_key.
                passed=query['expected_key'] in found if query['expected_key'] else not found
                records.append({**query,'returned_keys':found,'passed':passed,
                    'citation_ids':[hit['citation_id'] for hit in result['hits']]})
            answerable=[r for r in records if r['expected_key']]
            absent=[r for r in records if r['expected_key'] is None]
            return {'scope':'public synthetic developer-authored retrieval Dev set; not a held-out semantic or model benchmark',
                'method':'bm25-char-tfidf-rrf-v1','live_model_calls':0,'document_revisions':len(documents),
                'query_count':len(records),'recall_at_5':sum(r['passed'] for r in answerable)/len(answerable),
                'answerable_passed':sum(r['passed'] for r in answerable),'answerable_total':len(answerable),
                'no_match_correct':sum(r['passed'] for r in absent),'no_match_total':len(absent),
                'data_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [docs_path,queries_path]},
                'results':records}
        finally:db.engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Output exists; preserve earlier attempts by choosing a new path')
    result=evaluate();args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as output:json.dump(result,output,ensure_ascii=False,indent=2);output.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k!='results'},ensure_ascii=False))
    # Execution success is not an assertion that all retrieval tasks passed.
    return 0
if __name__=='__main__':raise SystemExit(main())
