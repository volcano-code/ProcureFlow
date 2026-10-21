"""Strict consistency check of joint records, NOT a new execution or attestation."""
import argparse
import hashlib
import json
from pathlib import Path
import re

STEPS=['native_pilot_pg_scope','queued_survives_restart','candidate_worker_then_unconfirmed_import',
       'unconfirmed_unapproved_self_approval_denied','normal_real_draft_readback_replay',
       'lost_receipt_restart_read_only_recovery','revoked_approver_zero_dispatch',
       'cancelled_candidate_zero_provider_calls','independent_pg_audit_after_restart']

def require(condition,code):
    if not condition:raise ValueError(code)

def unique(pairs):
    result={}
    for k,v in pairs:
        require(k not in result,'DUPLICATE_KEY');result[k]=v
    return result

def load(path):
    with Path(path).open('rb') as stream:raw=stream.read(1000001)
    require(len(raw)<=1000000,'OVERSIZED_RECORD')
    return json.loads(raw,object_pairs_hook=unique),hashlib.sha256(raw).hexdigest()

def check(directory):
    run,h1=load(Path(directory)/'roundtrip.json');audit,h2=load(Path(directory)/'database-audit.json')
    require(run.get('schema')=='candidate-procurement-v1' and run.get('status')==audit.get('status')=='passed','STAGES_INCOMPLETE')
    require(run.get('steps')==STEPS,'STEPS_INCOMPLETE')
    require(all(run.get(k) is True for k in ('synthetic_only','scripted_provider','live_erp','browser_responses_not_mocked',
        'api_without_model_key','candidate_worker_without_erp_key')) and run.get('real_model_used') is False
        and run.get('human_approval_measured') is False,'SCOPE_MISMATCH')
    require(run.get('database')=='postgresql' and re.fullmatch('[0-9a-f]{40}',run.get('source_tree','')),'SOURCE_OR_BACKEND')
    require(type(run.get('provider_requests')) is int and run['provider_requests']==6
            and type(run.get('erp_post_attempts')) is int and run['erp_post_attempts']==2,'CALL_COUNTS')
    operations=run.get('operations',[])
    require(len(operations)==3 and [x.get('scenario') for x in operations]==['normal','lost-receipt','revoked'],'SCENARIOS')
    require(len({x.get('operation_id') for x in operations})==3 and len({x.get('candidate_run_id') for x in operations})==3,'DUPLICATE_IDENTITIES')
    for index,op in enumerate(operations):
        require(op.get('expected_total')=='2000.00' and re.fullmatch('[0-9a-f]{64}',op.get('snapshot_hash','')),'OPERATION_BINDING')
        require(op.get('final_status')==('NEEDS_HUMAN' if index==2 else 'COMPLETED'),'OPERATION_STATE')
        if index==2:require(op.get('remote_id') is None,'REVOKED_HAS_DRAFT')
        else:require(isinstance(op.get('remote_id'),str) and op['remote_id'].startswith('PUR-SQTN-'),'REAL_DRAFT_ID')
    require(len({x['remote_id'] for x in operations[:2]})==2,'DUPLICATE_DRAFT')
    pg=run.get('business_audit',{})
    require(pg.get('status')=='passed' and pg.get('database')=='postgresql','PG_AUDIT')
    for key,value in dict(candidate_runs=4,imported=3,cancelled=1,quotes=3,confirmed_quotes=3,approvals=3,
                          operations=3,completed=2,denied=1,done_outbox=3,verified_receipts=2).items():
        require(type(pg.get(key)) is int and pg[key]==value,'PG_COUNTS')
    require(all(pg.get(k) is True for k in ('candidate_provenance_checked','after_api_restart','read_only_audit')),'PG_CHECKS')
    require(audit.get('stage')=='database-audit' and audit.get('source_tree')==run['source_tree'],'AUDIT_BINDING')
    require(audit.get('operation_ids')==[x['operation_id'] for x in operations[:2]],'AUDIT_IDS')
    for key,value in dict(draft_count=2,purchase_order_count=0,submitted_count=0).items():
        require(type(audit.get(key)) is int and audit[key]==value,'ERP_COUNTS')
    require(all(audit.get(k) is True for k in ('contents_checked','unique_index_checked','permissions_checked','revoked_operation_absent')),'ERP_CHECKS')
    return {'status':'passed','real_model_used':False,'scripted_provider':True,'real_erp_used':True,
        'source_tree':run['source_tree'],'record_sha256':{'roundtrip.json':h1,'database-audit.json':h2},
        'scope':'native UI/PG/scripted model/independent approval/real ERP, not model quality'}

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);a=p.parse_args(argv)
    try:result=check(a.directory)
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        print('{"status":"failed","reason":"INCOMPLETE_JOINT_EVIDENCE"}');return 1
    print(json.dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
