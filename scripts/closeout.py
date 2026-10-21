"""No-key corpus audit, exact-tree engineering receipts and tracked-only packaging.

Never marks live model quality, external human review or an unseen Holdout done.
These checks establish consistency of supplied records, not third-party attestation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from evals.candidate_quality import write_json,require
from evals.candidate_grouping import audit
from check_candidate_procurement import check,load


def joint_receipts(tree,directories):
    require(len(directories)==3 and len({Path(p).resolve() for p in directories})==3,'THREE_DISTINCT_RECORDS_REQUIRED')
    receipts=[];ids=set()
    for directory in directories:
        result=check(directory);run,_=load(Path(directory)/'roundtrip.json')
        require(result['source_tree']==tree,'STALE_JOINT_EVIDENCE')
        identity=(Path(directory)/'source-identity.txt').read_text().splitlines()
        require(len(identity)==2 and re.fullmatch('[0-9a-f]{40}',identity[0]) and identity[1]==tree,'SOURCE_IDENTITY_MISMATCH')
        current={op['operation_id'] for op in run['operations']}
        require(not ids & current,'REUSED_JOINT_EXECUTION');ids.update(current)
        xml=ET.parse(Path(directory)/'tests.xml').getroot()
        tests=xml.findall('.//testcase')
        require(len(tests)>=34 and not xml.findall('.//failure') and not xml.findall('.//error')
                and not xml.findall('.//skipped'),'JOINT_TESTS_INCOMPLETE')
        receipts.append({'source_commit':identity[0],**result})
    return receipts


def readiness(tree,directories):
    grouped=audit(ROOT/'evals/candidate60')
    try:
        records=joint_receipts(tree,directories);status='selected_keyless_gates_passed';blocks=[]
    except (OSError,ValueError,KeyError,TypeError,ET.ParseError):
        records=[];status='blocked';blocks=['CURRENT_TREE_THREE_JOINT_RECORDS_REQUIRED']
    return {'schema_version':1,'status':status,'source_tree':tree,'grouped_data':grouped,'joint_records':records,
        'blocking_engineering_evidence':blocks,'product_release_approved':False,
        'external_requirements':[
            {'id':'live_B_C_trials','status':'blocked_provider_configuration_and_execution'},
            {'id':'live_model_full_procurement','status':'not_executed'},
            {'id':'semantic_review','status':'pending_real_human_annotations'},
            {'id':'independent_holdout','status':'public_corpus_is_not_unseen'},
            {'id':'external_user_trials','status':'pending_real_participants'},
            {'id':'maintainer_release_review','status':'pending'}],
        'scope':'selected no-key engineering gate consistency; not complete product acceptance or security certification'}


def git(*args,root=ROOT):
    return subprocess.check_output(['git',*args],cwd=root)


def package(destination,root=ROOT):
    root=Path(root).resolve();destination=Path(destination)
    require(not destination.resolve().is_relative_to(root),'PACKAGE_OUTSIDE_REPOSITORY_REQUIRED')
    require(not git('status','--porcelain',root=root).strip(),'CLEAN_SOURCE_REQUIRED')
    entries=git('ls-tree','-rz','--full-tree','HEAD',root=root).split(b'\0')
    names=[]
    for entry in entries:
        if not entry:continue
        metadata,name=entry.split(b'\t',1);mode,kind,blob=metadata.decode().split();path=Path(name.decode())
        require(kind=='blob' and mode in {'100644','100755'},'REGULAR_TRACKED_FILES_ONLY')
        require(not path.is_absolute() and '..' not in path.parts,'UNSAFE_SOURCE_PATH')
        require(not any(p in {'.data','.venv','node_modules','__pycache__'} for p in path.parts),'RUNTIME_FILE_TRACKED')
        require(not any(p=='.env' or (p.startswith('.env.') and p!='.env.example') for p in path.parts),'PRIVATE_ENV_TRACKED')
        require(path.suffix not in {'.sqlite3','.db','.key'},'PRIVATE_RUNTIME_FILE_TRACKED')
        names.append(path.as_posix())
    destination.mkdir(parents=False,exist_ok=False)
    archive=destination/'procureflow-source.zip'
    git('archive','--format=zip','--prefix=procureflow/','HEAD','-o',str(archive.resolve()),root=root)
    with zipfile.ZipFile(archive) as z:
        hashes={n.removeprefix('procureflow/'):hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if not n.endswith('/')}
    require(set(hashes)==set(names),'ARCHIVE_SOURCE_MISMATCH')
    receipt={'schema_version':1,'source_commit':git('rev-parse','HEAD',root=root).decode().strip(),
        'source_tree':git('rev-parse','HEAD^{tree}',root=root).decode().strip(),'tracked_files':len(names),
        'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':hashes,
        'formal_release':False,'scope':'tracked source package only, no model/quality/security certification'}
    write_json(destination/'manifest.json',receipt)
    return receipt


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['data-audit','readiness','package'])
    p.add_argument('--joint',type=Path,action='append',default=[]);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    try:
        if a.command=='package':
            r=package(a.output);code=0
        else:
            r=audit(ROOT/'evals/candidate60') if a.command=='data-audit' else readiness(git('rev-parse','HEAD^{tree}').decode().strip(),a.joint)
            write_json(a.output,r);code=2 if r['status']=='blocked' else 0
        print(json.dumps({'status':r.get('status','packaged'),'exit_code':code}));return code
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        print('{"status":"failed","reason":"CLOSEOUT_CHECK_FAILED"}');return 1

if __name__=='__main__':raise SystemExit(main())
