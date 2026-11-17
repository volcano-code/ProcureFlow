"""Official SDK subprocess <-> stdio server <-> real HTTP API and pilot auth.

All accounts/data are disposable synthetic fixtures. No LLM or external ERP.
This suite requires requirements-dev.txt + requirements-mcp.txt; never skip it.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import httpx
import pytest
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"services/api")]
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from procureflow.read_tools import TOOLS

@pytest.fixture
def pilot_api(tmp_path,monkeypatch,request):
    for key in list(os.environ):
        if key.startswith(("PF_","ERP_","LLM_","LANGSMITH_","LANGCHAIN_")):
            monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv("PF_DATA_DIR",str(tmp_path/"bootstrap"))
    monkeypatch.setenv("PF_MODE","demo");monkeypatch.setenv("PF_ERP_MODE","mock")
    from procureflow.app import create_app
    from procureflow.auth import IdentityService
    from procureflow.config import Settings
    from procureflow.db import Database
    from procureflow.erp import MockERP
    import uvicorn
    settings=Settings(data_dir=tmp_path/"pilot",mode="pilot",
        database_url=f"sqlite:///{tmp_path/'pilot.sqlite3'}",auth_tokens={})
    db=Database(settings.database_url,create_schema=True)
    identities=IdentityService(db,settings)
    for tenant in ("mcp-test","other-workspace"):
        identities.create_tenant(tenant)
        for user,role in (("buyer","buyer"),("auditor","auditor")):
            identities.set_membership(tenant,user,role)
    erp=MockERP(tmp_path/"erp.sqlite3")
    def prohibited(*args,**kwargs):
        raise AssertionError("MCP_MUST_NOT_CONTACT_ERP")
    erp.create_draft=prohibited;erp.find_by_operation=prohibited
    app=create_app(settings,db,erp)
    sock=socket.socket();sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="critical",access_log=False))
    thread=threading.Thread(target=server.run,kwargs={"sockets":[sock]},daemon=True);thread.start()
    url=f"http://127.0.0.1:{port}"
    client=httpx.Client(base_url=url,trust_env=False,timeout=15)
    def cleanup():
        client.close();server.should_exit=True;thread.join(timeout=5);sock.close()
        assert not thread.is_alive()
    request.addfinalizer(cleanup)
    for _ in range(200):
        if server.started:break
        time.sleep(.02)
    assert server.started
    def login(user="buyer",tenant="mcp-test"):
        invite=identities.issue_invite(tenant,user)
        r=client.post("/api/v1/auth/login",json={"credential":invite["credential"]})
        assert r.status_code==200
        return r.json()["token"]
    buyer=login();auditor=login("auditor");other=login("auditor","other-workspace")
    headers={"Authorization":f"Bearer {buyer}"}
    r=client.post("/api/v1/requests",headers=headers,json={
        "title":"MCP synthetic request","sku":"STAND-01","quantity":"20","budget":"30000.00","max_delivery_days":14})
    assert r.status_code==201,r.text
    request=r.json()
    raw=(ROOT/"evals/fixtures/supplier-a.txt").read_bytes()
    r=client.post(f"/api/v1/requests/{request['id']}/documents",headers=headers,files={"file":("quote.txt",raw)})
    assert r.status_code==201,r.text
    quote=r.json()
    # Import increments the business version: read current state, do not weaken
    # the production optimistic concurrency check to accommodate the fixture.
    r=client.get(f"/api/v1/requests/{request['id']}",headers=headers)
    assert r.status_code==200
    request=r.json()
    r=client.post(f"/api/v1/requests/{request['id']}/advice-runs",headers=headers,
        json={"expected_version":request["version"],"idempotency_key":"mcp-bound-test-01"})
    assert r.status_code==201,r.text
    receipt=r.json()
    def counts():
        with db.transaction() as session:
            return {name:session.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
                    for name in db.engine.dialect.get_table_names(session.connection())}
    yield dict(url=url,client=client,headers=headers,buyer=buyer,auditor=auditor,other=other,
        request=request,quote=quote,receipt=receipt,counts=counts,identities=identities,tmp=tmp_path)

@asynccontextmanager
async def session_for(api,token=None):
    env={"PATH":os.environ.get("PATH",""),"PYTHONPATH":str(ROOT/"services/api"),
         "PF_MCP_API_BASE_URL":api["url"],"PF_MCP_ADVICE_RUN_ID":api["receipt"]["id"],
         "PF_MCP_BEARER_TOKEN":token or api["auditor"],
         "PF_MCP_INPUT_HASH":api["receipt"]["input_hash"]}
    # Deliberately provide forbidden ambient config; bridge has no matching consumers.
    env.update(ERP_BASE_URL="https://never-contact.invalid",ERP_API_KEY="not-a-real-erp-key",
               LLM_BASE_URL="https://never-contact.invalid",LLM_API_KEY="not-a-real-model-key")
    log=api["tmp"]/"mcp-stderr.log"
    with log.open("w") as stderr:
        async with stdio_client(StdioServerParameters(command=sys.executable,
                args=[str(ROOT/"scripts/start_mcp.py")],env=env,cwd=api["tmp"]),errlog=stderr) as (read,write):
            async with ClientSession(read,write) as session:
                initialized=await session.initialize()
                assert initialized.serverInfo.name=="procureflow-readonly"
                yield session
    logged=log.read_text()
    assert (token or api["auditor"]) not in logged
    assert "not-a-real-erp-key" not in logged

def error_code(result):
    assert result.isError
    return json.loads(result.content[0].text)["error"]["code"]

def test_real_sdk_discovery_and_three_tools_have_no_writes(pilot_api):
    api=pilot_api;before=api["counts"]()
    async def scenario():
        async with session_for(api) as session:
            tools=(await session.list_tools()).tools
            assert {t.name:t.inputSchema for t in tools}=={d["function"]["name"]:d["function"]["parameters"] for d in TOOLS}
            assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
            c=await session.call_tool("get_comparison",{})
            assert not c.isError
            result=c.structuredContent
            assert result["advisory_only"] is True
            assert result["result"]["request"]["id"]==api["request"]["id"]
            assert result["result"]["quotes"][0]["values"]["unit_price"]=="1200.00"
            assert result["result"]["quotes"][0]["confirmed_by"] is None
            p=await session.call_tool("search_policy",{})
            assert p.structuredContent["input_hash"]==result["input_hash"]
            e=await session.call_tool("get_evidence",{"document_id":api["quote"]["document_id"]})
            assert not e.isError and e.structuredContent["result"]["trust"]=="untrusted_source_content"
            assert e.structuredContent["result"]["fragments"]
            assert api["auditor"] not in str(c)
            await session.send_ping()
    asyncio.run(scenario())
    assert before==api["counts"]()

@pytest.mark.parametrize("tool,args,code",[
    ("execute",{},"TOOL_POLICY_DENIED"),
    ("get_comparison",{"tenant_id":"other-workspace"},"TOOL_ARGUMENTS_INVALID"),
    ("get_evidence",{"document_id":"doc_not_in_request"},"TOOL_SCOPE_DENIED"),
    ("search_policy",{"role":"approver"},"TOOL_ARGUMENTS_INVALID"),
])
def test_real_protocol_denies_write_or_scope_override(pilot_api,tool,args,code):
    before=pilot_api["counts"]()
    async def scenario():
        async with session_for(pilot_api) as session:
            assert error_code(await session.call_tool(tool,args))==code
    asyncio.run(scenario())
    assert before==pilot_api["counts"]()

def test_pilot_revocation_is_checked_on_next_call(pilot_api):
    api=pilot_api
    async def scenario():
        async with session_for(api) as session:
            assert not (await session.call_tool("get_comparison",{})).isError
            response=api["client"].post("/api/v1/auth/logout",
                    headers={"Authorization":f"Bearer {api['auditor']}"})
            assert response.status_code==200
            assert error_code(await session.call_tool("get_comparison",{}))=="MCP_UNAUTHENTICATED"
    asyncio.run(scenario())

@pytest.mark.parametrize("kind,expected",[("other","MCP_ACCESS_DENIED"),("invalid","MCP_UNAUTHENTICATED")])
def test_tenant_isolation_and_invalid_token(pilot_api,kind,expected):
    async def scenario():
        token=pilot_api["other"] if kind=="other" else "synthetic-invalid-token"
        async with session_for(pilot_api,token) as session:
            assert error_code(await session.call_tool("get_comparison",{}))==expected
    asyncio.run(scenario())

def test_quote_change_invalidates_bound_context(pilot_api):
    api=pilot_api
    async def scenario():
        async with session_for(api) as session:
            assert not (await session.call_tool("get_comparison",{})).isError
            r=api["client"].post(f"/api/v1/quotes/{api['quote']['id']}/confirm",headers=api["headers"],
                json={"expected_version":1,"acknowledge":True})
            assert r.status_code==200,r.text
            assert error_code(await session.call_tool("get_comparison",{}))=="MCP_CONTEXT_STALE"
    asyncio.run(scenario())

def test_same_tenant_other_request_document_cannot_be_read(pilot_api):
    api=pilot_api
    r=api["client"].post("/api/v1/requests",headers=api["headers"],json={
        "title":"Other request","sku":"STAND-01","quantity":"20","budget":"30000.00","max_delivery_days":14})
    assert r.status_code==201
    rid=r.json()["id"]
    r=api["client"].post(f"/api/v1/requests/{rid}/documents",headers=api["headers"],
        files={"file":("other.txt",(ROOT/"evals/fixtures/supplier-a.txt").read_bytes())})
    assert r.status_code==201
    doc=r.json()["document_id"]
    async def scenario():
        async with session_for(api) as session:
            assert error_code(await session.call_tool("get_evidence",{"document_id":doc}))=="TOOL_SCOPE_DENIED"
    asyncio.run(scenario())


def reference_setup(api):
    """Publish through actual pilot HTTP, then bind a new advice receipt."""
    identities=api['identities'];identities.set_membership('mcp-test','reference-approver','approver')
    invite=identities.issue_invite('mcp-test','reference-approver')
    response=api['client'].post('/api/v1/auth/login',json={'credential':invite['credential']})
    assert response.status_code==200
    headers={'Authorization':'Bearer '+response.json()['token']}
    body={'expected_version':0,'title':'运费制度参考','source_label':'MCP合成参考来源',
        'content':'运费未明确时需要澄清，不能默认为零。','reason':'协议验证发布参考'}
    response=api['client'].post('/api/v1/policy-library/documents/freight/versions',headers=headers,json=body)
    assert response.status_code==201,response.text
    revision=response.json()
    response=api['client'].post(f'/api/v1/requests/{api["request"]["id"]}/advice-runs',headers=api['headers'],
        json={'expected_version':api['request']['version'],'idempotency_key':'mcp-reference-bound-01'})
    assert response.status_code==201,response.text
    api['receipt']=response.json()
    return headers,body,revision


def test_official_mcp_search_and_reference_readback_bound_to_library(pilot_api):
    api=pilot_api;_,_,revision=reference_setup(api);before=api['counts']()
    async def scenario():
        async with session_for(api) as session:
            result=await session.call_tool('search_references',{'query':'运费'})
            assert not result.isError,result
            result=result.structuredContent
            hit=result['result']['results'][0]
            assert hit['revision_id']==revision['id'] and hit['advisory_only'] is True
            assert hit['trust']=='reference_text_not_instructions'
            exact=await session.call_tool('get_reference_fragment',{'citation_id':hit['citation_id']})
            assert not exact.isError
            assert exact.structuredContent['result']['results'][0]['fragment']==hit['fragment']
            assert exact.structuredContent['input_hash']==api['receipt']['input_hash']
            assert error_code(await session.call_tool('search_references',{'query':'运费','tenant_id':'other-workspace'}))=='TOOL_ARGUMENTS_INVALID'
    asyncio.run(scenario())
    assert before==api['counts']()


def test_official_mcp_withdrawal_blocks_next_reference_read(pilot_api):
    api=pilot_api;headers,body,_=reference_setup(api)
    async def scenario():
        async with session_for(api) as session:
            result=await session.call_tool('search_references',{'query':'运费'})
            assert not result.isError
            citation=result.structuredContent['result']['results'][0]['citation_id']
            response=api['client'].post('/api/v1/policy-library/documents/freight/versions',headers=headers,
                json={**body,'expected_version':1,'withdrawn':True,'content':''})
            assert response.status_code==201
            assert error_code(await session.call_tool('get_reference_fragment',{'citation_id':citation}))=='MCP_CONTEXT_STALE'
            assert error_code(await session.call_tool('get_comparison',{}))=='MCP_CONTEXT_STALE'
    asyncio.run(scenario())


def test_official_mcp_revoked_session_cannot_search_reference(pilot_api):
    api=pilot_api;reference_setup(api)
    async def scenario():
        async with session_for(api) as session:
            assert not (await session.call_tool('search_references',{'query':'运费'})).isError
            assert api['client'].post('/api/v1/auth/logout',headers={'Authorization':'Bearer '+api['auditor']}).status_code==200
            assert error_code(await session.call_tool('search_references',{'query':'运费'}))=='MCP_UNAUTHENTICATED'
    asyncio.run(scenario())
