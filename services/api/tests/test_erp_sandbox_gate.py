"""The lab runner must never use missing authorization or arbitrary account files."""
import importlib.util
import json
from pathlib import Path
import stat
import pytest

ROOT=Path(__file__).resolve().parents[3]


def script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_sandbox_initializer_opt_in_and_exclusive_private_file(tmp_path):
    module=script('init_erp_sandbox');out=tmp_path/'.env.erp-sandbox'
    assert module.main(['--output',str(out)])==2 and not out.exists()
    assert module.main(['--create-ephemeral','--output',str(out)])==0
    original=out.read_bytes()
    assert stat.S_IMODE(out.stat().st_mode)==0o600
    assert module.main(['--create-ephemeral','--output',str(out)])==2
    assert out.read_bytes()==original


@pytest.mark.parametrize('args', [[], ['--ephemeral-test']])
def test_sandbox_runner_without_valid_marker_never_networks(tmp_path,monkeypatch,args):
    module=script('verify_erp_sandbox')
    monkeypatch.setattr(module,'exercise',lambda *_:pytest.fail('unauthorized network attempt'))
    out=tmp_path/'report.json'
    assert module.main(args+['--credentials',str(tmp_path/'missing.json'), '--env-file',str(tmp_path/'missing.env'), '--output',str(out)])==2
    report=json.loads(out.read_text())
    assert report['status']=='blocked' and report['network_attempted'] is False
    assert not (tmp_path/'input-fixtures').exists()


@pytest.mark.parametrize('field,value',[('nonce','b'*64),('site','production'),('company','Actual Company'),('user','Administrator'),('sku','ACTUAL'),('api_key','')])
def test_sandbox_refuses_unmatched_or_nonfixture_identity(tmp_path,field,value):
    module=script('verify_erp_sandbox')
    data=dict(site='pf-erp-test.local',company='ProcureFlow Sandbox',user='pf-integration@example.invalid',
              sku='PF-SANDBOX-ITEM',nonce='a'*64,api_key='synthetic-key',api_secret='synthetic-secret')
    env=tmp_path/'fixture.env';env.write_text('PF_EPHEMERAL_NONCE='+'a'*64+'\n')
    source=tmp_path/'fixture.json';source.write_text(json.dumps(data))
    assert module.validate_lab(source,env)==data
    data[field]=value;source.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='LAB_CONFIGURATION_MISMATCH'):module.validate_lab(source,env)
