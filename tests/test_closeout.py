import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from closeout import package,joint_receipts,readiness
from joint_evidence import required_cases
spec=importlib.util.spec_from_file_location('joint_record_examples',ROOT/'tests/test_candidate_procurement.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


def dirs(tmp_path):
    paths=[]
    for i in range(3):
        p=tmp_path/str(i);p.mkdir();run,audit=fixtures.records()
        for op in run['operations']:
            op['operation_id']+=f'-{i}';op['candidate_run_id']+=f'-{i}'
        audit['operation_ids']=[x['operation_id'] for x in run['operations'][:2]]
        fixtures.write(p,run,audit);(p/'source-identity.txt').write_text('b'*40+'\n'+'a'*40+'\n')
        _,names=required_cases()
        (p/'tests.xml').write_text(f'<testsuite tests="{len(names)}" failures="0" errors="0" skipped="0">'+''.join(
            f'<testcase classname="tests.test_candidate_procurement" name="{name}"/>' for name in sorted(names))+'</testsuite>')
        (p/'execution.json').write_text(json.dumps({'schema_version':1,'source_commit':'b'*40,
            'source_tree':'a'*40,'repository':'volcano-code/ProcureFlow','run_id':'123','run_attempt':'1',
            'replica':i+1,'project':f'pf-joint-123-1-{i+1}'}))
        paths.append(p)
    return paths


def test_three_records_do_not_approve_product(tmp_path):
    paths=dirs(tmp_path);r=readiness('a'*40,paths)
    assert r['status']=='selected_keyless_gates_passed' and not r['product_release_approved']
    assert len(r['external_requirements'])==6


@pytest.mark.parametrize('mutation',['missing','stale','repeat_path','repeat_ops','bad_identity','skip','no_tests'])
def test_incomplete_or_reused_evidence_cannot_close_gate(tmp_path,mutation):
    paths=dirs(tmp_path);tree='a'*40
    if mutation=='missing':paths.pop()
    elif mutation=='stale':tree='c'*40
    elif mutation=='repeat_path':paths[2]=paths[1]
    elif mutation=='repeat_ops':
        for name in ('roundtrip.json','database-audit.json'):(paths[2]/name).write_bytes((paths[1]/name).read_bytes())
    elif mutation=='bad_identity':(paths[2]/'source-identity.txt').write_text('b'*40+'\n'+'c'*40)
    elif mutation=='skip':(paths[2]/'tests.xml').write_text('<testsuite><testcase><skipped/></testcase></testsuite>')
    else:(paths[2]/'tests.xml').unlink()
    assert readiness(tree,paths)['status']=='blocked'


def small_repo(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    for args in [('init','-q'),('config','user.name','Test'),('config','user.email','test@example.invalid')]:
        subprocess.run(['git',*args],cwd=root,check=True)
    (root/'README.md').write_text('synthetic package test\n');(root/'.gitignore').write_text('.data/\n')
    subprocess.run(['git','add','.'],cwd=root,check=True);subprocess.run(['git','commit','-qm','test'],cwd=root,check=True)
    return root


def test_package_only_tracked_regular_clean_files(tmp_path):
    root=small_repo(tmp_path);(root/'.data').mkdir();(root/'.data/secret').write_text('never archive me')
    result=package(tmp_path/'release',root)
    assert set(result['files'])=={'.gitignore','README.md'} and not result['formal_release']
    assert (tmp_path/'release/procureflow-source.zip').is_file()
    with pytest.raises(FileExistsError):package(tmp_path/'release',root)


@pytest.mark.parametrize('mutation',['dirty','private_env','symlink','inside','database'])
def test_package_rejects_unsafe_inputs(tmp_path,mutation):
    root=small_repo(tmp_path);out=tmp_path/'release'
    if mutation=='inside':out=root/'release'
    elif mutation=='dirty':(root/'README.md').write_text('changed')
    else:
        name={'private_env':'.env','symlink':'link','database':'state.sqlite3'}[mutation]
        if mutation=='symlink':(root/name).symlink_to('README.md')
        else:(root/name).write_text('synthetic forbidden data')
        subprocess.run(['git','add',name],cwd=root,check=True);subprocess.run(['git','commit','-qm','bad fixture'],cwd=root,check=True)
    with pytest.raises(ValueError):package(out,root)
