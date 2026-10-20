"""Offline/unit and loopback protocol tests; not actual Docker/browser acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from check_candidate_stack import check, unique_object, STEPS, STATUSES
from fixtures.candidate_provider import FixtureState, KEY, MODEL, MAX_BYTES, handler_for, response_for


def valid_report():
    return {"schema":"candidate-stack-fixture-v1","status":"passed","steps":STEPS.copy(),
        "source_commit":"a"*40,"database":"postgresql","synthetic_only":True,
        "api_without_provider_key":True,"browser_responses_not_mocked":True,
        "real_sigkill":True,"lease_expiry_injected":True,"no_automatic_replay":True,
        "real_model_used":False,"real_erp_used":False,"model_quality_measured":False,
        "provider_requests":4,
        "runs":[{"scenario":s,"id":"can_"+str(i)*32,"status":v}
                for i,(s,v) in enumerate(STATUSES.items())],
        "database_counts":{"candidate_runs":4,"quotes":1,"confirmed_quotes":0,
            "approvals":0,"external_operations":0,"candidate_outputs":1}}


def test_checker_reference_is_only_a_shape_self_test():
    assert check(valid_report())["status"]=="passed"


@pytest.mark.parametrize("field,value",[
    ("status","failed"),("database","sqlite"),("provider_requests",True),
    ("provider_requests",3),("provider_requests",5),("source_commit","main"),
    ("synthetic_only",False),("api_without_provider_key",False),
    ("browser_responses_not_mocked",False),("real_sigkill",False),
    ("lease_expiry_injected",False),("no_automatic_replay",False),
    ("real_model_used",True),("real_erp_used",True),("model_quality_measured",True),
    ("steps",STEPS[:-1]),("steps",STEPS+[STEPS[-1]]),
    ("runs",[]),("schema","legacy"),
])
def test_checker_rejects_incomplete_or_mislabeled_evidence(field,value):
    report=valid_report();report[field]=value
    with pytest.raises(ValueError): check(report)


@pytest.mark.parametrize("field",["candidate_runs","quotes","confirmed_quotes","approvals","external_operations","candidate_outputs"])
def test_checker_rejects_false_side_effect_counts(field):
    report=valid_report();report["database_counts"][field]+=1
    with pytest.raises(ValueError): check(report)


def test_checker_rejects_boolean_zero_and_repeated_run():
    report=valid_report();report["database_counts"]["approvals"]=False
    with pytest.raises(ValueError):check(report)
    report=valid_report();report["runs"][1]["id"]=report["runs"][0]["id"]
    with pytest.raises(ValueError):check(report)
    with pytest.raises(ValueError):json.loads('{"status":"failed","status":"passed"}',object_pairs_hook=unique_object)


def test_checker_cli_missing_file_and_bad_json_fail_closed(tmp_path):
    target=tmp_path/"missing.json"
    command=[sys.executable,"-S",str(ROOT/"scripts/check_candidate_stack.py"),"--report",str(target)]
    assert subprocess.run(command,capture_output=True).returncode==1
    target.write_text('{"status":"passed","secret":"DO_NOT_ECHO"}')
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode==1 and "DO_NOT_ECHO" not in result.stdout+result.stderr


def test_runner_and_provider_disabled_without_explicit_flag(tmp_path,monkeypatch):
    monkeypatch.delenv("PF_CANDIDATE_STACK_FIXTURE",raising=False)
    result=subprocess.run([sys.executable,"-S",str(ROOT/"scripts/fixtures/candidate_provider.py")],
        capture_output=True,text=True,timeout=5)
    assert result.returncode==2
    output=tmp_path/"blocked.json"
    command=[sys.executable,"-S",str(ROOT/"scripts/verify_candidate_stack.py"),"--output",str(output)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=5)
    assert result.returncode==2 and json.loads(output.read_text())["network_attempted"] is False
    # Earlier results must never be silently replaced.
    original=output.read_bytes()
    assert subprocess.run(command,capture_output=True,timeout=5).returncode!=0
    assert output.read_bytes()==original


def test_fixture_requires_exact_model_and_read_evidence():
    with pytest.raises(ValueError):response_for({"model":"arbitrary","messages":[{"role":"user"}]},1)
    with pytest.raises(ValueError):response_for({"model":MODEL,"messages":[{"role":"tool","content":"{}"}]},1)
    first=response_for({"model":MODEL,"messages":[{"role":"user","content":"synthetic"}]},1)
    assert {x["function"]["name"] for x in first["choices"][0]["message"]["tool_calls"]}=={"get_request","get_evidence"}
    from fixtures.candidate_provider import VALUES
    fragments=[{"id":"frag-"+key,"text":f"{key}: {value}"} for key,value in VALUES.items()]
    reply=response_for({"model":MODEL,"messages":[{"role":"tool","content":json.dumps({"data":{"fragments":fragments}})}]},2)
    proposal=json.loads(reply["choices"][0]["message"]["content"])
    assert all(proposal["references"][field]==["frag-"+field] for field in VALUES)


@pytest.fixture
def server():
    import http.server
    state=FixtureState()
    server=http.server.ThreadingHTTPServer(("127.0.0.1",0),handler_for(state))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield "http://127.0.0.1:"+str(server.server_port),state
    finally:state.release.set();server.shutdown();server.server_close();thread.join(timeout=3)


def send(url,body=None,auth=True,raw=None):
    headers={"Authorization":"Bearer "+KEY} if auth else {}
    data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
    request=urllib.request.Request(url,data=data,headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=5) as reply:return reply.status,json.loads(reply.read())
    except urllib.error.HTTPError as error:return error.code,json.loads(error.read())


def test_actual_loopback_fixture_auth_bounds_routes_and_no_upstream(server):
    url,state=server
    assert send(url+"/health",auth=False)[0]==200
    assert send(url+"/state",auth=False)[0]==404
    assert send(url+"/chat/completions",{},auth=False)[0]==401
    assert send(url+"/api/resource/Supplier%20Quotation",{})[0]==404
    assert send(url+"/chat/completions",raw=b'{"model":1,"model":2}')[0]==400
    assert send(url+"/chat/completions",raw=b"x"*(MAX_BYTES+1))[0]==413
    assert state.calls==0
    assert send(url+"/chat/completions",{"model":MODEL,"messages":[{"role":"user"}]})[0]==200
    assert state.calls==1


def test_hold_release_keeps_counter_and_does_not_record_payload(server):
    from concurrent.futures import ThreadPoolExecutor
    import time
    url,state=server
    assert send(url+"/control",{"action":"hold"})[0]==200
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(send,url+"/chat/completions",
            {"model":MODEL,"messages":[{"role":"user","content":"CANARY_NOT_RETAINED"}]})
        deadline=time.monotonic()+3
        while state.view()["waiting"]!=1 and time.monotonic()<deadline:time.sleep(.01)
        assert state.view()["waiting"]==1
        assert send(url+"/control",{"action":"hold"})[0]==400
        assert send(url+"/control",{"action":"release"})[0]==200
        assert future.result(timeout=3)[0]==200
    assert state.view()=={"scripted":True,"calls":1,"waiting":0,"hold":False}
    assert "CANARY_NOT_RETAINED" not in json.dumps(send(url+"/state")[1])


def test_fixture_compose_uses_worker_namespace_without_host_provider_port():
    # Structural contract, not a Compose runtime proof.
    text=(ROOT/"compose.candidate-fixture.yaml").read_text()
    assert 'network_mode: "service:candidate-fixture"' in text
    assert "ports:" not in text and "host.docker.internal" not in text
    assert ":ro" in text and "read_only: true" in text
    runner=(ROOT/"scripts/verify_candidate_stack.py").read_text()
    assert "page.route(" not in runner and ".fulfill(" not in runner
    assert 'compose("kill","-s","SIGKILL","candidate-worker")' in runner
