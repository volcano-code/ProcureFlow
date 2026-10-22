"""No SDK/model needed here. Real SDK+API tests live under integrations/mcp/tests."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import httpx
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
from procureflow.domain import digest
from procureflow.mcp_bridge import (AdviceBridge, BridgeConfig, BridgeError, checked_snapshot, MAX_CALLS)
from procureflow.read_tools import TOOLS, invoke_snapshot, validate_tool_arguments

RUN_ID = "adv_" + "a" * 32
TOKEN = "synthetic-mcp-private-token-" + "0" * 32

def config(**kw):
    return BridgeConfig(**{"base_url": "http://127.0.0.1:8000", "token": TOKEN,
                          "advice_run_id": RUN_ID, **kw})

def specimen():
    docs = {"doc_1": {"id":"doc_1", "sha256":"b"*64, "filename":"quote.txt",
        "trust":"untrusted_source_content", "fragments":[{"id":"frag_1","text":"unit_price: 10","line":1}]}}
    quotes = [{"id":"q_1","document_id":"doc_1","values":{"unit_price":"10"}}]
    snapshot = {"request":{"id":"req_1","version":1}, "quotes":quotes,"documents":docs,
                "policy":{"version":1,"policy_hash":"c"*64}, "quote_collection_hash":digest(quotes)}
    return {"id":RUN_ID,"current":True,"stale_reason":None,
        "request_id":"req_1","request_version":1,"input_snapshot":snapshot,
        "input_hash":digest(snapshot),"policy_version":1,"policy_hash":"c"*64,
        "quote_collection_hash":digest(quotes)}

def test_exact_registry_and_copy_boundary():
    names = {t["function"]["name"] for t in TOOLS}
    assert names == {"get_comparison","get_evidence","search_policy"}
    source=specimen()["input_snapshot"]
    copy=invoke_snapshot(source,"get_comparison",{})
    copy["quotes"].clear()
    assert source["quotes"]
    assert invoke_snapshot(source,"search_policy",{}) == source["policy"]
    with pytest.raises(Exception) as e:
        invoke_snapshot(source,"get_evidence",{"document_id":"other"})
    assert e.value.code == "TOOL_SCOPE_DENIED"

@pytest.mark.parametrize("name,args", [
    ("execute",{}), ("approve",{}), ("get_comparison",{"tenant_id":"other"}),
    ("search_policy",{"url":"https://untrusted.invalid"}),
    ("get_evidence",{"document_id":12}),("get_evidence",{}),("get_evidence",None),
])
def test_bad_tool_arguments_never_contact_api(name,args):
    seen=[]
    async def scenario():
        bridge=AdviceBridge(config(), transport=httpx.MockTransport(lambda req: seen.append(req)))
        try:
            with pytest.raises(BridgeError):
                await bridge.call(name,args)
        finally: await bridge.close()
    asyncio.run(scenario())
    assert seen == []

@pytest.mark.parametrize("url", [
    "http://example.com", "http://localhost", "http://127.0.0.1.evil.test",
    "http://2130706433", "http://169.254.169.254", "file:///tmp/data",
    "http://user:pass@127.0.0.1", "http://127.0.0.1/path",
    "http://127.0.0.1?secret=x", "http://127.0.0.1#x",
    "https://untrusted.invalid", "http://127.0.0.1:70000", "http://127.0.0.1\\@evil.test",
])
def test_invalid_upstream_configuration(url):
    with pytest.raises(BridgeError, match="MCP_CONFIG_INVALID"):
        config(base_url=url)

def test_https_requires_exact_approved_host_and_private_token():
    assert config(base_url="https://procureflow.example.com",approved_host="procureflow.example.com").base_url.endswith("/")
    with pytest.raises(BridgeError): config(base_url="https://procureflow.example.com",approved_host="other.example.com")
    with pytest.raises(BridgeError): config(base_url="https://procureflow.example.com",approved_host="procureflow.example.com",token="demo-buyer")
    with pytest.raises(BridgeError): config(base_url="https://127.0.0.1",approved_host="127.0.0.1")
    assert TOKEN not in repr(config())
    with pytest.raises(BridgeError): config(advice_run_id="../execute")
    with pytest.raises(BridgeError): config(token="has\nnewline")

def test_env_is_explicit_and_has_no_ambient_model_or_erp_authority():
    values={"PF_MCP_BEARER_TOKEN":TOKEN,"PF_MCP_ADVICE_RUN_ID":RUN_ID,
            "LLM_API_KEY":"must-not-read","ERP_API_KEY":"must-not-read","PF_DATABASE_URL":"must-not-read"}
    loaded=BridgeConfig.from_environment(values)
    assert loaded.token==TOKEN
    assert "must-not-read" not in repr(loaded)
    with pytest.raises(BridgeError): BridgeConfig.from_environment({"LLM_API_KEY":"not-a-mcp-token"})

@pytest.mark.parametrize("field,value,code", [
    ("current",False,"MCP_CONTEXT_STALE"),("current",1,"MCP_CONTEXT_STALE"),
    ("stale_reason","changed","MCP_CONTEXT_STALE"),("id","adv_other","MCP_CONTEXT_INVALID"),
    ("input_hash","0"*64,"MCP_CONTEXT_INVALID"),("input_snapshot",None,"MCP_CONTEXT_INVALID"),
    ("request_id","req_other","MCP_CONTEXT_INVALID"),("request_version",2,"MCP_CONTEXT_INVALID"),
    ("policy_hash","e"*64,"MCP_CONTEXT_INVALID"),("policy_version",2,"MCP_CONTEXT_INVALID"),
    ("quote_collection_hash","e"*64,"MCP_CONTEXT_INVALID"),
])
def test_receipt_tamper_rejected(field,value,code):
    receipt=specimen();receipt[field]=value
    with pytest.raises(BridgeError,match=code): checked_snapshot(receipt,config(),None)

def test_pinned_hash_and_same_session_binding():
    receipt=specimen()
    with pytest.raises(BridgeError,match="MCP_CONTEXT_STALE"):
        checked_snapshot(receipt,config(expected_input_hash="f"*64),None)
    with pytest.raises(BridgeError,match="MCP_CONTEXT_STALE"):
        checked_snapshot(receipt,config(),"f"*64)
    assert checked_snapshot(receipt,config(),receipt["input_hash"])[0]==receipt["input_snapshot"]

def test_snapshot_membership_cannot_be_swapped_even_with_recomputed_digest():
    receipt=specimen(); receipt["input_snapshot"]["documents"]["doc_1"]["id"]="doc_else"
    receipt["input_hash"]=digest(receipt["input_snapshot"])
    with pytest.raises(BridgeError): checked_snapshot(receipt,config(),None)

def test_reads_each_time_no_cached_authority_or_snapshot():
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200,json=specimen()) if len(seen)==1 else httpx.Response(401,json={"secret":TOKEN})
    async def scenario():
        bridge=AdviceBridge(config(),transport=httpx.MockTransport(handler))
        try:
            first=await bridge.call("get_comparison",{})
            assert first["advisory_only"] and first["result"]["quotes"]
            with pytest.raises(BridgeError,match="MCP_UNAUTHENTICATED") as e:
                await bridge.call("get_comparison",{})
            assert TOKEN not in str(e.value)
        finally: await bridge.close()
    asyncio.run(scenario())
    assert len(seen)==2
    assert all(r.method=="GET" and r.url.path==f"/api/v1/advice-runs/{RUN_ID}" for r in seen)
    assert all(r.headers["authorization"]==f"Bearer {TOKEN}" for r in seen)

@pytest.mark.parametrize("status,code",[
    (301,"MCP_UPSTREAM_UNAVAILABLE"),(401,"MCP_UNAUTHENTICATED"),
    (403,"MCP_ACCESS_DENIED"),(404,"MCP_ACCESS_DENIED"),(429,"MCP_UPSTREAM_UNAVAILABLE"),
    (500,"MCP_UPSTREAM_UNAVAILABLE"),
])
def test_status_no_retry_no_redirect_no_error_body_echo(status,code):
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(status,headers={"Location":"https://untrusted.invalid"},json={"token":TOKEN})
    async def scenario():
        bridge=AdviceBridge(config(),transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(BridgeError,match=code) as e: await bridge.call("search_policy",{})
            assert TOKEN not in str(e.value)
        finally: await bridge.close()
    asyncio.run(scenario())
    assert len(seen)==1

@pytest.mark.parametrize("content,headers,code",[
    (b'{"id":1,"id":2}',{"content-type":"application/json"},"MCP_UPSTREAM_INVALID"),
    (b'{"x":NaN}',{"content-type":"application/json"},"MCP_UPSTREAM_INVALID"),
    (b'<html>secret</html>',{"content-type":"text/html"},"MCP_UPSTREAM_INVALID"),
    (b'[]',{"content-type":"application/json"},"MCP_CONTEXT_INVALID"),
    (b' ' * 256001,{"content-type":"application/json"},"MCP_RESPONSE_LIMIT"),
], ids=["duplicate-key","non-finite","html","not-object","oversize"])
def test_malformed_or_oversized_response(content,headers,code):
    async def scenario():
        bridge=AdviceBridge(config(),transport=httpx.MockTransport(
            lambda req:httpx.Response(200,content=content,headers=headers)))
        try:
            with pytest.raises(BridgeError,match=code): await bridge.call("get_comparison",{})
        finally: await bridge.close()
    asyncio.run(scenario())

def test_absolute_timeout_and_call_limit():
    async def slow(req):
        await asyncio.sleep(0.1)
        return httpx.Response(200,json=specimen())
    async def scenario():
        bridge=AdviceBridge(config(),transport=httpx.MockTransport(slow),deadline=0.01)
        try:
            with pytest.raises(BridgeError,match="MCP_TIMEOUT"): await bridge.call("search_policy",{})
            bridge._calls=MAX_CALLS
            with pytest.raises(BridgeError,match="MCP_CALL_LIMIT"): await bridge.call("search_policy",{})
        finally: await bridge.close()
    asyncio.run(scenario())

def test_cancellation_is_not_swallowed():
    started=asyncio.Event()
    async def slow(req):
        started.set()
        await asyncio.sleep(30)
    async def scenario():
        bridge=AdviceBridge(config(),transport=httpx.MockTransport(slow))
        try:
            task=asyncio.create_task(bridge.call("get_comparison",{}))
            await started.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        finally: await bridge.close()
    asyncio.run(scenario())
